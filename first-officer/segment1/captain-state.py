"""Keep the Telegram captain grant and lane receipts next to the existing fence."""

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import quote
from zoneinfo import ZoneInfo


HOME = Path.home()
STATE = HOME / ".local/state/openclaw-conductor"
LANES = HOME / "work/lanes"
RUNNER = LANES / "openclaw-backup2-20261118-1340"
PRIVATE = STATE / "telegram-captain"
LOCAL_AUTHORITY = HOME / ".local/state/openclaw-captain"
LOCAL_NODES = ("mac-mini", "nucbox")
TRANSFER_NAMESPACE = "openclaw-captain-transfer-v1"
STATE_LIMIT = 256 * 1024
TRANSFER_LIMIT = 2 * STATE_LIMIT
SCOPE = "integration-ci"
SHA = re.compile(r"[a-f0-9]{40}")
LANE = re.compile(r"fo-ci-[a-f0-9]{24}")
OWNER = re.compile(r"[1-9][0-9]{0,15}")
DELIVERY = ("reported", "delivery-unverified")
PROJECT_REPO = re.compile(r"[a-z0-9][a-z0-9_.-]{0,99}/[a-z0-9_-][a-z0-9_.-]{0,99}")
PROJECT_ACTIONS = {"investigate", "fix", "verify", "wait", "refresh", "blocked", "closed", "working"}
ACTIVE_PROJECT = ("reserved", "running", "unresolved", "patch-ready", "ci-pending")
BLOCKERS = ("fleet", "gate", "quota", "ci-changed", "ci-unavailable", "ci-pending", "ci-timeout", "ci-paused")


def private_bytes(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Invalid captain receipt")
        data = file.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Captain receipt exceeded its limit")
        return data


def read_private(path, limit):
    return json.loads(private_bytes(path, limit))


def save(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temp = Path(file.name)
        try:
            file.write((json.dumps(data, sort_keys=True) + "\n").encode())
            file.flush()
            os.fsync(file.fileno())
            os.replace(temp, path)
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temp.unlink(missing_ok=True)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def private_directory(path):
    info = path.lstat()
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077


def check_epoch(epoch):
    if type(epoch) is not int or not 0 <= epoch < 1_000_000_000_000:
        raise ValueError("Invalid Captain epoch")
    return epoch


class StandaloneParked(ValueError):
    """The host has deliberately yielded authority."""


def gateway_environment(path):
    settings = read_private(path, 16 * 1024)
    allowed = {"OPENCLAW_LITELLM_API_KEY", "FO_HATCHET_LANE_TOKEN", "FO_RESTATE_LANE_TOKEN", "FO_QUEUE_HOSTS"}
    if not isinstance(settings, dict) or not settings.keys() <= allowed or any(
            not isinstance(value, str) or not value or len(value) > 4096 or
            any(character.isspace() for character in value) for value in settings.values()):
        raise ValueError("The standalone gateway environment file is invalid")
    return {**os.environ, **settings}


class StandaloneAuthority:
    """Move authority with a signed handoff after parking the source host."""

    def __init__(self, node, root):
        if node not in LOCAL_NODES or not private_directory(root):
            raise ValueError("Standalone Captain authority unavailable")
        self.node = node
        self.peer = next(name for name in LOCAL_NODES if name != node)
        self.AUTHORITY = root
        self.path = root / "lease.json"

    def private_authority(self, path):
        return path == self.AUTHORITY and private_directory(path)

    def load_fence(self):
        return lambda _: self

    def enabled(self):
        return True

    @contextmanager
    def _lock(self, name):
        with os.fdopen(os.open(self.AUTHORITY / name,
                               os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "rb") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Invalid standalone authority lock")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield lock.fileno()

    def _locked(self):
        return self._lock("authority.lock")

    def run_gateway(self, executable, environment=None):
        if not executable.is_absolute() or executable.name != "openclaw":
            raise ValueError("Expected the installed OpenClaw executable")
        with self._lock("gateway.lock") as lease_fd:
            if self.status().state != "held":
                raise StandaloneParked("This host is parked")

            # The child retains the lock if the supervisor is interrupted.
            child_env = {**(os.environ if environment is None else environment), "FO_CAPTAIN_PYTHON": sys.executable}
            child = subprocess.Popen([str(executable), "gateway", "run"], pass_fds=(lease_fd,), env=child_env)

            def forward(signum, _frame):
                if child.poll() is None:
                    child.send_signal(signum)

            signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
            previous = {number: signal.signal(number, forward) for number in signals}
            try:
                status = child.wait()
            finally:
                for number, handler in previous.items():
                    signal.signal(number, handler)
            return status if status >= 0 else 128 - status

    def _record(self):
        try:
            row = read_private(self.path, TRANSFER_LIMIT)
        except FileNotFoundError:
            return {"node": self.node, "epoch": 0, "active": False}
        if row["node"] != self.node or type(row["active"]) is not bool or check_epoch(row["epoch"]) < 1:
            raise ValueError("Invalid standalone authority record")
        return row

    def _read_unlocked(self):
        row = self._record()
        return SimpleNamespace(epoch=row["epoch"], holder="first-officer" if row["active"] else "standby",
                               state="held" if row["active"] else "parked")

    def status(self):
        with self._locked():
            return self._read_unlocked()

    def initialize(self):
        with self._locked():
            if self.node != "mac-mini" or self._record()["epoch"] != 0:
                raise ValueError("Initial authority is only available on an uninitialized Mac Mini")
            self._peer_key()
            save(self.path, {"node": self.node, "epoch": 1, "active": True})
        return {"epoch": 1}

    @staticmethod
    def _payload(bundle):
        return json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()

    def _identity(self):
        identity = self.AUTHORITY / "handoff-key"
        private_bytes(identity, 4096)
        return identity

    def _peer_key(self):
        fields = private_bytes(self.AUTHORITY / "peer.pub", 256).strip().split()
        if (len(fields) < 2 or fields[0] != b"ssh-ed25519" or
                not re.fullmatch(rb"[A-Za-z0-9+/]{68}", fields[1])):
            raise ValueError("Expected the paired host's Ed25519 public key")
        local = subprocess.run(
            ["/usr/bin/ssh-keygen", "-y", "-f", str(self._identity())], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=True,
        ).stdout.split()
        if local[0] != b"ssh-ed25519" or local[:2] == fields[:2]:
            raise ValueError("The Mac Mini and Nuc need distinct handoff keys")
        return b" ".join(fields[:2])

    def _sign(self, bundle):
        result = subprocess.run(
            ["/usr/bin/ssh-keygen", "-Y", "sign", "-f", str(self._identity()), "-n", TRANSFER_NAMESPACE],
            input=self._payload(bundle), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5, check=True,
        )
        return result.stdout.decode("ascii")

    def release(self, epoch, state_lock):
        """Park first. The same handoff can be exported again after a lost response."""
        check_epoch(epoch)
        with self._lock("gateway.lock"), state_lock() as data, self._locked():
            row = self._record()
            if row["active"] and row["epoch"] == epoch:
                if any(job["state"] in ("launching", "running") for job in data["jobs"]):
                    raise ValueError("Captain still has an active review")
                if any(job["state"] in ACTIVE_PROJECT for job in data.get("projectJobs", [])):
                    raise ValueError("Resolve the active project coding job before transferring authority")
                if data.get("blocked", {}).get("reason") == "ci-pending":
                    raise ValueError("Pause the pending review before transferring authority")
                self._peer_key()
                state = {**data, "grant": None}
                bundle = {"from": self.node, "to": self.peer, "epoch": check_epoch(epoch + 1), "state": state}
                save(self.path, {"node": self.node, "epoch": bundle["epoch"], "active": False, "outbound": bundle})
            elif not row["active"] and row["epoch"] == epoch + 1 and "outbound" in row:
                bundle = row["outbound"]
            else:
                raise ValueError("Standalone Captain epoch changed")
            return {"transfer": bundle, "signature": self._sign(bundle)}

    def _verify(self, receipt):
        bundle = receipt["transfer"]
        if bundle["from"] != self.peer or bundle["to"] != self.node or check_epoch(bundle["epoch"]) < 2:
            raise ValueError("The handoff names a different peer or epoch")
        peer_key = self._peer_key()
        with tempfile.TemporaryDirectory(dir=self.AUTHORITY) as directory:
            allowed = Path(directory) / "allowed"
            allowed.write_bytes(self.peer.encode() + b" " + peer_key + b"\n")
            signature = Path(directory) / "transfer.sig"
            signature.write_text(receipt["signature"], encoding="ascii")
            subprocess.run(
                ["/usr/bin/ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", self.peer,
                 "-n", TRANSFER_NAMESPACE, "-s", str(signature)],
                input=self._payload(bundle), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=True,
            )
        return bundle

    def accept(self, receipt, state_path, state_lock):
        bundle = self._verify(receipt)
        with self._lock("gateway.lock"), state_lock(), self._locked():
            row = self._record()
            if row["active"] or bundle["epoch"] <= row["epoch"]:
                raise ValueError("The standalone Captain handoff is stale")
            # Persist work and replay history before granting authority. A crash
            # between these writes leaves the host parked and the import retryable.
            save(state_path, bundle["state"])
            save(self.path, {"node": self.node, "epoch": bundle["epoch"], "active": True})
        return {"epoch": bundle["epoch"]}


class LiveGate:
    """Probe the same local lock and hold marker used by the host's full gate."""

    def __init__(self, authority):
        self.authority = authority

    @contextmanager
    def open(self):
        config = read_private(self.authority.AUTHORITY / "gate.json", 4096)
        if not isinstance(config, dict) or config.keys() != {"directory", "lock", "hold"}:
            raise ValueError("Invalid standalone gate policy")
        directory = Path(config["directory"])
        if (not directory.is_absolute() or directory.resolve() != directory or
                any(not isinstance(config[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", config[key])
                    for key in ("lock", "hold")) or config["lock"] == config["hold"]):
            raise ValueError("Invalid standalone gate path")
        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(parent)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError("The standalone gate directory is unsafe")
            handle = os.open(config["lock"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                info = os.fstat(handle)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_nlink != 1:
                    raise ValueError("The standalone gate lock is unsafe")
                try:
                    fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    running = False
                except BlockingIOError:
                    running = True
                try:
                    marker = os.stat(config["hold"], dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    hold = False
                else:
                    if not stat.S_ISREG(marker.st_mode) or marker.st_uid != os.getuid() or marker.st_mode & 0o022:
                        raise ValueError("The standalone hold marker is unsafe")
                    hold = True
                yield {"node": self.authority.node, "observed": int(time.time()), "hold": hold, "running": running}
            finally:
                os.close(handle)
        finally:
            os.close(parent)

    def read(self):
        with self.open() as reading:
            return reading


def local_capacity(authority):
    policy = read_private(authority.AUTHORITY / "capacity.json", 4096)
    if not isinstance(policy, dict) or policy.keys() != {"commands", "limit"}:
        raise ValueError("Invalid standalone capacity policy")
    commands, limit = policy["commands"], policy["limit"]
    if (type(limit) is not int or not 1 <= limit <= 100 or not isinstance(commands, list)
            or not 1 <= len(commands) <= 32 or any(
                not isinstance(command, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", command)
                for command in commands)):
        raise ValueError("Invalid standalone capacity policy")
    listing = subprocess.run(
        ["/bin/ps", "-U", str(os.getuid()), "-o", "comm="], check=True, timeout=1,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace",
    ).stdout
    names = set(commands)
    used = sum(Path(line.strip()).name in names for line in listing.splitlines())
    return {"node": authority.node, "observed": int(time.time()), "used": used, "limit": limit}


def github_get(route, *, home=None):
    home = HOME if home is None else home
    candidates = (Path("/usr/bin/gh"), home / ".local/bin/gh", Path("/opt/homebrew/bin/gh"), Path("/usr/local/bin/gh"))
    binary = next((candidate for candidate in candidates if candidate.is_file() and os.access(candidate, os.X_OK)), None)
    if binary is None:
        raise ValueError("The approved GitHub CLI is unavailable")
    result = subprocess.run(
        ["/usr/bin/nice", "-n", "10", str(binary), "api", "--method", "GET", route, "--hostname", "github.com"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=4,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "GH_PROMPT_DISABLED": "1"},
    )
    return json.loads(result.stdout)


def current_project_evidence(request, branch, read=None):
    read = github_get if read is None else read
    root = f"repos/{request['repository']}"
    issue = read(f"{root}/issues/{request['issue']}")
    if (issue.get("number") != request["issue"] or "pull_request" in issue or
            str(issue.get("repository_url", "")).lower() != f"https://api.github.com/{root}" or
            issue.get("state") != "open"):
        return False
    ref = read(f"{root}/git/ref/heads/{quote(branch, safe='/')}")
    target = ref.get("object")
    return (ref.get("ref") == f"refs/heads/{branch}" and isinstance(target, dict) and
            target.get("sha") == request["head"] and target.get("type") == "commit")


def project_pr_checks(request, branch, candidate, verification):
    number, required = verification["pr"], verification["checks"]
    root = f"repos/{request['repository']}"
    path = f"{root}/pulls/{number}"

    def exact(pull):
        return (pull.get("number") == number and pull.get("state") == "open" and
                pull.get("head", {}).get("sha") == candidate and pull.get("base", {}).get("ref") == branch and
                pull.get("base", {}).get("repo", {}).get("full_name", "").lower() == request["repository"])

    if not exact(github_get(path)):
        return "stale-candidate"
    comparison = github_get(f"{root}/compare/{request['head']}...{candidate}")
    if comparison.get("status") != "ahead" or (comparison.get("merge_base_commit") or {}).get("sha") != request["head"]:
        return "stale-candidate"
    checks = github_get(f"{root}/commits/{candidate}/check-runs?per_page=100&filter=latest")
    if not exact(github_get(path)) or not current_project_evidence(request, branch):
        return "stale-candidate"
    rows = checks.get("check_runs")
    if not isinstance(rows, list) or checks.get("total_count") != len(rows):
        return "ci-pending"
    for name in required:
        matched = [row for row in rows if row.get("name") == name and row.get("head_sha") == candidate and
                   (row.get("app") or {}).get("slug") == "github-actions"]
        if len(matched) != 1 or matched[0].get("status") != "completed":
            return "ci-pending"
        if matched[0].get("conclusion") != "success":
            return "ci-failed"
    return "verified"


def project_daily_date(root, instant):
    try:
        enabled = read_private(root / "project-daily.json", 1024)
    except FileNotFoundError:
        return None
    if not isinstance(enabled, dict) or enabled.keys() != {"enabled"} or type(enabled["enabled"]) is not bool:
        raise ValueError("Invalid daily project profile")
    local = instant.astimezone(ZoneInfo("America/Chicago"))
    return local.date().isoformat() if enabled["enabled"] and local.hour >= 7 else None


def validate_project_snapshot(snapshot):
    if not isinstance(snapshot, dict) or snapshot.keys() != {"projects", "recommended"}:
        raise ValueError("Invalid daily snapshot")
    projects, recommendation = snapshot["projects"], snapshot["recommended"]
    if not isinstance(projects, list) or not 1 <= len(projects) <= 2:
        raise ValueError("A daily snapshot needs verified project evidence")
    for project in projects:
        if (not isinstance(project, dict) or project.keys() not in (
                {"repository", "issue", "action"}, {"repository", "issue", "action", "head", "ci"}) or
                not isinstance(project["repository"], str) or not PROJECT_REPO.fullmatch(project["repository"]) or
                type(project["issue"]) is not int or not 1 <= project["issue"] <= 99_999_999 or
                project["action"] not in PROJECT_ACTIONS or
                ("head" in project and (not isinstance(project["head"], str) or not SHA.fullmatch(project["head"]) or
                 project["ci"] not in ("changed", "passing", "failing", "pending", "unavailable")))):
            raise ValueError("The daily snapshot contains invalid evidence")
    if (len({row["repository"] for row in projects}) != len(projects) or
            recommendation is not None and (recommendation not in projects or
                recommendation["action"] not in {"investigate", "fix", "verify"})):
        raise ValueError("The daily recommendation is outside the current evidence")
    return projects, recommendation


def project_verification(root, request, candidate):
    expected = {"repository": request["repository"], "issue": request["issue"], "commit": candidate}
    try:
        verification = read_private(root / "project-verification.json", 4096)
    except FileNotFoundError:
        return None
    if not isinstance(verification, dict) or any(verification.get(key) != value for key, value in expected.items()):
        return None
    checks, pr = verification.get("checks"), verification.get("pr")
    if (type(pr) is not int or not 0 < pr < 100_000_000 or not isinstance(checks, list) or
            not 0 < len(checks) <= 6 or not all(isinstance(check, str) and 0 < len(check) <= 100 for check in checks) or
            len(set(checks)) != len(checks)):
        raise ValueError("The independent verification profile is invalid")
    return verification


def brokered_project_ci(root, evidence):
    if not isinstance(evidence, dict) or evidence.keys() != {"request", "commit"} or not private_directory(root):
        raise ValueError("Expected an exact brokered candidate")
    request, commit = evidence["request"], evidence["commit"]
    queue = CaptainQueue(root).queue
    if not isinstance(request, dict) or request.keys() != queue.PROJECT_REQUEST:
        raise ValueError("Expected an exact brokered project identity")
    queue.canonical(request)
    if not isinstance(commit, str) or not SHA.fullmatch(commit) or commit == request["head"]:
        raise ValueError("Invalid brokered candidate commit")
    verification = project_verification(root, request, commit)
    if verification is None:
        return {"state": "patch-ready"}
    branch = verification.get("branch")
    if (not isinstance(branch, str) or len(branch) > 150
            or not re.fullmatch(r"[a-zA-Z0-9_-]+(?:[./-][a-zA-Z0-9_-]+)*", branch)):
        raise ValueError("Brokered verification needs the exact approved base branch")
    state = project_pr_checks(request, branch, commit, verification)
    return {"state": state, "pr": verification["pr"]}


def brokered_project_submit(home, queue, broker, epoch, repository, issue):
    root = home / ".local/state/openclaw-captain"
    try:
        if not private_directory(root):
            return {"result": "scope-denied"}
        grant = read_private(root / "project-grant.json", 4096)
        if not isinstance(grant, dict) or set(grant) != {
                "repository", "issue", "head", "epoch", "worker", "branch", "expires", "planSha256"}:
            return {"result": "scope-denied"}
        identity = {"repository": repository, "issue": issue, "head": grant["head"], "epoch": epoch}
        request = {**identity, "lane": queue.project_lane(**identity)}
        queue.canonical(request)
        branch, expires, plan_hash = grant["branch"], grant["expires"], grant["planSha256"]
        if (any(type(grant[key]) is not type(value) or grant[key] != value for key, value in identity.items())
                or grant["worker"] != "local-qwen" or type(expires) is not int
                or not int(time.time()) < expires <= int(time.time()) + 3600
                or not isinstance(branch, str) or len(branch) > 150
                or not re.fullmatch(r"[a-zA-Z0-9_-]+(?:[./-][a-zA-Z0-9_-]+)*", branch)
                or not isinstance(plan_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", plan_hash)):
            return {"result": "scope-denied"}
        payload = private_bytes(root / "project-worker.json", 8192)
        if digest(payload) != plan_hash or json.loads(payload).get("request") != identity:
            return {"result": "scope-denied"}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"result": "scope-denied"}

    authority = SimpleNamespace(AUTHORITY=root, node="nucbox")
    with ExitStack() as resources:
        try:
            gate = resources.enter_context(LiveGate(authority).open())
            capacity = local_capacity(authority)
            if gate["hold"] or gate["running"] or capacity["used"] >= capacity["limit"] or os.getloadavg()[0] > 16:
                return {"result": "admission-blocked"}
        except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
            return {"result": "admission-unavailable"}
        try:
            if not current_project_evidence(request, branch, lambda route: github_get(route, home=home)):
                return {"result": "stale-github"}
        except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
            return {"result": "github-unavailable"}
        try:
            if (int(time.time()) >= expires or read_private(root / "project-grant.json", 4096) != grant
                    or digest(private_bytes(root / "project-worker.json", 8192)) != plan_hash):
                return {"result": "scope-denied"}
        except (OSError, ValueError, TypeError):
            return {"result": "scope-denied"}
        receipt = broker.submit_project(epoch, request, plan_hash)

    if receipt.get("retired") is True:
        return {"result": "duplicate", "job": {"request": request, "state": "retired", "commit": receipt["commit"]}}
    state = {"queued": "running", "refused": "refused"}.get(receipt["phase"], "unresolved")
    result = "duplicate" if receipt.get("reused") else state
    return {"result": result, "job": {"request": request, "state": state}}


class CaptainQueue:
    """Use the reviewed dispatcher and its local receipt for standalone reviews."""

    def __init__(self, root):
        source = Path(__file__).with_name("queue-runtime") / "lanes/queue.py"
        if not source.is_file():
            source = Path(__file__).resolve().parents[1] / "lanes/queue.py"
        spec = importlib.util.spec_from_file_location("fo_captain_queue", source)
        self.queue = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.queue)
        self.policy = root / "queue.json"
        self.ledger = root / "queue-receipts.jsonl"
        self.project_policy = root / "project-queue.json"
        self.project_ledger = root / "project-queue-receipts.jsonl"

    def config(self):
        config = self.queue.validate_config(read_private(self.policy, 4096))
        if (config["hatchet"]["workflow"], config["restate"]["service"], config["restate"]["handler"]) != (
                "fo-captain-review", "CaptainReview", "verify"):
            raise ValueError("Expected the reviewed, model-free Captain worker routes")
        return config

    def project_config(self):
        config = self.queue.validate_config(read_private(self.project_policy, 4096))
        if config["hatchet"]["workflow"] != self.queue.PROJECT_WORKFLOW:
            raise ValueError("Expected the scoped Hatchet coding workflow")
        return config

    def project_receipt(self, request):
        try:
            with self.queue.locked_ledger(self.project_ledger, read_only=True) as file:
                return self.queue.queue_state(file, request, self.queue.idempotency_key(request))
        except FileNotFoundError:
            return None

    @staticmethod
    def request(job):
        return {"lane": job["lane"], "head": job["head"], "pull_request": job["pr"]}

    def start(self, job, config):
        try:
            return self.queue.dispatch(config, self.request(job), self.ledger, timeout=3)
        except (OSError, ValueError):
            return self.receipt(job)

    def receipt(self, job):
        request = self.request(job)
        key = self.queue.idempotency_key(request)
        try:
            with self.queue.locked_ledger(self.ledger, read_only=True) as file:
                return self.queue.queue_state(file, request, key)
        except FileNotFoundError:
            return None

    def poll(self, job):
        return self.queue.poll_review(self.config, self.request(job), self.receipt(job))


class Controller:
    def __init__(self, watchdog, *, private=None):
        self.watchdog = watchdog
        self.private = PRIVATE if private is None else private
        self.standalone = isinstance(watchdog, StandaloneAuthority)
        if not watchdog.private_authority(watchdog.AUTHORITY):
            raise ValueError("Authority unavailable")
        self.fence = watchdog.load_fence()(str(watchdog.AUTHORITY / "lease.json"))
        self.private.mkdir(mode=0o700, exist_ok=True)
        if not private_directory(self.private):
            raise ValueError("Captain state is not private")
        self.file = self.private / "state.json"
        self.queue = CaptainQueue(watchdog.AUTHORITY) if self.standalone else None
        self.gate = LiveGate(watchdog) if self.standalone else None

    @contextmanager
    def locked(self, write=False):
        with os.fdopen(os.open(self.private / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "rb") as lock:
            fcntl.flock(lock, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            try:
                data = read_private(self.file, STATE_LIMIT)
            except FileNotFoundError:
                data = {"grant": None, "jobs": [], "lastControl": 0}
            yield data
            if write:
                save(self.file, data)

    def matches(self, record, epoch):
        return self.watchdog.enabled() and record is not None and (
            record.holder, record.state, record.epoch
        ) == ("first-officer", "held", epoch)

    def view(self):
        with self.locked() as data:
            return self.view_locked(data)

    def view_locked(self, data):
        record = self.fence.status()
        if not record:
            raise ValueError("No authority lease")
        active = next((job for job in data["jobs"] if job["state"] in ("launching", "running")), None)
        if self.queue is not None and active is not None:
            receipt = self.queue.receipt(active)
            phase = receipt["phase"] if receipt else "unseen"
            active = {**active, "queuePhase": phase}
            if receipt:
                active.update(backend=receipt["backend"], acceptedAt=receipt["at"])
        recommendations = data.get("projectRecommendations", [])
        return {"lease": {"holder": record.holder, "state": record.state, "epoch": record.epoch},
                "armed": self.watchdog.enabled() and (not self.standalone or record.state == "held"),
                "grant": data["grant"], "blocked": data.get("blocked"),
                **({"standalone": self.watchdog.node} if self.standalone else {}),
                "jobs": data["jobs"][-5:],
                "lastProjectRecommendation": recommendations[-1] if recommendations else None,
                "activeProjectJobs": [job for job in data.get("projectJobs", []) if job["state"] in ACTIVE_PROJECT],
                "recentProjectResults": [job for job in data.get("projectJobs", []) if job["state"] not in ACTIVE_PROJECT][-5:],
                "lastProjectDaily": next(reversed(data.get("projectDaily", [])), None),
                "projectTelegramLedgerFull": len(data.get("projectTelegram", [])) >= 512,
                "unconfirmedProjectDelivery": [row["key"] for row in data.get("projectTelegram", [])
                                               if row["state"] in ("pending", "unverified")],
                "active": active}

    def handoff(self):
        with self.locked(write=True) as data:
            view = self.view_locked(data)
            # These are already structured, local receipts. No Telegram body
            # or model output becomes part of the handoff.
            view["recorded"] = int(time.time())
            save(self.private / "handoff.json", view)
            return view

    def project_telegram_review(self):
        with self.locked() as data:
            return {"unconfirmed": [row for row in data.get("projectTelegram", [])
                                    if row["state"] in ("pending", "unverified")]}

    def project_telegram(self, action, req):
        if not self.standalone or not isinstance(req, dict):
            raise ValueError("The project delivery journal requires standalone authority")
        key = req.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("Invalid project delivery identity")
        with self.locked(write=True) as data, self.fence._locked():
            lease = self.fence._read_unlocked()
            if not lease or not self.matches(lease, lease.epoch):
                return {"result": "stale"}
            journal = data.setdefault("projectTelegram", [])
            existing = next((row for row in journal if row["key"] == key), None)
            if action == "reserve":
                if existing:
                    return {"result": "duplicate"}
                if len(journal) >= 512:
                    return {"result": "ledger-full"}
                journal.append({"key": key, "state": "pending", "epoch": lease.epoch, "at": int(time.time())})
                return {"result": "reserved"}
            if action == "finish":
                outcome = req.get("outcome")
                if outcome not in ("delivered", "unverified"):
                    raise ValueError("Invalid project delivery outcome")
                if not existing or existing["state"] != "pending" or existing["epoch"] != lease.epoch:
                    return {"result": "stale"}
            elif action == "resolve":
                outcome = req.get("outcome")
                if outcome not in ("delivered", "missing"):
                    raise ValueError("Invalid observed project delivery outcome")
                if not existing or existing["state"] not in ("pending", "unverified"):
                    return {"result": "stale"}
            else:
                raise ValueError("Unknown project delivery action")
            existing.update(state=outcome, settled=int(time.time()))
            return {"result": outcome}

    def daily_project_date(self):
        if not self.standalone:
            return None
        return project_daily_date(self.watchdog.AUTHORITY, datetime.now(timezone.utc))

    def daily_project_due(self):
        day = self.daily_project_date()
        if not day:
            return None
        with self.locked() as data:
            lease = self.fence.status()
            if (not lease or not self.matches(lease, lease.epoch) or
                    any(receipt["date"] == day for receipt in data.get("projectDaily", []))):
                return None
            return {"date": day, "epoch": lease.epoch}

    def save_daily_project(self, req):
        day, epoch = req["date"], check_epoch(req["epoch"])
        if not isinstance(day, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
            raise ValueError("The daily snapshot date is invalid")
        projects, recommendation = validate_project_snapshot(req["snapshot"])
        with self.locked(write=True) as data, self.fence._locked():
            if day != self.daily_project_date() or not self.matches(self.fence._read_unlocked(), epoch):
                return {"result": "stale"}
            history = data.get("projectDaily", [])
            if any(receipt["date"] == day for receipt in history):
                return {"result": "duplicate"}
            receipt = {"date": day, "epoch": epoch, "observed": int(time.time()),
                       "projects": projects, "recommended": recommendation}
            data["projectDaily"] = [*history[-13:], receipt]
            return {"result": "saved"}

    def record_project_recommendation(self, req):
        if (not isinstance(req, dict) or req.keys() - {"repo", "issue", "action", "head"} or
                not isinstance(req.get("repo"), str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}", req["repo"]) or
                type(req.get("issue")) is not int or not 1 <= req["issue"] <= 99_999_999 or
                req.get("action") not in ("investigate", "fix", "verify") or
                ("head" in req) != (req["action"] in ("fix", "verify")) or
                ("head" in req and (not isinstance(req["head"], str) or not SHA.fullmatch(req["head"])))):
            raise ValueError("Invalid project recommendation")
        with self.locked(write=True) as data:
            lease = self.fence.status()
            if not lease or not self.matches(lease, lease.epoch):
                raise ValueError("The Captain lease is unavailable")
            entry = {**req, "repo": req["repo"].lower(), "epoch": lease.epoch}
            history = data.get("projectRecommendations", [])
            if history and all(history[-1].get(key) == value for key, value in entry.items()) and (
                    history[-1].get("head") == entry.get("head")):
                return {"recorded": False, "duplicate": True}
            data["projectRecommendations"] = [*history[-49:], {**entry, "observed": int(time.time())}]
            return {"recorded": True}

    def submit_project(self, req):
        """Submit one operator-approved coding identity under the current authority fence."""
        if not self.standalone or not isinstance(req, dict) or req.keys() != {"repository", "issue", "head", "epoch"}:
            raise ValueError("Expected a standalone exact project request")
        if self.watchdog.node != "nucbox":
            return {"result": "runner-unconfigured"}
        request = {**req, "lane": self.queue.queue.project_lane(**req)}
        self.queue.queue.canonical(request)
        with self.locked(write=True) as data, self.fence._locked(), ExitStack() as resources:
            lease = self.fence._read_unlocked()
            if not self.matches(lease, req["epoch"]):
                return {"result": "stale"}
            jobs = data.get("projectJobs", [])
            existing = next((job for job in jobs if job["request"] == request), None)
            if existing:
                return {"result": "duplicate", "job": existing}
            if any(job["request"]["repository"] == req["repository"] and job["state"] in ACTIVE_PROJECT for job in jobs):
                return {"result": "active"}
            if len(jobs) >= 100:
                return {"result": "ledger-full"}

            try:
                grant = read_private(self.watchdog.AUTHORITY / "project-grant.json", 4096)
                config = self.queue.project_config()
            except (OSError, TypeError, ValueError):
                return {"result": "runner-unconfigured"}
            now = int(time.time())
            if (not isinstance(grant, dict) or grant.keys() != req.keys() | {"branch", "expires", "planSha256", "worker"} or
                    any(type(grant[key]) is not type(value) or grant[key] != value for key, value in req.items()) or
                    type(grant["expires"]) is not int or not now < grant["expires"] <= now + 3600 or
                    grant["worker"] != "local-qwen" or not isinstance(grant["branch"], str) or
                    not re.fullmatch(r"[a-zA-Z0-9_-]+(?:[./-][a-zA-Z0-9_-]+)*", grant["branch"]) or
                    len(grant["branch"]) > 150):
                return {"result": "scope-denied"}
            try:
                plan_bytes = private_bytes(self.watchdog.AUTHORITY / "project-worker.json", 8192)
                plan = json.loads(plan_bytes)
            except (OSError, ValueError):
                return {"result": "runner-unconfigured"}
            if not isinstance(plan, dict) or plan.get("request") != req:
                return {"result": "runner-unconfigured"}
            if digest(plan_bytes) != grant["planSha256"]:
                return {"result": "scope-denied"}

            try:
                gate = resources.enter_context(self.gate.open())
                capacity = local_capacity(self.watchdog)
            except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
                return {"result": "admission-unavailable"}
            if gate["hold"] or gate["running"] or capacity["used"] >= capacity["limit"] or os.getloadavg()[0] > 16:
                return {"result": "admission-blocked"}
            if self.queue.project_receipt(request) is not None:
                return {"result": "existing-queue-receipt"}
            try:
                if not current_project_evidence(request, grant["branch"]):
                    return {"result": "stale-github"}
            except (OSError, TypeError, ValueError, subprocess.SubprocessError):
                return {"result": "github-unavailable"}
            if int(time.time()) >= grant["expires"]:
                return {"result": "scope-denied"}

            job = {"request": request, "worker": grant["worker"], "branch": grant["branch"],
                   "planSha256": grant["planSha256"], "state": "reserved", "started": now}
            data["projectJobs"] = [*jobs, job]
            save(self.file, data)
            try:
                receipt = self.queue.queue.dispatch(config, request, self.queue.project_ledger, timeout=3)
            except (OSError, ValueError):
                receipt = self.queue.project_receipt(request)
            phase = receipt["phase"] if receipt else "unseen"
            job.update(queuePhase=phase, state={"queued": "running", "refused": "refused"}.get(phase, "unresolved"))
            return {"result": job["state"], "job": job}

    def dispatch_project(self, req):
        if not isinstance(req, dict) or req.keys() != {"repository", "issue"}:
            raise ValueError("Expected a repository and issue")
        try:
            grant = read_private(self.watchdog.AUTHORITY / "project-grant.json", 4096)
        except (OSError, ValueError):
            return {"result": "scope-denied"}
        if (not isinstance(grant, dict) or not {"head", "epoch"} <= grant.keys() or
                any(type(grant.get(key)) is not type(value) or grant[key] != value for key, value in req.items())):
            return {"result": "scope-denied"}
        return self.submit_project({**req, "head": grant["head"], "epoch": grant["epoch"]})

    def reconcile_project(self, req):
        if not self.standalone or not isinstance(req, dict) or req.keys() != {"repository", "issue", "head", "epoch", "run"}:
            raise ValueError("Expected an exact project request and its original Hatchet run ID")
        request = {key: value for key, value in req.items() if key != "run"}
        request["lane"] = self.queue.queue.project_lane(**request)
        self.queue.queue.canonical(request)
        with self.locked(write=True) as data, self.fence._locked():
            if not self.matches(self.fence._read_unlocked(), request["epoch"]):
                return {"result": "stale"}
            job = next((row for row in data.get("projectJobs", []) if row["request"] == request), None)
            if job is None or job["state"] not in ("reserved", "unresolved", "running"):
                return {"result": "no-active-job"}
            receipt = self.queue.queue.reconcile_project(self.queue.project_config(), request,
                                                         self.queue.project_ledger, req["run"], timeout=3)
            job.update(queuePhase=receipt["phase"], state="running")
            return {"result": job["state"], "job": job}

    def inspect_project(self, req):
        if not self.standalone or not isinstance(req, dict) or req.keys() != {"repository", "issue", "head", "epoch"}:
            raise ValueError("Expected the exact project identity")
        request = {**req, "lane": self.queue.queue.project_lane(**req)}
        self.queue.queue.canonical(request)
        with self.locked(write=True) as data, self.fence._locked():
            if not self.matches(self.fence._read_unlocked(), req["epoch"]):
                return {"result": "stale"}
            job = next((row for row in data.get("projectJobs", []) if row["request"] == request), None)
            if job is None:
                return {"result": "job-unavailable"}
            if job["state"] not in (*ACTIVE_PROJECT, "verified", "ci-failed"):
                return {"result": job["state"], "job": job}
            job["observed"] = int(time.time())
            try:
                receipt = self.queue.project_receipt(request)
                if not receipt or receipt["phase"] != "queued":
                    if receipt and receipt["phase"] == "refused":
                        job.update(state="refused", queuePhase="refused")
                    return {"result": job["state"], "job": job}
                row = self.queue.queue.read_project_run(self.queue.project_config(), request, receipt, receipt["run"], 3)
                if row.get("status") in ("FAILED", "CANCELLED"):
                    job.update(state="worker-failed")
                elif row.get("status") == "COMPLETED":
                    self.review_project_candidate(job, row.get("output"))
                elif row.get("status") in ("QUEUED", "RUNNING"):
                    job.update(state="running")
                else:
                    return {"result": "worker-evidence-unavailable"}
            except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
                if job["state"] in ("verified", "ci-failed"):
                    job.update(state="ci-pending")
                return {"result": "verification-unavailable"}
            return {"result": job["state"], "job": job}

    def review_project_candidate(self, job, output):
        request = job["request"]
        commit = self.queue.queue.project_artifact(request, output, previous=job.get("commit"))
        if commit is None:
            job.update(state="invalid-artifact")
            return
        job.update(commit=commit, state="patch-ready")
        if not current_project_evidence(request, job["branch"]):
            job.update(state="stale-base")
            return
        verification = project_verification(self.watchdog.AUTHORITY, request, commit)
        if verification is None:
            return
        outcome = project_pr_checks(request, job["branch"], commit, verification)
        job.update(state=outcome, pr=verification["pr"], checks=verification["checks"], checked=int(time.time()))

    @staticmethod
    def submitted_message(data, owner, message):
        return any(row["owner"] == owner and row.get("message") == message for row in data["jobs"])

    def block(self, req):
        if req["reason"] not in BLOCKERS or type(req["pr"]) is not int or not 1 <= req["pr"] <= 99_999_999:
            raise ValueError("Invalid captain blocker")
        with self.locked(write=True) as data:
            grant = data["grant"]
            if self.submitted_message(data, req["owner"], req["message"]):
                return {"recorded": False}
            if (self.matches(self.fence.status(), req["epoch"]) and grant and grant["epoch"] == req["epoch"]
                    and grant["owner"] == req["owner"] and not grant["paused"]
                    and type(req["message"]) is int and req["message"] > grant["message"]):
                blocked = {"pr": req["pr"], "epoch": req["epoch"], "reason": req["reason"],
                           "observed": int(time.time()), "message": req["message"], "owner": req["owner"]}
                previous = data.get("blocked", {})
                if previous.get("message", 0) > req["message"]:
                    return {"recorded": False}
                if req["reason"] == "ci-pending":
                    if not isinstance(req.get("head"), str) or not SHA.fullmatch(req["head"]):
                        raise ValueError("Missing pending CI head")
                    start = previous.get("started") if previous.get("message") == req["message"] else None
                    blocked.update(head=req["head"], started=start or blocked["observed"])
                data["blocked"] = blocked
                return {"recorded": True}
        return {"recorded": False}

    def control(self, action, req):
        epoch, message, owner = req["epoch"], req["message"], req["owner"]
        if type(epoch) is not int or epoch < 1 or type(message) is not int or message < 1 or not OWNER.fullmatch(owner):
            raise ValueError("Invalid epoch or owner receipt")
        # Hold both locks in the same order used for submission. Verify and
        # request handback under the authority lock so the epoch cannot change.
        if self.standalone and action == "handback":
            return {"result": "operator-transfer"}
        with self.locked(write=True) as data, self.fence._locked():
            record = self.fence._read_unlocked()
            if not self.matches(record, epoch) or message <= data["lastControl"]:
                return {"result": "stale"}
            grant = data["grant"]
            if action == "grant":
                data["grant"] = {"epoch": epoch, "scope": SCOPE, "paused": False, "message": message, "owner": owner}
            elif action == "pause":
                if not grant or grant["epoch"] != epoch:
                    return {"result": "no-grant"}
                grant["paused"] = True
            elif action == "handback":
                record.state = "handback-pending"
                record.handback_requested_at = time.time()
                self.fence._write_unlocked(record)
                if grant and grant["epoch"] == epoch:
                    grant["paused"] = True
            if data.get("blocked", {}).get("reason") == "ci-pending":
                data["blocked"]["reason"] = "ci-paused"
            data["lastControl"] = message
        return {"result": action}

    def submit(self, req):
        epoch, message, pr, head, ci = (req[name] for name in ("epoch", "message", "pr", "head", "ci"))
        if (type(epoch) is not int or type(message) is not int or type(pr) is not int or not 1 <= pr <= 99_999_999
                or not SHA.fullmatch(head) or ci not in ("passing", "failing")):
            raise ValueError("Invalid lane plan")
        lane = "fo-ci-" + digest(f"{pr}:{head}".encode())[:24]
        with self.locked(write=True) as data, ExitStack() as resources:
            record = self.fence.status()
            grant = data["grant"]
            if (not self.matches(record, epoch) or not grant or grant["epoch"] != epoch or grant["paused"]
                    or grant["owner"] != req["owner"]):
                return {"result": "stale"}
            if message <= grant["message"]:
                return {"result": "stale"}
            if req.get("resumePending"):
                pending = data.get("blocked", {})
                if (pending.get("message"), pending.get("head"), pending.get("reason")) != (message, head, "ci-pending"):
                    return {"result": "stale"}
            if self.submitted_message(data, req["owner"], message) or any(row["lane"] == lane for row in data["jobs"]):
                return {"result": "duplicate"}
            if any(row["state"] in ("launching", "running") for row in data["jobs"]):
                return {"result": "active"}

            if self.queue is not None:
                try:
                    queue_config = self.queue.config()
                except (OSError, ValueError):
                    return {"result": "runner-unconfigured"}
                try:
                    gate = resources.enter_context(self.gate.open())
                except (OSError, KeyError, TypeError, ValueError):
                    return {"result": "gate-unavailable"}
                if gate["hold"] or gate["running"]:
                    return {"result": "gate-blocked"}
                try:
                    capacity = local_capacity(self.watchdog)
                except (OSError, TypeError, ValueError, subprocess.SubprocessError):
                    return {"result": "capacity-unavailable"}
                if capacity["used"] >= capacity["limit"] or os.getloadavg()[0] > 16:
                    return {"result": "capacity-blocked"}

            job = {"lane": lane, "pr": pr, "head": head, "ci": ci, "epoch": epoch,
                   "state": "launching", "started": int(time.time()), "delivery": None,
                   "owner": req["owner"], "message": message}
            data["jobs"].append(job)
            if data.get("blocked", {}).get("pr") == pr:
                del data["blocked"]
            # Persist the reservation while holding the scope lock. A crash or
            # uncertain launch never causes a second spawn for this head.
            save(self.file, data)
            if self.queue is not None:
                receipt = self.queue.start(job, queue_config)
                job["queuePhase"] = receipt["phase"] if receipt else "unseen"
                if receipt:
                    job["backend"] = receipt["backend"]
                    job["acceptedAt"] = receipt["at"]
                    if receipt["phase"] == "queued":
                        job["state"] = "running"
                return {"result": job["state"], "job": job}
            root = LANES / "first-officer-captain-prompts"
            root.mkdir(mode=0o700, exist_ok=True)
            info = root.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Captain prompt directory is not private")
            prompt = (
                f"Review the exact CI evidence for rupret007/Cursor-OpenClaw-Integration PR #{pr}.\n"
                f"GitHub bound the required workflows to commit {head} and reports them {ci}.\n"
                "Write a concise CI review in REVIEW.md with the next action this result implies. "
                "Write ci-result.json with exactly the fields head (the full commit) and ci (passing or failing). "
                "Set permissions on ci-result.json to mode 0600. "
                "Stay in this workspace. Use only the supplied receipt. Leave external services untouched.\n"
            ).encode()
            # conductor-action allows only relative names below the lane root.
            # Keep the leaf unique and correlate it to the persisted lane ID.
            path = root / (lane + ".md")
            try:
                with path.open("xb") as file:
                    file.write(prompt)
                launch = subprocess.run(
                    ["/usr/bin/nice", "-n", "10", str(RUNNER / "conductor-action"), "--fence", str(epoch),
                     "launch", lane, str(path.relative_to(LANES)), digest(prompt)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
                if launch.returncode == 0:
                    job["state"] = "running"
                else:
                    # The process receipt settles ambiguous runner failures.
                    job["launchRejected"] = True
            except (OSError, subprocess.SubprocessError):
                job["launchRejected"] = True
            finally:
                path.unlink(missing_ok=True)
                save(self.file, data)
            return {"result": job["state"], "job": job}

    def poll(self, lane):
        if not LANE.fullmatch(lane):
            raise ValueError("Invalid lane")
        if self.queue is not None:
            with self.locked() as data:
                job = next((row for row in data["jobs"] if row["lane"] == lane), None)
            return self.queue.poll(job) if job else {"worker": "unresolved"}
        directory = LANES / "first-officer-codex" / lane
        if directory.resolve() != directory or (directory / "workspace").resolve() != directory / "workspace":
            return {"worker": "invalid-result"}
        try:
            receipt = read_private(directory / "process.json", 4096)
        except FileNotFoundError:
            return {"worker": "no-launch-receipt"}
        pid, start = receipt["pid"], receipt["start"]
        if type(pid) is not int or pid < 2 or type(start) is not int or start < 0:
            raise ValueError("Invalid process receipt")
        try:
            state, *fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
            if state != "Z" and int(fields[18]) == start:
                return {"worker": "running"}
        except FileNotFoundError:
            pass
        try:
            result = read_private(directory / "workspace/ci-result.json", 1024)
        except (OSError, ValueError):
            return {"worker": "missing-result"}
        if (not isinstance(result, dict) or result.keys() != {"head", "ci"}
                or not isinstance(result["head"], str) or not SHA.fullmatch(result["head"])
                or result["ci"] not in ("passing", "failing")):
            return {"worker": "invalid-result"}
        return {"worker": "settled", **result}

    def finish(self, req):
        reasons = ("verified", "ci-failed", "head-changed", "worker-failed", "worker-timeout", "ci-unavailable")
        if req["state"] not in reasons:
            raise ValueError("Invalid terminal state")
        with self.locked(write=True) as data:
            for job in data["jobs"]:
                if job["lane"] == req["lane"] and job["state"] in ("running", "launching"):
                    job["state"] = req["state"]
                    job["delivery"] = "delivery-unverified"
                    return {"job": job}
        return {}

    def reported(self, lane):
        with self.locked(write=True) as data:
            for job in data["jobs"]:
                if job["lane"] == lane and job["delivery"] == DELIVERY[1]:
                    job["delivery"] = DELIVERY[0]
                    return {}
        return {}

    def overtime(self, lane):
        with self.locked(write=True) as data:
            for job in data["jobs"]:
                if job["lane"] == lane and job["state"] in ("launching", "running") and not job.get("overtime"):
                    job["overtime"] = True
                    return {"job": job}
        return {}

    def settle_handback(self):
        if self.standalone or not self.watchdog.enabled():
            return {"result": "disarmed"}
        result, _ = self.fence.complete_handback()
        return {"result": result}


def quota_reader():
    spec = importlib.util.spec_from_file_location("runner_quota", Path(__file__).with_name("runner-quota.py"))
    quota = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(quota)
    return quota


def main():
    sys.dont_write_bytecode = True
    os.umask(0o077)
    entry = sys.argv[1]
    node = os.environ.get("FO_LOCAL_NODE", "bobserver")
    if node not in (*LOCAL_NODES, "bobserver"):
        raise ValueError("Unknown Captain host")
    if entry == "local-run":
        if len(sys.argv) not in (3, 4):
            raise ValueError("Expected an OpenClaw executable and optional private environment")
        authority = StandaloneAuthority(node, LOCAL_AUTHORITY)
        environment = gateway_environment(Path(sys.argv[3])) if len(sys.argv) == 4 else None
        try:
            raise SystemExit(authority.run_gateway(Path(sys.argv[2]), environment))
        except StandaloneParked:
            print("Standalone Captain is parked.")
            raise SystemExit(0) from None
    limit = TRANSFER_LIMIT if entry == "local-import" else 4096
    raw = sys.stdin.buffer.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Captain request exceeded its limit")
    req = json.loads(raw)
    broker_epoch = os.environ.get("FO_STANDBY_EPOCH")
    if broker_epoch is not None:
        epoch = check_epoch(int(broker_epoch))
        if epoch == 0 or node not in LOCAL_NODES:
            raise ValueError("Invalid brokered gateway epoch")
        if entry in ("view", "handoff"):
            view = {"lease": {"holder": "first-officer", "state": "held", "epoch": epoch},
                    "armed": False, "grant": None, "blocked": None, "standalone": node,
                    "jobs": [], "active": None}
            if entry == "handoff":
                view["observed"] = int(time.time())
            return view
        if entry == "verify-brokered-project":
            return brokered_project_ci(LOCAL_AUTHORITY, req)
        if entry not in ("local-gate", "local-capacity", "local-quota", "local-projects",
                         "project-telegram-review", "record-project-recommendation"):
            raise ValueError("Brokered job state is unavailable in this gateway configuration")
    if node in LOCAL_NODES:
        authority = StandaloneAuthority(node, LOCAL_AUTHORITY)
        if entry == "local-initialize":
            return authority.initialize()
        controller = Controller(authority, private=LOCAL_AUTHORITY / "reviews")
        if entry == "local-release":
            return authority.release(req["epoch"], controller.locked)
        if entry == "local-import":
            return authority.accept(req, controller.file, controller.locked)
        if entry == "local-gate":
            return controller.gate.read()
        if entry == "local-capacity":
            return local_capacity(authority)
        if entry == "record-project-recommendation":
            return controller.record_project_recommendation(req)
        if entry == "submit-project":
            return controller.submit_project(req)
        if entry == "dispatch-project":
            return controller.dispatch_project(req)
        if entry == "reconcile-project":
            return controller.reconcile_project(req)
        if entry == "inspect-project":
            return controller.inspect_project(req)
        if entry in ("reserve-project-telegram", "finish-project-telegram", "resolve-project-telegram"):
            return controller.project_telegram(entry.split("-", 1)[0], req)
        if entry == "project-telegram-review":
            return controller.project_telegram_review()
        if entry == "project-daily-due":
            return controller.daily_project_due()
        if entry == "save-project-daily":
            return controller.save_daily_project(req)
        if entry == "local-projects":
            if not private_directory(LOCAL_AUTHORITY):
                raise ValueError("The standalone authority directory is invalid")
            try:
                return read_private(LOCAL_AUTHORITY / "projects.json", 8192)
            except FileNotFoundError:
                return None
        if entry == "local-quota":
            profile = read_private(LOCAL_AUTHORITY / "quota.json", 4096)
            if not isinstance(profile, dict) or profile.keys() != {"codex"} or not isinstance(profile["codex"], str):
                raise ValueError("The standalone quota profile is invalid")
            return quota_reader().read_local_quota(profile["codex"])
        if entry == "runner-quota":
            controller.queue.config()
            return {"allowed": True, "modelFree": True}
    else:
        sys.path.insert(0, str(RUNNER))
        import conductor_watchdog

        if entry == "runner-quota":
            return quota_reader().read_runner_quota(RUNNER / "conductor-action")
        controller = Controller(conductor_watchdog)
    actions = {"view": controller.view, "submit": lambda: controller.submit(req),
               "handoff": controller.handoff, "block": lambda: controller.block(req),
               "settle-handback": controller.settle_handback,
               "poll": lambda: controller.poll(req["lane"]), "finish": lambda: controller.finish(req),
               "reported": lambda: controller.reported(req["lane"]), "overtime": lambda: controller.overtime(req["lane"])}
    if entry in ("grant", "pause", "handback"):
        return controller.control(entry, req)
    return actions[entry]()


if __name__ == "__main__":
    try:
        print(json.dumps(main()))
    except Exception:
        print('{"error":"Captain state or authority unavailable"}')
        sys.exit(1)

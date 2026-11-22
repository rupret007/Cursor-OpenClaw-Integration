"""Keep the Telegram captain grant and lane receipts next to the existing fence."""

from contextlib import ExitStack, contextmanager
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
from urllib.request import Request


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
            child = subprocess.Popen([str(executable), "gateway", "run"], pass_fds=(lease_fd,), env=environment)

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

    def config(self):
        config = self.queue.validate_config(read_private(self.policy, 4096))
        if (config["hatchet"]["workflow"], config["restate"]["service"], config["restate"]["handler"]) != (
                "fo-captain-review", "CaptainReview", "verify"):
            raise ValueError("Expected the reviewed, model-free Captain worker routes")
        return config

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
        receipt = self.receipt(job)
        if receipt is None:
            return {"worker": "unresolved"}
        backend, phase = receipt["backend"], receipt["phase"]
        metadata = {"backend": backend, "accepted": phase == "queued",
                    "age": max(0, int(time.time()) - receipt["at"])}
        if phase != "queued":
            result = "missing-result" if phase == "refused" and backend == "restate" else "unresolved"
            return {"worker": result, **metadata}

        try:
            route = self.config()[backend]
            args, target = self.queue.submission(backend, route, self.request(job), receipt["key"])
            if target != receipt["target"]:
                return {"worker": "unresolved", **metadata}
            opener, _, headers, _ = args
            base = route["base_url"].rstrip("/")
            run = receipt["run"]
            if backend == "hatchet":
                status_url = f"{base}/api/v1/stable/workflow-runs/{run}"
                status = self.queue.response_json(opener, Request(
                    status_url + "/status", headers=headers), 1, str)
                if status in ("QUEUED", "RUNNING"):
                    return {"worker": "running", **metadata}
                completed = status == "COMPLETED"
                failed = status in ("FAILED", "CANCELLED")
            else:
                headers = {"Authorization": headers["Authorization"]}
                status_url = f"{base}/restate/output/{run}"
                status = self.queue.response_json(opener, Request(
                    f"{base}/restate/status/{run}", headers=headers), 1)
                if status.get("stage") in ("created", "started"):
                    return {"worker": "running", **metadata}
                failed = status.get("stage") == "completed" and "error" in status
                completed = status.get("stage") == "completed" and not failed
            if not completed:
                return {"worker": "missing-result" if failed else "unresolved", **metadata}
            result = self.queue.response_json(opener, Request(status_url, headers=headers), 1)
        except (OSError, ValueError, self.queue.Refusal, self.queue.Uncertain):
            return {"worker": "unresolved", **metadata}
        if backend == "hatchet":
            run_details = result.get("run")
            run_metadata = run_details.get("metadata") if isinstance(run_details, dict) else None
            if not isinstance(run_metadata, dict) or run_metadata.get("id") != run:
                return {"worker": "invalid-result", **metadata}
            result = run_details.get("output")
        if not isinstance(result, dict) or result.keys() != {"lane", "pr", "head", "ci"}:
            return {"worker": "invalid-result", **metadata}
        if any(result[key] != job[key] for key in ("lane", "pr", "head")) or result["ci"] not in ("passing", "failing"):
            return {"worker": "invalid-result", **metadata}
        return {"worker": "settled", "head": result["head"], "ci": result["ci"], **metadata}


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
        return {"lease": {"holder": record.holder, "state": record.state, "epoch": record.epoch},
                "armed": self.watchdog.enabled() and (not self.standalone or record.state == "held"),
                "grant": data["grant"], "blocked": data.get("blocked"),
                **({"standalone": self.watchdog.node} if self.standalone else {}),
                "jobs": data["jobs"][-5:],
                "active": active}

    def handoff(self):
        with self.locked(write=True) as data:
            view = self.view_locked(data)
            # These are already structured, local receipts. No Telegram body
            # or model output becomes part of the handoff.
            view["recorded"] = int(time.time())
            save(self.private / "handoff.json", view)
            return view

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

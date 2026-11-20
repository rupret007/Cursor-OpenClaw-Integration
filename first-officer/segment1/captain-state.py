"""Keep the Telegram captain grant and lane receipts next to the existing fence."""

from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time


HOME = Path.home()
STATE = HOME / ".local/state/openclaw-conductor"
LANES = HOME / "work/lanes"
RUNNER = LANES / "openclaw-backup2-20261118-1340"
PRIVATE = STATE / "telegram-captain"
SCOPE = "integration-ci"
SHA = re.compile(r"[a-f0-9]{40}")
LANE = re.compile(r"fo-ci-[a-f0-9]{24}")
OWNER = re.compile(r"[1-9][0-9]{0,15}")
DELIVERY = ("reported", "delivery-unverified")
BLOCKERS = ("fleet", "gate", "quota", "ci-changed", "ci-unavailable", "ci-pending", "ci-timeout", "ci-paused")


def read_private(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Invalid captain receipt")
        data = file.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Captain receipt exceeded its limit")
        return json.loads(data)


def save(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temp = Path(file.name)
        try:
            file.write((json.dumps(data, sort_keys=True) + "\n").encode())
            file.flush()
            os.fsync(file.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Controller:
    def __init__(self, watchdog):
        self.watchdog = watchdog
        if not watchdog.private_authority(watchdog.AUTHORITY):
            raise ValueError("Authority unavailable")
        self.fence = watchdog.load_fence()(str(watchdog.AUTHORITY / "lease.json"))
        PRIVATE.mkdir(mode=0o700, exist_ok=True)
        info = PRIVATE.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Captain state is not private")
        self.file = PRIVATE / "state.json"

    @contextmanager
    def locked(self, write=False):
        with os.fdopen(os.open(PRIVATE / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "rb") as lock:
            fcntl.flock(lock, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            try:
                data = read_private(self.file, 256 * 1024)
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
        return {"lease": {"holder": record.holder, "state": record.state, "epoch": record.epoch},
                "armed": self.watchdog.enabled(), "grant": data["grant"], "blocked": data.get("blocked"),
                "jobs": data["jobs"][-5:],
                "active": next((job for job in data["jobs"] if job["state"] in ("launching", "running")), None)}

    def handoff(self):
        with self.locked(write=True) as data:
            view = self.view_locked(data)
            # These are already structured, local receipts. No Telegram body
            # or model output becomes part of the handoff.
            view["recorded"] = int(time.time())
            save(PRIVATE / "handoff.json", view)
            return view

    def block(self, req):
        if req["reason"] not in BLOCKERS or type(req["pr"]) is not int or not 1 <= req["pr"] <= 99_999_999:
            raise ValueError("Invalid captain blocker")
        with self.locked(write=True) as data:
            grant = data["grant"]
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
        with self.locked(write=True) as data:
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
            if any(row["lane"] == lane for row in data["jobs"]):
                return {"result": "duplicate"}
            if any(row["state"] in ("launching", "running") for row in data["jobs"]):
                return {"result": "active"}

            job = {"lane": lane, "pr": pr, "head": head, "ci": ci, "epoch": epoch,
                   "state": "launching", "started": int(time.time()), "delivery": None, "owner": req["owner"]}
            data["jobs"].append(job)
            if data.get("blocked", {}).get("pr") == pr:
                del data["blocked"]
            # Persist the reservation while holding the scope lock. A crash or
            # uncertain launch never causes a second spawn for this head.
            save(self.file, data)
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
        if not self.watchdog.enabled():
            return {"result": "disarmed"}
        result, _ = self.fence.complete_handback()
        return {"result": result}


def main():
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(RUNNER))
    import conductor_watchdog

    os.umask(0o077)
    entry = sys.argv[1]
    req = json.loads(sys.stdin.buffer.read(4096))
    if entry == "runner-quota":
        spec = importlib.util.spec_from_file_location("runner_quota", Path(__file__).with_name("runner-quota.py"))
        quota = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(quota)
        return quota.read_runner_quota(RUNNER / "conductor-action")
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

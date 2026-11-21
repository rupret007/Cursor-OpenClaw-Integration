#!/usr/bin/env python3
"""Rehearse the supervised authority pilot on a staged lease.

The live fence, the watchdog service and the gateway are not opened. A missed
check-in returns the staged lease to captain, advances the epoch and rejects the
old grant. The checklist at the end is the live sequence, and this command does
not run it.
"""

import argparse
from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import sys


FRESH_S = 300
SCOPE = "integration-ci"
HEAD = "a" * 40
CHECKLIST = (
    "1. Leave the live lease with captain until this staged rehearsal has passed.",
    "2. Run python3 first-officer/pilot/rehearse.py rehearse --root /tmp/fo-pilot --epoch 12 --now 1763700000.",
    "3. On bobserver, read captain status. Stop if the holder is not captain.",
    "4. Require a fresh captain check-in and an explicit handoff at that epoch.",
    "5. Arm that epoch and run one watchdog tick. Stop unless first-officer holds the next epoch.",
    "6. Grant integration-ci at the new epoch, submit one pull request, and confirm a repeat is one lane.",
    "7. Pause that epoch and confirm a new task creates no lane.",
    "8. A missed 300 second check-in must return the lease to captain at a newer epoch and reject the old epoch.",
    "9. Hand back at the current epoch, confirm captain holds a newer epoch, and disarm there.",
    "10. Send no message from this checklist. Telegram checks stay on the live-test list.",
)


def _clock(now):
    if type(now) is not int or not 0 <= now < 1 << 42:
        raise ValueError("The pilot clock is unusable")
    return now


def _epoch(epoch):
    if type(epoch) is not int or not 0 < epoch < 1_000_000:
        raise ValueError("The pilot epoch is unusable")
    return epoch


def refuse_live(path):
    """Reject the installed fence, the runner tree and the gateway release store."""
    resolved = Path(path).resolve()
    blocked = (
        Path.home() / "work/lanes/openclaw-backup2-20261118-1340",
        Path.home() / ".local/state/openclaw-conductor",
        Path("/opt/openclaw-fo"),
    )
    for marker in blocked:
        if resolved == marker or marker in resolved.parents:
            raise ValueError("The pilot rehearsal must stay on a staged copy")


def _root(path):
    refuse_live(path)
    root = Path(path)
    if root.is_symlink():
        raise ValueError("The pilot root must be a private directory")
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    info = root.lstat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("The pilot root must be a private directory")
    return root


def _check(info):
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("The staged lease must be a private owner file")


@contextmanager
def _locked(path):
    try:
        handle = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("The staged lease must be a private owner file") from None
        raise
    file = os.fdopen(handle, "r+", encoding="utf-8")
    try:
        _check(os.fstat(file.fileno()))
        fcntl.flock(file, fcntl.LOCK_EX)
        yield file
    finally:
        file.close()


def _canonical(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def _valid(record):
    keys = {"epoch", "grant", "handoff", "holder", "lane", "reason", "renewed_at", "state", "takeover"}
    if not isinstance(record, dict) or set(record) != keys:
        raise ValueError("The staged lease is unreadable")
    if record["holder"] not in {"captain", "first-officer"} or record["takeover"] not in {"OFF", "ON"}:
        raise ValueError("The staged lease is unreadable")
    if record["state"] not in {"held", "handback-pending"} or type(record["handoff"]) is not bool:
        raise ValueError("The staged lease is unreadable")
    _epoch(record["epoch"])
    _clock(record["renewed_at"])
    if record["grant"] is not None:
        grant = record["grant"]
        if set(grant) != {"epoch", "paused", "scope"} or grant["scope"] != SCOPE or type(grant["paused"]) is not bool:
            raise ValueError("The staged lease is unreadable")
        _epoch(grant["epoch"])
    if record["lane"] is not None:
        lane = record["lane"]
        if set(lane) != {"head", "pr", "state"} or lane["head"] != HEAD or lane["pr"] != 33:
            raise ValueError("The staged lease is unreadable")
        if lane["state"] not in {"running", "interrupted"}:
            raise ValueError("The staged lease is unreadable")
    return record


def _fresh(record, now):
    return 0 <= now - record["renewed_at"] <= FRESH_S


def _store(file, record):
    file.seek(0)
    file.write(_canonical(record) + "\n")
    file.truncate()
    file.flush()
    os.fsync(file.fileno())


def _update(root, change):
    refuse_live(root)
    with _locked(root / "lease.json") as file:
        record = _valid(json.loads(file.read()))
        result = change(record)
        _store(file, record)
        return result


def begin(root, epoch, now):
    """Create a captain-held staged lease. An existing lease is left untouched."""
    epoch, now = _epoch(epoch), _clock(now)
    root = _root(root)
    target = root / "lease.json"
    record = {
        "epoch": epoch, "grant": None, "handoff": True, "holder": "captain", "lane": None,
        "reason": "", "renewed_at": now, "state": "held", "takeover": "OFF",
    }
    try:
        handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise ValueError("The staged lease already exists") from None
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        file.write(_canonical(record) + "\n")
        file.flush()
        os.fsync(file.fileno())
    return record


def arm(root, epoch, now):
    """Turn takeover on only for a fresh explicit handoff at the captain epoch."""
    epoch, now = _epoch(epoch), _clock(now)

    def change(record):
        if (record["holder"], record["epoch"], record["state"], record["takeover"]) != ("captain", epoch, "held", "OFF"):
            raise ValueError("Captain does not hold the expected epoch")
        if not record["handoff"] or not _fresh(record, now) or record["lane"] is not None:
            raise ValueError("A fresh explicit handoff with no running lane is required")
        record["takeover"] = "ON"
        record["reason"] = "armed"
        return "ON"

    return _update(root, change)


def tick(root, now):
    """Advance one staged watchdog tick. A stale pilot holder fails back to captain."""
    now = _clock(now)

    def change(record):
        if (record["takeover"] == "ON" and record["holder"] == "captain" and record["state"] == "held"
                and record["handoff"] and record["lane"] is None and _fresh(record, now)):
            record.update(epoch=record["epoch"] + 1, holder="first-officer", reason="handoff", renewed_at=now)
            return {"epoch": record["epoch"], "holder": "first-officer", "result": "handoff"}
        missed = record["holder"] == "first-officer" and record["state"] == "held" and not _fresh(record, now)
        returning = record["state"] == "handback-pending"
        if missed or returning:
            if missed and record["lane"] and record["lane"]["state"] == "running":
                record["lane"]["state"] = "interrupted"
            record.update(
                epoch=record["epoch"] + 1, grant=None, holder="captain", reason="missed-check-in" if missed else "handback",
                renewed_at=now, state="held", takeover="OFF",
            )
            return {"epoch": record["epoch"], "holder": "captain", "result": record["reason"]}
        return {"epoch": record["epoch"], "holder": record["holder"], "result": "unchanged"}

    return _update(root, change)


def grant(root, epoch, now):
    epoch, now = _epoch(epoch), _clock(now)

    def change(record):
        if (record["holder"], record["epoch"], record["state"], record["takeover"]) != ("first-officer", epoch, "held", "ON"):
            return "stale"
        if not _fresh(record, now):
            return "stale"
        record["grant"] = {"epoch": epoch, "paused": False, "scope": SCOPE}
        record["reason"] = "grant"
        return "grant"

    return _update(root, change)


def pause(root, epoch, now):
    epoch, now = _epoch(epoch), _clock(now)

    def change(record):
        grant_record = record["grant"]
        if grant_record is None or grant_record["epoch"] != epoch or record["epoch"] != epoch or not _fresh(record, now):
            return "stale"
        grant_record["paused"] = True
        record["reason"] = "pause"
        return "pause"

    return _update(root, change)


def handback(root, epoch, now):
    epoch, now = _epoch(epoch), _clock(now)

    def change(record):
        if (record["holder"], record["epoch"], record["state"]) != ("first-officer", epoch, "held") or not _fresh(record, now):
            return "stale"
        record["state"] = "handback-pending"
        record["reason"] = "handback-requested"
        return "handback"

    return _update(root, change)


def submit(root, epoch, now):
    """Accept one staged lane. A second submit, a pause, or an old epoch does nothing."""
    epoch, now = _epoch(epoch), _clock(now)

    def change(record):
        grant_record = record["grant"]
        if (record["holder"] != "first-officer" or record["epoch"] != epoch or record["state"] != "held"
                or grant_record is None or grant_record["epoch"] != epoch or grant_record["paused"]
                or not _fresh(record, now)):
            return "stale"
        if record["lane"] is not None:
            return "duplicate"
        record["lane"] = {"head": HEAD, "pr": 33, "state": "running"}
        record["reason"] = "running"
        return "running"

    return _update(root, change)


def rehearse(root, epoch, now):
    """Run the staged handoff, one lane, a pause, and a missed-check-in failover."""
    started = begin(root, epoch, now)
    armed = arm(root, started["epoch"], now)
    moved = tick(root, now)
    granted = grant(root, moved["epoch"], now)
    first = submit(root, moved["epoch"], now)
    second = submit(root, moved["epoch"], now)
    paused = pause(root, moved["epoch"], now)
    blocked = submit(root, moved["epoch"], now)
    failed = tick(root, now + FRESH_S + 1)
    rejected = submit(root, moved["epoch"], now + FRESH_S + 1)
    lines = [
        f"Armed takeover {armed} while captain held epoch {started['epoch']}.",
        f"Watchdog moved the lease to {moved['holder']} at epoch {moved['epoch']}.",
        f"Granted {SCOPE} at epoch {moved['epoch']}." if granted == "grant" else "Grant was rejected.",
        "One lane accepted for pull 33." if first == "running" else "The lane was rejected.",
        "A second submission was a duplicate." if second == "duplicate" else "A second submission was accepted.",
        "Pause rejected a new lane." if paused == "pause" and blocked == "stale" else "Pause did not hold.",
        f"A missed check-in returned the lease to {failed['holder']} at epoch {failed['epoch']}.",
        "An old-epoch submit was rejected." if rejected == "stale" else "An old-epoch submit was accepted.",
    ]
    return lines


def checklist():
    return "\n".join(CHECKLIST)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("rehearse")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--epoch", type=int, default=12)
    run.add_argument("--now", type=int, required=True)
    commands.add_parser("checklist")
    args = parser.parse_args(argv)
    try:
        if args.command == "checklist":
            print(checklist())
            return 0
        for line in rehearse(args.root, args.epoch, args.now):
            print(line)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        message = str(error) if type(error) is ValueError else "The staged lease could not be written"
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

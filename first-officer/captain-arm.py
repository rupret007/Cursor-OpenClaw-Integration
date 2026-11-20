#!/usr/bin/env python3
"""Arm the existing watchdog only for a fresh explicit pilot handoff."""

import argparse
import os
from pathlib import Path
import sys
import tempfile
import time


RUNNER = Path.home() / "work/lanes/openclaw-backup2-20261118-1340"


def change_arming(watchdog, action, epoch, clock=time.time):
    if not watchdog.trusted(watchdog.AUTHORITY):
        raise ValueError("The installed shared fence could not be verified.")
    fence = watchdog.load_fence()(str(watchdog.AUTHORITY / "lease.json"))
    with fence._locked():
        record = fence._read_unlocked()
        if record is None or (record.holder, record.epoch, record.state) != ("captain", epoch, "held"):
            raise ValueError("Grok Bot does not hold the expected epoch.")
        if watchdog.flag_value() not in ("ON", "OFF"):
            raise ValueError("The existing takeover flag could not be verified.")
        if action == "arm":
            now = clock()
            if not watchdog.timestamp(record.renewed_at) or not 0 <= now - record.renewed_at <= 300:
                raise ValueError("A fresh Grok Bot check-in is required.")
            outcome = watchdog.observe(watchdog.AUTHORITY, record.to_json(), watchdog.HEARTBEAT, watchdog.STATUS, now)
            if outcome != "takeover-handoff":
                raise ValueError("A current explicit Grok Bot handoff with drained dispatches is required.")
        value = "ON" if action == "arm" else "OFF"
        with tempfile.NamedTemporaryFile(dir=watchdog.FLAG.parent, mode="w", delete=False) as handle:
            staging = Path(handle.name)
            try:
                handle.write(value + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                os.replace(staging, watchdog.FLAG)
            finally:
                staging.unlink(missing_ok=True)
    return value


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("action", choices=("arm", "disarm"))
    args.add_argument("epoch", type=int)
    options = args.parse_args()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(RUNNER))
    try:
        import conductor_watchdog as watchdog

        result = change_arming(watchdog, options.action, options.epoch)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ImportError):
        print("The takeover flag operation could not be verified.", file=sys.stderr)
        return 1
    print("Takeover flag is " + result + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

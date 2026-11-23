"""Lease and effect journal for a single, independently hosted Captain authority.

The broker owns this store and calls it from one thread. Its local disk and
upstream credentials stay with the broker. Gateways authenticate to the broker,
which supplies the node identity and owns the outbound effect callbacks.
"""
import argparse
from contextlib import ExitStack, closing
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time


PRIMARY = "nucbox"
STANDBY = "mac-mini"
HEARTBEAT_SECONDS = 15
LEASE_SECONDS = 90
EFFECT_KINDS = frozenset(("telegram-reply", "telegram-typing", "hatchet-submit", "restate-submit"))


class LeaseRejected(ValueError):
    pass


@dataclass(frozen=True)
class Grant:
    node: str
    epoch: int


def open_private(directory, name, *, create=True):
    access = (os.O_RDWR | os.O_CREAT) if create else os.O_RDONLY
    handle = os.fdopen(os.open(directory / name, access | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600), "rb")
    info = os.fstat(handle.fileno())
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        handle.close()
        raise ValueError("Invalid private authority file")
    return handle


def read_private_json(path):
    with open_private(path.parent, path.name, create=False) as handle:
        payload = handle.read(8193)
    if len(payload) > 8192:
        raise ValueError("Private broker configuration is too large")
    return json.loads(payload)


def reserve_effect(db, kind, operation):
    with db:
        inserted = db.execute("INSERT OR IGNORE INTO effects (kind, operation, state) VALUES (?, ?, 'unconfirmed')",
                              (kind, operation)).rowcount
        if not inserted:
            return db.execute("SELECT state FROM effects WHERE kind = ? AND operation = ?",
                              (kind, operation)).fetchone()[0]
    return None


def confirm_effect(db, kind, operation):
    with db:
        db.execute("UPDATE effects SET state = 'confirmed' WHERE kind = ? AND operation = ?", (kind, operation))
    return "confirmed"


class StandbyAuthority:
    def __init__(self, directory: Path, *, clock=time.monotonic):
        self._clock = clock
        self._grant = None
        self._deadline = None
        directory_info = directory.lstat()
        if (not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.getuid()
                or directory_info.st_mode & 0o077):
            raise ValueError("Use a private authority directory on the broker's local disk")
        self._database = directory / "authority.sqlite"
        database = self._database
        with ExitStack() as opening:
            lock = opening.enter_context(open_private(directory, "authority.lock"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with open_private(directory, database.name):
                self._db = sqlite3.connect(database)
            opening.callback(self._db.close)
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS holder (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    node TEXT NOT NULL,
                    epoch INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS planned_holder (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    epoch INTEGER NOT NULL,
                    node TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS effects (
                    kind TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    state TEXT NOT NULL,
                    PRIMARY KEY (kind, operation)
                );
            """)
            prior = self._last_grant()
            self._recover_after = clock() + LEASE_SECONDS if prior else clock()
            self._resources = opening.pop_all()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._resources.close()

    def _last_grant(self):
        row = self._db.execute("SELECT node, epoch FROM holder WHERE id = 1").fetchone()
        return Grant(*row) if row else None

    def _valid_grant(self, now):
        return self._grant if self._deadline is not None and now < self._deadline else None

    def _planned_holder(self):
        row = self._db.execute("SELECT node, epoch FROM planned_holder WHERE id = 1").fetchone()
        return Grant(*row) if row else None

    def plan_handoff(self, expected, destination, *, replaces=None):
        """Set the next holder while exclusively opening a stopped broker's journal."""
        if destination not in (PRIMARY, STANDBY) or replaces not in (None, PRIMARY, STANDBY):
            raise ValueError("Unknown Captain host")
        if expected is None or self._grant is not None or self._last_grant() != expected:
            raise LeaseRejected("Handoff requires a stopped broker and the current source epoch")

        pending = self._planned_holder()
        if pending == Grant(destination, expected.epoch):
            return "planned"
        previous = Grant(replaces, expected.epoch) if replaces is not None else None
        if pending != previous:
            raise LeaseRejected("Handoff target changed")
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO planned_holder VALUES (1, ?, ?)",
                             (expected.epoch, destination))
        return "planned"

    def claim(self, node):
        if node not in (PRIMARY, STANDBY):
            raise ValueError("Unknown Captain host")
        now = self._clock()
        if now < self._recover_after:
            return None
        grant = self._valid_grant(now)
        if grant is not None:
            return grant if node == grant.node else None
        last = self._last_grant()
        pending = self._planned_holder()
        if pending is not None:
            if last is None or pending.epoch != last.epoch:
                raise LeaseRejected("Planned holder does not match the last epoch")
            eligible = pending.node
        else:
            # The Nuc cannot race the Mac once its renewal window has closed.
            eligible = PRIMARY if last is None else STANDBY
        if node != eligible:
            return None
        grant = Grant(node, 1 if last is None else last.epoch + 1)
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO holder (id, node, epoch) VALUES (1, ?, ?)",
                             (grant.node, grant.epoch))
            self._db.execute("DELETE FROM planned_holder WHERE id = 1")
        self._grant = grant
        self._deadline = self._clock() + LEASE_SECONDS
        return grant

    def renew(self, grant):
        now = self._clock()
        if grant is None or grant != self._valid_grant(now):
            raise LeaseRejected("Captain lease expired or superseded")
        self._deadline = now + LEASE_SECONDS

    def perform(self, grant, kind, operation, send):
        """Call the broker-owned sender once. An interrupted send stays unconfirmed.

        The sender must use a bounded timeout. Returning means the upstream
        accepted the request. Exceptions leave the durable reservation in place.
        """
        if (kind not in EFFECT_KINDS or not isinstance(operation, str)
                or not re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", operation)):
            raise ValueError("Invalid effect identity")
        self.require_grant(grant)
        state = reserve_effect(self._db, kind, operation)
        if state is not None:
            return state
        try:
            self.require_grant(grant)
        except LeaseRejected:
            with self._db:
                self._db.execute("DELETE FROM effects WHERE kind = ? AND operation = ?", (kind, operation))
            raise
        send()
        return confirm_effect(self._db, kind, operation)

    def send_telegram_progress(self, message_id, phase, send, *, grant=None):
        """Use the gateway’s receipt for typing or acknowledgment."""
        if type(message_id) is not int or not 0 < message_id < 2**63 - 1 or phase not in ("ack", "typing"):
            raise ValueError("Invalid queued message progress")
        kind = "telegram-typing" if phase == "typing" else "telegram-reply"
        operation = f"message:{message_id}:{phase}"
        path = self._database
        with open_private(path.parent, path.name, create=False), closing(sqlite3.connect(path, timeout=0.25)) as db:
            if grant is not None:
                self.require_grant(grant)
            state = reserve_effect(db, kind, operation)
            if state is not None:
                return state
            if grant is not None:
                try:
                    self.require_grant(grant)
                except LeaseRejected:
                    with db:
                        db.execute("DELETE FROM effects WHERE kind = ? AND operation = ?", (kind, operation))
                    raise
            send()
            return confirm_effect(db, kind, operation)

    def effect_status(self, grant, kind, operation):
        self.require_grant(grant)
        row = self._db.execute("SELECT state FROM effects WHERE kind = ? AND operation = ?",
                               (kind, operation)).fetchone()
        return row[0] if row else None

    def require_grant(self, grant):
        if grant is None or grant != self._valid_grant(self._clock()):
            raise LeaseRejected("Captain lease expired or superseded")


def main():
    parser = argparse.ArgumentParser(description="Plan the next lease on a stopped Captain broker.")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--source", choices=(PRIMARY, STANDBY), required=True)
    parser.add_argument("--epoch", type=int, required=True)
    parser.add_argument("--destination", choices=(PRIMARY, STANDBY), required=True)
    parser.add_argument("--replace-target", choices=(PRIMARY, STANDBY))
    args = parser.parse_args()
    try:
        with open_private(args.state, "authority.sqlite", create=False), StandbyAuthority(args.state) as authority:
            authority.plan_handoff(Grant(args.source, args.epoch), args.destination, replaces=args.replace_target)
    except BlockingIOError:
        parser.exit(1, "Captain broker journal is in use.\n")
    except (OSError, ValueError, sqlite3.Error):
        parser.exit(1, "Captain handoff could not be planned. Check the private journal and expected lease.\n")
    print("Captain handoff planned.")


if __name__ == "__main__":
    main()

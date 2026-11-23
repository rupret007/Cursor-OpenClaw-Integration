"""Lease and effect journal for a single, independently hosted Captain authority.

The broker owns this store and calls it from one thread. Its local disk and
upstream credentials stay with the broker. Gateways authenticate to the broker,
which supplies the node identity and owns the outbound effect callbacks.
"""
from contextlib import ExitStack
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


class StandbyAuthority:
    def __init__(self, directory: Path, *, clock=time.monotonic):
        self._clock = clock
        self._grant = None
        self._deadline = None
        directory_info = directory.lstat()
        if (not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.getuid()
                or directory_info.st_mode & 0o077):
            raise ValueError("Use a private authority directory on the broker's local disk")
        database = directory / "authority.sqlite"
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

    def claim(self, node):
        if node not in (PRIMARY, STANDBY):
            raise ValueError("Unknown Captain host")
        now = self._clock()
        if now < self._recover_after:
            return None
        grant = self._valid_grant(now)
        if grant is not None:
            return grant if node == grant.node else None
        # The Nuc cannot race the Mac once its renewal window has closed.
        # Handback is a separate operation outside this automatic path.
        last = self._last_grant()
        eligible = PRIMARY if last is None else STANDBY
        if node != eligible:
            return None
        grant = Grant(node, 1 if last is None else last.epoch + 1)
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO holder (id, node, epoch) VALUES (1, ?, ?)",
                             (grant.node, grant.epoch))
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
        state = self.effect_status(grant, kind, operation)
        if state is not None:
            return state
        with self._db:
            self._db.execute("INSERT INTO effects (kind, operation, state) VALUES (?, ?, 'unconfirmed')",
                             (kind, operation))
        try:
            self.require_grant(grant)
        except LeaseRejected:
            with self._db:
                self._db.execute("DELETE FROM effects WHERE kind = ? AND operation = ?", (kind, operation))
            raise
        send()
        with self._db:
            self._db.execute("UPDATE effects SET state = 'confirmed' WHERE kind = ? AND operation = ?",
                             (kind, operation))
        return "confirmed"

    def effect_status(self, grant, kind, operation):
        self.require_grant(grant)
        row = self._db.execute("SELECT state FROM effects WHERE kind = ? AND operation = ?",
                               (kind, operation)).fetchone()
        return row[0] if row else None

    def require_grant(self, grant):
        if grant is None or grant != self._valid_grant(self._clock()):
            raise LeaseRejected("Captain lease expired or superseded")

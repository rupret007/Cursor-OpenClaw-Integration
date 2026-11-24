"""Lease and effect journal for a single, independently hosted Captain authority.

The broker owns this store and calls it from one thread. Its local disk and
upstream credentials stay with the broker. Gateways authenticate to the broker,
which supplies the node identity and owns the outbound effect callbacks.
"""
import argparse
from contextlib import ExitStack, closing
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time


PRIMARY = "nucbox"
STANDBY = "mac-mini"
HEARTBEAT_SECONDS = 15
LEASE_SECONDS = 90
IMPORT_PENDING = "broker-import-pending.json"
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
    def __init__(self, directory: Path, *, clock=time.monotonic, export_id=None):
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
            pending = directory / IMPORT_PENDING
            if pending.exists() or pending.is_symlink():
                raise LeaseRejected("Broker journal has an interrupted import")
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
                CREATE TABLE IF NOT EXISTS broker_export (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    transfer_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS effects (
                    kind TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    state TEXT NOT NULL,
                    PRIMARY KEY (kind, operation)
                );
            """)
            sealed = self._export_id()
            if sealed is not None and sealed != export_id:
                raise LeaseRejected("Broker journal was sealed for transfer")
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

    def _export_id(self):
        row = self._db.execute("SELECT transfer_id FROM broker_export WHERE id = 1").fetchone()
        return row[0] if row else None

    def seal_export(self, expected, destination, transfer_id):
        """Fence restarts before a portable copy of the journal can be published."""
        if not isinstance(transfer_id, str) or not re.fullmatch(r"[0-9a-f]{32}", transfer_id):
            raise ValueError("Expected a 32-character hex transfer ID")
        sealed = self._export_id()
        if sealed is not None and sealed != transfer_id:
            raise LeaseRejected("Broker journal is sealed for another transfer")
        if sealed is not None and self._planned_holder() != Grant(destination, expected.epoch):
            raise LeaseRejected("Broker journal is sealed for a different destination")
        self.plan_handoff(expected, destination, transfer_id=transfer_id)

    def seal_rollback(self, source, failed_owner, imported_digest, destination, transfer_id):
        """Advance the fence without starting a broker or claiming a live lease."""
        if destination not in (PRIMARY, STANDBY) or failed_owner not in (PRIMARY, STANDBY):
            raise ValueError("Unknown Captain host")
        if not re.fullmatch(r"[0-9a-f]{32}", transfer_id) or source.epoch >= 2**63 - 1:
            raise ValueError("Invalid rollback identity")
        if self._grant is not None or read_imported(self._db) != (imported_digest,):
            raise LeaseRejected("Rollback requires the installed transfer on a stopped destination")

        fence = Grant(failed_owner, source.epoch + 1)
        sealed = self._export_id()
        if sealed is not None:
            if (sealed != transfer_id or self._last_grant() != fence
                    or self._planned_holder() != Grant(destination, fence.epoch)):
                raise LeaseRejected("Destination is already sealed for a different return")
            return fence
        if self._last_grant() != source or self._planned_holder() != Grant(failed_owner, source.epoch):
            raise LeaseRejected("Destination already claimed or changed its planned lease")

        with self._db:
            self._db.execute("UPDATE holder SET node = ?, epoch = ? WHERE id = 1", (fence.node, fence.epoch))
            self._db.execute("UPDATE planned_holder SET node = ?, epoch = ? WHERE id = 1",
                             (destination, fence.epoch))
            self._db.execute("INSERT INTO broker_export VALUES (1, ?)", (transfer_id,))
        return fence

    def _planned_holder(self):
        row = self._db.execute("SELECT node, epoch FROM planned_holder WHERE id = 1").fetchone()
        return Grant(*row) if row else None

    def plan_handoff(self, expected, destination, *, replaces=None, transfer_id=None):
        """Set the next holder while exclusively opening a stopped broker's journal."""
        sealed = self._export_id()
        if sealed is not None and sealed != transfer_id:
            raise LeaseRejected("Broker journal was sealed for transfer")
        if destination not in (PRIMARY, STANDBY) or replaces not in (None, PRIMARY, STANDBY):
            raise ValueError("Unknown Captain host")
        if expected is None or self._grant is not None or self._last_grant() != expected:
            raise LeaseRejected("Handoff requires a stopped broker and the current source epoch")

        pending = self._planned_holder()
        wanted = Grant(destination, expected.epoch)
        previous = Grant(replaces, expected.epoch) if replaces is not None else None
        if pending != wanted and pending != previous:
            raise LeaseRejected("Handoff target changed")
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO planned_holder VALUES (1, ?, ?)",
                             (expected.epoch, destination))
            if transfer_id is not None:
                self._db.execute("INSERT OR IGNORE INTO broker_export VALUES (1, ?)", (transfer_id,))
        return "planned"

    def claim(self, node):
        if node not in (PRIMARY, STANDBY):
            raise ValueError("Unknown Captain host")
        if self._export_id() is not None:
            raise LeaseRejected("Broker journal was sealed for transfer")
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


BROKER_JOURNALS = (
    "authority.sqlite", "telegram-inbox.sqlite", "project-daily.sqlite", "project-reservations.sqlite",
    "review-queue-receipts.jsonl", "project-queue-receipts.jsonl", "private-memory.sqlite",
)


def private_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Expected a private broker directory")


def sync_path(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def journal_digests(directory):
    result = {}
    for name in BROKER_JOURNALS:
        try:
            source = open_private(directory, name, create=False)
        except FileNotFoundError:
            if name == "authority.sqlite":
                raise
            continue
        with source:
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
            result[name] = digest.hexdigest()
    return result


def copy_database(source, output):
    deadline = time.monotonic() + 30

    def enforce_deadline(status, remaining, total):
        if time.monotonic() >= deadline:
            raise TimeoutError("Broker database copy timed out")

    with (closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as live,
          closing(sqlite3.connect(output, timeout=1)) as copy):
        live.backup(copy, pages=256, progress=enforce_deadline)
        if copy.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("Broker journal failed validation")


def export_snapshot(authority, state, output, source, destination, transfer_id):
    """Publish a sealed snapshot while holding the same lock as the live broker."""
    private_directory(state)
    private_directory(output.parent)
    if output.resolve().is_relative_to(state.resolve()) or state.resolve().is_relative_to(output.resolve()):
        raise ValueError("Keep the export outside the broker state")
    identity = {"version": 1, "transfer": transfer_id, "source": source.node,
                "epoch": source.epoch, "destination": destination}
    if output.exists() or output.is_symlink():
        private_directory(output)
        if authority._export_id() != transfer_id or authority._last_grant() != source:
            raise LeaseRejected("Existing export does not match the source fence")
        journals = journal_digests(output)
        if ({entry.name for entry in output.iterdir()} != {*journals, "transfer.json"}
                or read_private_json(output / "transfer.json") != {**identity, "journals": journals}):
            raise ValueError("Existing export failed verification")
        sync_path(output.parent)
        return

    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".captain-export-") as temporary:
        # Rename a child, so cleanup can still remove the private staging parent.
        snapshot = Path(temporary) / "snapshot"
        snapshot.mkdir(mode=0o700)
        authority.seal_export(source, destination, transfer_id)
        for name in BROKER_JOURNALS:
            try:
                data = open_private(state, name, create=False)
            except FileNotFoundError:
                if name == "authority.sqlite":
                    raise
                continue
            with data, open_private(snapshot, name):
                if name.endswith(".sqlite"):
                    copy_database(state / name, snapshot / name)
                else:
                    with (snapshot / name).open("wb") as copy:
                        shutil.copyfileobj(data, copy)
            sync_path(snapshot / name)
        manifest = {**identity, "journals": journal_digests(snapshot)}
        with open(snapshot / "transfer.json", "x", opener=private_create) as receipt:
            json.dump(manifest, receipt, sort_keys=True)
        sync_path(snapshot / "transfer.json")
        sync_path(snapshot)
        snapshot.rename(output)
        sync_path(output.parent)


def snapshot_manifest(directory, source, destination, transfer_id):
    private_directory(directory)
    journals = journal_digests(directory)
    expected = {"version": 1, "transfer": transfer_id, "source": source.node, "epoch": source.epoch,
                "destination": destination, "journals": journals}
    if (read_private_json(directory / "transfer.json") != expected
            or {entry.name for entry in directory.iterdir()} != {*journals, "transfer.json"}):
        raise ValueError("Snapshot does not match the expected transfer")
    return expected


def manifest_digest(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def rollback_snapshot(authority, state, original, output, source, destination, transfer_id):
    private_directory(original)
    metadata = read_private_json(original / "transfer.json")
    if not isinstance(metadata, dict) or metadata.get("destination") not in (PRIMARY, STANDBY):
        raise ValueError("Use the original verified import snapshot")
    incoming_id = metadata.get("transfer")
    if not isinstance(incoming_id, str) or not re.fullmatch(r"[0-9a-f]{32}", incoming_id) or incoming_id == transfer_id:
        raise ValueError("Rollback requires a new transfer ID")
    if original.resolve() == output.resolve():
        raise ValueError("Keep the original import snapshot separate from the return snapshot")

    failed_owner = metadata["destination"]
    manifest = snapshot_manifest(original, source, failed_owner, incoming_id)
    private_directory(output.parent)
    if output.resolve().is_relative_to(state.resolve()) or state.resolve().is_relative_to(output.resolve()):
        raise ValueError("Keep the export outside the broker state")
    fence = authority.seal_rollback(source, failed_owner, manifest_digest(manifest), destination, transfer_id)
    export_snapshot(authority, state, output, fence, destination, transfer_id)
    print(f"Captain return snapshot ready. Import source {fence.node}, epoch {fence.epoch}.")


def read_imported(db):
    table = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'broker_import'").fetchone()
    return db.execute("SELECT digest FROM broker_import WHERE id = 1").fetchone() if table else None


def check_import_target(state, source, digest):
    path = state / "authority.sqlite"
    try:
        existing = open_private(state, path.name, create=False)
    except FileNotFoundError:
        if any((state / name).exists() or (state / name).is_symlink() for name in BROKER_JOURNALS[1:]):
            raise LeaseRejected("Destination has journals without an authority")
        return False
    with existing, closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        exported = db.execute("SELECT transfer_id FROM broker_export WHERE id = 1").fetchone()
        if exported is None:
            if read_imported(db) == (digest,):
                return True
            raise LeaseRejected("Destination must be empty or sealed by an earlier export")
        prior = db.execute("SELECT epoch FROM holder WHERE id = 1").fetchone()
        if prior is None or prior[0] >= source.epoch:
            raise LeaseRejected("The incoming epoch is not newer than the destination fence")
    return False


def import_snapshot(state, snapshot, source, destination, transfer_id):
    """Install to the fixed destination state path while blocking startup across crashes."""
    private_directory(state)
    if snapshot.resolve().is_relative_to(state.resolve()) or state.resolve().is_relative_to(snapshot.resolve()):
        raise ValueError("Keep the snapshot separate from the destination state")
    if not re.fullmatch(r"[0-9a-f]{32}", transfer_id):
        raise ValueError("Expected a 32-character hex transfer ID")
    manifest = snapshot_manifest(snapshot, source, destination, transfer_id)
    digest = manifest_digest(manifest)
    pending = state / IMPORT_PENDING
    with open_private(state, "authority.lock") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if pending.exists() or pending.is_symlink():
            if read_private_json(pending) != digest:
                raise LeaseRejected("Resume the same destination transfer first")
        elif check_import_target(state, source, digest):
            return False
        for name in BROKER_JOURNALS:
            if any(os.path.lexists(state / (name + suffix)) for suffix in ("-wal", "-shm", "-journal")):
                raise LeaseRejected("Destination has unfinished database recovery")

        with tempfile.TemporaryDirectory(dir=state, prefix=".captain-import-") as temporary:
            staged = Path(temporary)
            for name in manifest["journals"]:
                with open_private(snapshot, name, create=False) as data:
                    with open(staged / name, "xb", opener=private_create) as output:
                        shutil.copyfileobj(data, output)
            if journal_digests(staged) != manifest["journals"]:
                raise ValueError("Staged snapshot failed verification")
            database = staged / "authority.sqlite"
            with closing(sqlite3.connect(database, timeout=1)) as db, db:
                if (db.execute("SELECT node, epoch FROM holder WHERE id = 1").fetchone() != (source.node, source.epoch)
                        or db.execute("SELECT node, epoch FROM planned_holder WHERE id = 1").fetchone()
                        != (destination, source.epoch)
                        or db.execute("SELECT transfer_id FROM broker_export WHERE id = 1").fetchone()
                        != (transfer_id,)):
                    raise LeaseRejected("Snapshot is missing the matching source fence")
                db.execute("CREATE TABLE IF NOT EXISTS broker_import (id INTEGER PRIMARY KEY CHECK (id = 1), digest TEXT)")
                db.execute("INSERT OR REPLACE INTO broker_import VALUES (1, ?)", (digest,))
                db.execute("DELETE FROM broker_export WHERE id = 1")
            for name in manifest["journals"]:
                sync_path(staged / name)
            with open(staged / IMPORT_PENDING, "x", opener=private_create) as receipt:
                json.dump(digest, receipt)
            sync_path(staged / IMPORT_PENDING)
            (staged / IMPORT_PENDING).replace(pending)
            sync_path(state)
            for name in BROKER_JOURNALS[1:]:
                if name in manifest["journals"]:
                    (staged / name).replace(state / name)
                else:
                    (state / name).unlink(missing_ok=True)
            database.replace(state / database.name)
            sync_path(state)
            pending.unlink()
            sync_path(state)
    return True


def private_create(path, flags):
    return os.open(path, flags | os.O_NOFOLLOW, 0o600)


def main():
    parser = argparse.ArgumentParser(description="Plan the next lease on a stopped Captain broker.")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--source", choices=(PRIMARY, STANDBY), required=True)
    parser.add_argument("--epoch", type=int, required=True)
    parser.add_argument("--destination", choices=(PRIMARY, STANDBY), required=True)
    parser.add_argument("--replace-target", choices=(PRIMARY, STANDBY))
    transfer = parser.add_mutually_exclusive_group()
    transfer.add_argument("--export-path", type=Path)
    transfer.add_argument("--import-path", type=Path)
    parser.add_argument("--transfer-id")
    parser.add_argument("--rollback-from", type=Path, help="Return a transfer before its destination claims a lease")
    args = parser.parse_args()
    transferring = args.export_path is not None or args.import_path is not None
    if transferring != (args.transfer_id is not None) or (transferring and args.replace_target):
        parser.error("A transfer requires a snapshot path and transfer ID, independent of target replacement")
    if args.rollback_from is not None and args.export_path is None:
        parser.error("Rollback requires a return export path and a new transfer ID")
    source = Grant(args.source, args.epoch)
    try:
        private_directory(args.state)
        if args.import_path:
            imported = import_snapshot(args.state, args.import_path, source, args.destination, args.transfer_id)
            print("Captain broker import ready." if imported else "Captain broker import already applied.")
            return
        with (open_private(args.state, "authority.sqlite", create=False),
              StandbyAuthority(args.state, export_id=args.transfer_id) as authority):
            if args.rollback_from:
                rollback_snapshot(authority, args.state, args.rollback_from, args.export_path,
                                  source, args.destination, args.transfer_id)
                return
            if args.export_path:
                export_snapshot(authority, args.state, args.export_path, source, args.destination, args.transfer_id)
            else:
                authority.plan_handoff(source, args.destination, replaces=args.replace_target)
    except BlockingIOError:
        parser.exit(1, "Captain broker journal is in use.\n")
    except (OSError, ValueError, sqlite3.Error):
        parser.exit(1, "Captain handoff could not be planned. Check the private journal and expected lease.\n")
    print("Captain broker export ready." if args.export_path else "Captain handoff planned.")


if __name__ == "__main__":
    main()

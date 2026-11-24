"""Owner-confirmed facts stored with the broker's transferable state."""

from contextlib import closing
import json
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import time

from standby import open_private


TOPIC = re.compile(r"[a-z][a-z0-9_-]{0,62}", re.ASCII)
REMEMBER = re.compile(r"remember ([a-z][a-z0-9_-]{0,62}): ([^\r\n]{1,350})", re.IGNORECASE | re.ASCII)
CONFIRM = re.compile(r"confirm memory ([0-9a-f]{16})", re.IGNORECASE | re.ASCII)
GUARD_SOURCE = """
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const { safeMemoryFact } = await import(pathToFileURL(process.argv[1]));
process.exit(safeMemoryFact(JSON.parse(readFileSync(0, 'utf8'))) ? 0 : 3);
"""


def packaged_guard(node):
    guard = Path(__file__).resolve().parents[2] / "memory-guard.mjs"
    if not node.is_absolute() or not node.is_file() or not guard.is_file():
        raise ValueError("Expected the pinned runtime for private memory")

    def check(statement):
        try:
            result = subprocess.run([str(node), "--input-type=module", "-e", GUARD_SOURCE, str(guard)],
                                    input=json.dumps(statement), text=True, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env={}, timeout=2, check=False)
        except subprocess.TimeoutExpired:
            raise ValueError("Private memory validation timed out") from None
        if result.returncode not in (0, 3):
            raise ValueError("Private memory validation did not complete")
        return result.returncode == 0

    return check


class BrokerMemory:
    def __init__(self, directory, owner, bot, validate, *, clock=time.time):
        self.path = directory / "private-memory.sqlite"
        self.validate, self.clock = validate, clock
        with open_private(directory, self.path.name), closing(sqlite3.connect(self.path, timeout=1)) as db, db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY, owner INTEGER, bot INTEGER)")
            db.execute("INSERT OR IGNORE INTO identity VALUES (1, ?, ?)", (owner, bot))
            if db.execute("SELECT owner, bot FROM identity WHERE id = 1").fetchone() != (owner, bot):
                raise ValueError("Private memory belongs to a different Telegram owner or bot")
            db.execute("CREATE TABLE IF NOT EXISTS facts (topic TEXT PRIMARY KEY, statement TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS proposals (
                message INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, topic TEXT NOT NULL,
                statement TEXT NOT NULL, epoch INTEGER NOT NULL, expires INTEGER NOT NULL
            )""")
            db.execute("CREATE TABLE IF NOT EXISTS confirmations (message INTEGER PRIMARY KEY, topic TEXT NOT NULL)")

    def run(self, authority, grant, inbox, message_id, operation, topic=None):
        authority.require_grant(grant)
        pending = inbox.next()
        if pending is None or pending["message_id"] != message_id:
            raise ValueError("Private memory needs the current owner message")
        with open_private(self.path.parent, self.path.name, create=False), closing(
                sqlite3.connect(self.path, timeout=1)) as db, db:
            db.execute("PRAGMA secure_delete=ON")
            if operation == "recall":
                if not isinstance(topic, str) or not TOPIC.fullmatch(topic):
                    raise ValueError("Invalid private memory topic")
                row = db.execute("SELECT statement FROM facts WHERE topic = ?", (topic,)).fetchone()
                return {"state": "saved", "topic": topic, "statement": row[0]} if row else {"state": "missing"}

            now = int(self.clock())
            if operation == "remember":
                return self.propose(db, authority, grant, message_id, pending["text"], now)
            if operation == "confirm":
                authority.require_grant(grant)
                return self.confirm(db, grant, message_id, pending["text"], now)
            raise ValueError("Unknown private memory operation")

    def propose(self, db, authority, grant, message, text, now):
        match = REMEMBER.fullmatch(text.strip())
        if match is None:
            return {"state": "syntax"}
        topic, statement = match[1].lower(), match[2].strip()
        if not self.validate(statement):
            return {"state": "unsafe"}
        authority.require_grant(grant)
        db.execute("DELETE FROM proposals WHERE expires <= ? OR epoch != ?", (now, grant.epoch))
        prior = db.execute("SELECT id FROM proposals WHERE message = ?", (message,)).fetchone()
        if prior:
            return {"state": "pending", "topic": topic, "code": prior[0]}
        if db.execute("SELECT count(*) FROM proposals").fetchone()[0] >= 24:
            return {"state": "busy"}
        code = secrets.token_hex(8)
        db.execute("DELETE FROM proposals WHERE topic = ?", (topic,))
        db.execute("INSERT INTO proposals VALUES (?, ?, ?, ?, ?, ?)",
                   (message, code, topic, statement, grant.epoch, now + 300))
        return {"state": "pending", "topic": topic, "code": code}

    def confirm(self, db, grant, message, text, now):
        match = CONFIRM.fullmatch(text.strip())
        if match is None:
            return {"state": "syntax"}
        saved = db.execute("SELECT topic FROM confirmations WHERE message = ?", (message,)).fetchone()
        if saved:
            return {"state": "confirmed", "topic": saved[0]}
        proposal = db.execute("SELECT topic, statement FROM proposals WHERE id = ? AND epoch = ? AND expires > ?",
                              (match[1].lower(), grant.epoch, now)).fetchone()
        if proposal is None:
            return {"state": "expired"}
        db.execute("INSERT OR REPLACE INTO facts VALUES (?, ?)", proposal)
        db.execute("INSERT INTO confirmations VALUES (?, ?)", (message, proposal[0]))
        db.execute("DELETE FROM proposals WHERE id = ?", (match[1].lower(),))
        return {"state": "confirmed", "topic": proposal[0]}

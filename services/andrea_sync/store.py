"""SQLite WAL event store for Andrea lockstep."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401 used in list_tasks

from .schema import EventType


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            ts REAL NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_events_task_seq ON events(task_id, seq);
        CREATE TABLE IF NOT EXISTS idempotency (
            idempotency_key TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')"
    )
    conn.commit()


def append_event(
    conn: sqlite3.Connection,
    task_id: str,
    event_type: EventType,
    payload: Dict[str, Any],
) -> int:
    ts = time.time()
    cur = conn.execute(
        "INSERT INTO events(task_id, ts, event_type, payload_json) VALUES (?,?,?,?)",
        (task_id, ts, event_type.value, json.dumps(payload, ensure_ascii=False)),
    )
    conn.execute(
        "UPDATE tasks SET updated_at = ? WHERE task_id = ?",
        (ts, task_id),
    )
    conn.commit()
    return int(cur.lastrowid)


def create_task(conn: sqlite3.Connection, task_id: str, channel: str) -> None:
    ts = time.time()
    conn.execute(
        "INSERT INTO tasks(task_id, channel, created_at, updated_at) VALUES (?,?,?,?)",
        (task_id, channel, ts, ts),
    )
    conn.commit()


def claim_idempotency_or_get_existing(
    conn: sqlite3.Connection, key: str, new_task_id: str
) -> Tuple[str, bool]:
    """
    Returns (task_id, is_new_claim).
    If key already mapped, returns existing task_id and is_new_claim False.
    Otherwise inserts mapping for new_task_id and returns (new_task_id, True).
    """
    row = conn.execute(
        "SELECT task_id FROM idempotency WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if row:
        return str(row["task_id"]), False
    ts = time.time()
    conn.execute(
        "INSERT INTO idempotency(idempotency_key, task_id, created_at) VALUES (?,?,?)",
        (key, new_task_id, ts),
    )
    conn.commit()
    return new_task_id, True


def claim_idempotency_and_create_task(
    conn: sqlite3.Connection,
    key: str,
    new_task_id: str,
    channel: str,
) -> Tuple[str, bool]:
    """
    Returns (task_id, created_new_task).

    This keeps the idempotency claim and task creation in one transaction so a
    crash cannot leave an idempotency mapping without a task row.
    """
    row = conn.execute(
        "SELECT task_id FROM idempotency WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if row:
        return str(row["task_id"]), False
    ts = time.time()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT task_id FROM idempotency WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row:
            conn.rollback()
            return str(row["task_id"]), False
        conn.execute(
            "INSERT INTO idempotency(idempotency_key, task_id, created_at) VALUES (?,?,?)",
            (key, new_task_id, ts),
        )
        conn.execute(
            "INSERT INTO tasks(task_id, channel, created_at, updated_at) VALUES (?,?,?,?)",
            (new_task_id, channel, ts, ts),
        )
        conn.commit()
        return new_task_id, True
    except Exception:
        conn.rollback()
        raise


def load_events_for_task(
    conn: sqlite3.Connection, task_id: str
) -> List[Tuple[int, float, str, Dict[str, Any]]]:
    rows = conn.execute(
        "SELECT seq, ts, event_type, payload_json FROM events WHERE task_id = ? ORDER BY seq ASC",
        (task_id,),
    ).fetchall()
    out: List[Tuple[int, float, str, Dict[str, Any]]] = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        out.append((int(r["seq"]), float(r["ts"]), str(r["event_type"]), payload))
    return out


def task_exists(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    return row is not None


def get_task_channel(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT channel FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    return str(row["channel"]) if row else None


def list_tasks(conn: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT task_id, channel, created_at, updated_at FROM tasks ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


SYSTEM_TASK_ID = "tsk_system_lockstep"


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def ensure_system_task(conn: sqlite3.Connection) -> None:
    """Reserved task row for global audit events (capabilities, kill switch)."""
    if task_exists(conn, SYSTEM_TASK_ID):
        return
    create_task(conn, SYSTEM_TASK_ID, "internal")

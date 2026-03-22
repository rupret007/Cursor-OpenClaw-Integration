"""SQLite WAL event store for Andrea lockstep."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401 used in list_tasks

from .schema import Channel, EventType


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


def delete_meta(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM meta WHERE key = ?", (key,))
    conn.commit()


def claim_scoped_idempotency(
    conn: sqlite3.Connection, key: str, task_id: str
) -> str:
    """
    Idempotency for operations on an existing task (Telegram retries, cursor reports).

    Returns:
        'fresh' — first time this key was claimed for this task_id
        'duplicate' — same key and same task_id (safe retry)
        'conflict' — key exists for a different task_id (should not happen)
    """
    row = conn.execute(
        "SELECT task_id FROM idempotency WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if row:
        existing = str(row["task_id"])
        if existing == task_id:
            return "duplicate"
        return "conflict"
    ts = time.time()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row2 = conn.execute(
            "SELECT task_id FROM idempotency WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row2:
            conn.rollback()
            existing = str(row2["task_id"])
            if existing == task_id:
                return "duplicate"
            return "conflict"
        conn.execute(
            "INSERT INTO idempotency(idempotency_key, task_id, created_at) VALUES (?,?,?)",
            (key, task_id, ts),
        )
        conn.commit()
        return "fresh"
    except Exception:
        conn.rollback()
        raise


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
            if os.environ.get("ANDREA_SYNC_JSON_PARSE_WARNINGS", "0") == "1":
                print(
                    f"andrea_sync JSON parse warning: task={task_id} seq={r['seq']}",
                    flush=True,
                )
            payload = {}
        try:
            seq = int(r["seq"])
            ts = float(r["ts"])
        except (TypeError, ValueError):
            continue
        out.append((seq, ts, str(r["event_type"]), payload))
    return out


def _clip_text(value: Any, limit: int = 600) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def load_recent_telegram_history(
    conn: sqlite3.Connection,
    chat_id: Any,
    *,
    limit_turns: int = 6,
    exclude_task_id: Optional[str] = None,
) -> List[Dict[str, str]]:
    if chat_id is None:
        return []
    params: List[Any] = [Channel.TELEGRAM.value, EventType.USER_MESSAGE.value, chat_id]
    exclude_clause = ""
    if exclude_task_id:
        exclude_clause = "AND e.task_id != ?"
        params.append(exclude_task_id)
    params.append(max(1, int(limit_turns)))
    rows = conn.execute(
        f"""
        SELECT e.task_id, MAX(e.seq) AS last_seq
        FROM events e
        JOIN tasks t ON t.task_id = e.task_id
        WHERE t.channel = ?
          AND e.event_type = ?
          AND json_extract(e.payload_json, '$.chat_id') = ?
          {exclude_clause}
        GROUP BY e.task_id
        ORDER BY last_seq DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    task_ids = [str(row["task_id"]) for row in reversed(rows)]
    history: List[Dict[str, str]] = []
    for task_id in task_ids:
        user_text = ""
        assistant_text = ""
        assistant_source = ""
        for _seq, _ts, et_raw, payload in load_events_for_task(conn, task_id):
            if et_raw == EventType.USER_MESSAGE.value and payload.get("text"):
                user_text = _clip_text(payload.get("routing_text") or payload.get("text"))
            elif et_raw == EventType.ASSISTANT_REPLIED.value and payload.get("text"):
                assistant_text = _clip_text(payload.get("text"))
                assistant_source = "direct"
            elif et_raw == EventType.JOB_COMPLETED.value and payload.get("summary"):
                backend = str(payload.get("backend") or "").strip()
                delegated = bool(payload.get("delegated_to_cursor"))
                if backend == "openclaw" and delegated:
                    assistant_text = _clip_text(f"OpenClaw and Cursor completed: {payload.get('summary')}")
                    assistant_source = "openclaw_cursor"
                elif backend == "openclaw":
                    assistant_text = _clip_text(f"OpenClaw completed: {payload.get('summary')}")
                    assistant_source = "openclaw"
                else:
                    assistant_text = _clip_text(f"Cursor completed: {payload.get('summary')}")
                    assistant_source = "cursor"
            elif et_raw == EventType.JOB_FAILED.value:
                detail = payload.get("message") or payload.get("error")
                if detail:
                    backend = str(payload.get("backend") or "").strip()
                    if backend == "openclaw":
                        assistant_text = _clip_text(f"OpenClaw failed: {detail}")
                        assistant_source = "openclaw"
                    else:
                        assistant_text = _clip_text(f"Cursor failed: {detail}")
                        assistant_source = "cursor"
        if user_text:
            history.append({"role": "user", "content": user_text, "task_id": task_id})
        if assistant_text:
            history.append(
                {
                    "role": "assistant",
                    "content": assistant_text,
                    "task_id": task_id,
                    "source": assistant_source or "direct",
                }
            )
    return history


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


def get_task_updated_at(conn: sqlite3.Connection, task_id: str) -> Optional[float]:
    row = conn.execute(
        "SELECT updated_at FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    return float(row["updated_at"]) if row else None


def list_recent_telegram_task_ids(conn: sqlite3.Connection, limit: int = 25) -> List[str]:
    """Most recently touched Telegram tasks (excluding the reserved system task)."""
    rows = conn.execute(
        """
        SELECT task_id FROM tasks
        WHERE channel = ? AND task_id != ?
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (Channel.TELEGRAM.value, SYSTEM_TASK_ID, limit),
    ).fetchall()
    return [str(r["task_id"]) for r in rows]


def list_telegram_task_ids_for_chat(
    conn: sqlite3.Connection,
    chat_id: Any,
    *,
    limit: int = 25,
) -> List[str]:
    """
    Telegram tasks that have at least one UserMessage with this chat_id,
    ordered by task updated_at (best-effort per-chat continuation lookup).
    """
    if chat_id is None:
        return []
    cid = str(chat_id).strip()
    rows = conn.execute(
        """
        SELECT t.task_id
        FROM tasks t
        WHERE t.channel = ?
          AND t.task_id != ?
          AND EXISTS (
            SELECT 1 FROM events e
            WHERE e.task_id = t.task_id
              AND e.event_type = ?
              AND CAST(json_extract(e.payload_json, '$.chat_id') AS TEXT) = ?
          )
        ORDER BY t.updated_at DESC
        LIMIT ?
        """,
        (
            Channel.TELEGRAM.value,
            SYSTEM_TASK_ID,
            EventType.USER_MESSAGE.value,
            cid,
            limit,
        ),
    ).fetchall()
    return [str(r["task_id"]) for r in rows]


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

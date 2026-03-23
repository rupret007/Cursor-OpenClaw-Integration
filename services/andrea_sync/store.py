"""SQLite WAL event store for Andrea lockstep."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401 used in list_tasks

from .experience_types import new_experience_run_id
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
        CREATE TABLE IF NOT EXISTS principals (
            principal_id TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            display_name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS principal_links (
            channel TEXT NOT NULL,
            external_key TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(channel, external_key)
        );
        CREATE INDEX IF NOT EXISTS idx_principal_links_principal
            ON principal_links(principal_id, channel);
        CREATE TABLE IF NOT EXISTS task_principals (
            task_id TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_principals_principal
            ON task_principals(principal_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS principal_memories (
            memory_id TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            source_task_id TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_principal_memories_principal
            ON principal_memories(principal_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS principal_preferences (
            principal_id TEXT NOT NULL,
            pref_key TEXT NOT NULL,
            pref_value_json TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(principal_id, pref_key)
        );
        CREATE TABLE IF NOT EXISTS reminders (
            reminder_id TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL,
            channel TEXT NOT NULL,
            delivery_target TEXT NOT NULL,
            message TEXT NOT NULL,
            due_at REAL NOT NULL,
            status TEXT NOT NULL,
            source_task_id TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_reminders_due
            ON reminders(status, due_at ASC);
        CREATE TABLE IF NOT EXISTS incidents (
            incident_id TEXT PRIMARY KEY,
            source_task_id TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL,
            error_type TEXT NOT NULL,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            fingerprint TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            incident_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_incidents_status_updated
            ON incidents(status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint
            ON incidents(fingerprint, updated_at DESC);
        CREATE TABLE IF NOT EXISTS repair_attempts (
            attempt_id TEXT PRIMARY KEY,
            incident_id TEXT NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
            attempt_no INTEGER NOT NULL,
            stage TEXT NOT NULL,
            model_used TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            branch TEXT NOT NULL DEFAULT '',
            worktree_path TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            attempt_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_repair_attempts_incident
            ON repair_attempts(incident_id, attempt_no ASC, updated_at ASC);
        CREATE TABLE IF NOT EXISTS repair_plans (
            plan_id TEXT PRIMARY KEY,
            incident_id TEXT NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            model_used TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            plan_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_repair_plans_incident
            ON repair_plans(incident_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS experience_runs (
            run_id TEXT PRIMARY KEY,
            actor TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            passed INTEGER NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT '',
            total_checks INTEGER NOT NULL DEFAULT 0,
            failed_checks INTEGER NOT NULL DEFAULT 0,
            average_score REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            run_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_experience_runs_updated
            ON experience_runs(updated_at DESC);
        CREATE TABLE IF NOT EXISTS experience_checks (
            check_key TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES experience_runs(run_id) ON DELETE CASCADE,
            scenario_id TEXT NOT NULL,
            status TEXT NOT NULL,
            score REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            check_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_experience_checks_run
            ON experience_checks(run_id, updated_at DESC);
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


def new_principal_id() -> str:
    return f"prn_{uuid.uuid4().hex[:16]}"


def new_memory_id() -> str:
    return f"mem_{uuid.uuid4().hex[:16]}"


def new_reminder_id() -> str:
    return f"rem_{uuid.uuid4().hex[:16]}"


def new_incident_id() -> str:
    return f"inc_{uuid.uuid4().hex[:16]}"


def new_repair_attempt_id() -> str:
    return f"att_{uuid.uuid4().hex[:16]}"


def new_repair_plan_id() -> str:
    return f"rpl_{uuid.uuid4().hex[:16]}"


def principal_exists(conn: sqlite3.Connection, principal_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM principals WHERE principal_id = ?",
        (str(principal_id).strip(),),
    ).fetchone()
    return row is not None


def _principal_external_keys(channel: str, payload: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    ch = str(channel or "").strip().lower()
    if ch == Channel.TELEGRAM.value:
        if payload.get("chat_id") is not None:
            keys.append(f"chat:{payload.get('chat_id')}")
        if payload.get("from_user") is not None:
            keys.append(f"user:{payload.get('from_user')}")
        username = str(payload.get("from_username") or "").strip().lower()
        if username:
            keys.append(f"username:{username}")
    elif ch == Channel.ALEXA.value:
        user_id = str(payload.get("user_id") or "").strip()
        device_id = str(payload.get("device_id") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        if user_id:
            keys.append(f"user:{user_id}")
        if device_id:
            keys.append(f"device:{device_id}")
        if session_id:
            keys.append(f"session:{session_id}")
    else:
        principal_key = str(payload.get("principal_key") or "").strip()
        if principal_key:
            keys.append(principal_key)
    deduped: List[str] = []
    for key in keys:
        if key and key not in deduped:
            deduped.append(key)
    return deduped


def link_principal_identity(
    conn: sqlite3.Connection,
    principal_id: str,
    *,
    channel: str,
    external_key: str,
) -> None:
    pid = str(principal_id or "").strip()
    ch = str(channel or "").strip().lower()
    key = str(external_key or "").strip()
    if not pid or not ch or not key:
        return
    ts = time.time()
    conn.execute(
        """
        INSERT INTO principal_links(channel, external_key, principal_id, created_at, updated_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(channel, external_key) DO UPDATE SET
            principal_id = excluded.principal_id,
            updated_at = excluded.updated_at
        """,
        (ch, key, pid, ts, ts),
    )
    conn.execute(
        "UPDATE principals SET updated_at = ? WHERE principal_id = ?",
        (ts, pid),
    )
    conn.commit()


def resolve_principal_id(
    conn: sqlite3.Connection,
    *,
    channel: str,
    payload: Dict[str, Any],
    principal_id_hint: str = "",
) -> str:
    hinted = str(principal_id_hint or payload.get("principal_id") or "").strip()
    if hinted and principal_exists(conn, hinted):
        return hinted
    ch = str(channel or "").strip().lower()
    candidate_ids: List[str] = []
    keys = _principal_external_keys(ch, payload)
    for key in keys:
        row = conn.execute(
            "SELECT principal_id FROM principal_links WHERE channel = ? AND external_key = ?",
            (ch, key),
        ).fetchone()
        if row:
            pid = str(row["principal_id"])
            if pid and pid not in candidate_ids:
                candidate_ids.append(pid)
    principal_id = candidate_ids[0] if candidate_ids else new_principal_id()
    ts = time.time()
    if not principal_exists(conn, principal_id):
        conn.execute(
            "INSERT INTO principals(principal_id, created_at, updated_at, display_name) VALUES (?,?,?,?)",
            (principal_id, ts, ts, ""),
        )
    for key in keys:
        conn.execute(
            """
            INSERT INTO principal_links(channel, external_key, principal_id, created_at, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(channel, external_key) DO UPDATE SET
                principal_id = excluded.principal_id,
                updated_at = excluded.updated_at
            """,
            (ch, key, principal_id, ts, ts),
        )
    conn.execute(
        "UPDATE principals SET updated_at = ? WHERE principal_id = ?",
        (ts, principal_id),
    )
    conn.commit()
    return principal_id


def link_task_principal(
    conn: sqlite3.Connection,
    task_id: str,
    principal_id: str,
    *,
    channel: str,
) -> None:
    tid = str(task_id or "").strip()
    pid = str(principal_id or "").strip()
    ch = str(channel or "").strip().lower()
    if not tid or not pid or not ch:
        return
    ts = time.time()
    conn.execute(
        """
        INSERT INTO task_principals(task_id, principal_id, channel, created_at, updated_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(task_id) DO UPDATE SET
            principal_id = excluded.principal_id,
            channel = excluded.channel,
            updated_at = excluded.updated_at
        """,
        (tid, pid, ch, ts, ts),
    )
    conn.execute(
        "UPDATE principals SET updated_at = ? WHERE principal_id = ?",
        (ts, pid),
    )
    conn.commit()


def get_task_principal_id(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT principal_id FROM task_principals WHERE task_id = ?",
        (str(task_id or "").strip(),),
    ).fetchone()
    return str(row["principal_id"]) if row else None


def save_principal_memory(
    conn: sqlite3.Connection,
    principal_id: str,
    *,
    content: str,
    kind: str = "note",
    source: str = "",
    source_task_id: str = "",
    memory_id: str = "",
) -> str:
    pid = str(principal_id or "").strip()
    text = str(content or "").strip()
    if not pid or not text:
        raise ValueError("principal_id and content are required")
    mid = str(memory_id or "").strip() or new_memory_id()
    ts = time.time()
    conn.execute(
        """
        INSERT INTO principal_memories(memory_id, principal_id, kind, content, source, source_task_id, created_at, updated_at, is_active)
        VALUES (?,?,?,?,?,?,?,?,1)
        ON CONFLICT(memory_id) DO UPDATE SET
            kind = excluded.kind,
            content = excluded.content,
            source = excluded.source,
            source_task_id = excluded.source_task_id,
            updated_at = excluded.updated_at,
            is_active = 1
        """,
        (mid, pid, str(kind or "note"), text, str(source or ""), str(source_task_id or ""), ts, ts),
    )
    conn.execute(
        "UPDATE principals SET updated_at = ? WHERE principal_id = ?",
        (ts, pid),
    )
    conn.commit()
    return mid


def list_principal_memories(
    conn: sqlite3.Connection,
    principal_id: str,
    *,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT memory_id, principal_id, kind, content, source, source_task_id, created_at, updated_at
        FROM principal_memories
        WHERE principal_id = ? AND is_active = 1
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (str(principal_id or "").strip(), max(1, int(limit))),
    ).fetchall()
    return [dict(r) for r in rows]


def set_principal_preference(
    conn: sqlite3.Connection,
    principal_id: str,
    key: str,
    value: Any,
) -> None:
    pid = str(principal_id or "").strip()
    pref_key = str(key or "").strip()
    if not pid or not pref_key:
        return
    ts = time.time()
    conn.execute(
        """
        INSERT INTO principal_preferences(principal_id, pref_key, pref_value_json, updated_at)
        VALUES (?,?,?,?)
        ON CONFLICT(principal_id, pref_key) DO UPDATE SET
            pref_value_json = excluded.pref_value_json,
            updated_at = excluded.updated_at
        """,
        (pid, pref_key, json.dumps(value, ensure_ascii=False), ts),
    )
    conn.execute(
        "UPDATE principals SET updated_at = ? WHERE principal_id = ?",
        (ts, pid),
    )
    conn.commit()


def get_principal_preferences(conn: sqlite3.Connection, principal_id: str) -> Dict[str, Any]:
    rows = conn.execute(
        """
        SELECT pref_key, pref_value_json
        FROM principal_preferences
        WHERE principal_id = ?
        """,
        (str(principal_id or "").strip(),),
    ).fetchall()
    prefs: Dict[str, Any] = {}
    for row in rows:
        key = str(row["pref_key"] or "").strip()
        raw = row["pref_value_json"]
        if not key:
            continue
        try:
            prefs[key] = json.loads(raw)
        except Exception:
            prefs[key] = raw
    return prefs


def load_recent_principal_history(
    conn: sqlite3.Connection,
    principal_id: str,
    *,
    limit_turns: int = 8,
    exclude_task_id: Optional[str] = None,
) -> List[Dict[str, str]]:
    pid = str(principal_id or "").strip()
    if not pid:
        return []
    params: List[Any] = [pid]
    exclude_clause = ""
    if exclude_task_id:
        exclude_clause = "AND tp.task_id != ?"
        params.append(str(exclude_task_id))
    params.append(max(1, int(limit_turns)))
    rows = conn.execute(
        f"""
        SELECT tp.task_id, t.channel, t.updated_at
        FROM task_principals tp
        JOIN tasks t ON t.task_id = tp.task_id
        WHERE tp.principal_id = ?
          {exclude_clause}
        ORDER BY t.updated_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    ordered = list(reversed(rows))
    history: List[Dict[str, str]] = []
    for row in ordered:
        task_id = str(row["task_id"])
        channel = str(row["channel"] or "")
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
                    assistant_text = _clip_text(f"Completed: {payload.get('summary')}")
                    assistant_source = "openclaw_cursor"
                elif backend == "openclaw":
                    assistant_text = _clip_text(f"Completed: {payload.get('summary')}")
                    assistant_source = "openclaw"
                else:
                    assistant_text = _clip_text(f"Completed: {payload.get('summary')}")
                    assistant_source = "cursor"
            elif et_raw == EventType.JOB_FAILED.value:
                detail = payload.get("user_safe_error") or payload.get("message") or payload.get("error")
                if detail:
                    backend = str(payload.get("backend") or "").strip()
                    if backend == "openclaw":
                        assistant_text = _clip_text(f"Could not finish: {detail}")
                        assistant_source = "openclaw"
                    else:
                        assistant_text = _clip_text(f"Could not finish: {detail}")
                        assistant_source = "cursor"
        if user_text:
            history.append(
                {
                    "role": "user",
                    "content": user_text,
                    "task_id": task_id,
                    "channel": channel,
                }
            )
        if assistant_text:
            history.append(
                {
                    "role": "assistant",
                    "content": assistant_text,
                    "task_id": task_id,
                    "channel": channel,
                    "source": assistant_source or "direct",
                }
            )
    return history


def get_principal_recent_telegram_chat_id(
    conn: sqlite3.Connection, principal_id: str
) -> Optional[str]:
    row = conn.execute(
        """
        SELECT CAST(json_extract(e.payload_json, '$.chat_id') AS TEXT) AS chat_id
        FROM task_principals tp
        JOIN tasks t ON t.task_id = tp.task_id
        JOIN events e ON e.task_id = tp.task_id
        WHERE tp.principal_id = ?
          AND t.channel = ?
          AND e.event_type = ?
          AND json_extract(e.payload_json, '$.chat_id') IS NOT NULL
        ORDER BY e.seq DESC
        LIMIT 1
        """,
        (
            str(principal_id or "").strip(),
            Channel.TELEGRAM.value,
            EventType.USER_MESSAGE.value,
        ),
    ).fetchone()
    if not row:
        return None
    value = str(row["chat_id"] or "").strip()
    return value or None


def create_reminder(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    channel: str,
    delivery_target: str,
    message: str,
    due_at: float,
    status: str = "scheduled",
    source_task_id: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    reminder_id: str = "",
) -> str:
    pid = str(principal_id or "").strip()
    text = str(message or "").strip()
    rid = str(reminder_id or "").strip() or new_reminder_id()
    if not pid or not text:
        raise ValueError("principal_id and message are required")
    ts = time.time()
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO reminders(
            reminder_id, principal_id, channel, delivery_target, message, due_at,
            status, source_task_id, metadata_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(reminder_id) DO UPDATE SET
            channel = excluded.channel,
            delivery_target = excluded.delivery_target,
            message = excluded.message,
            due_at = excluded.due_at,
            status = excluded.status,
            source_task_id = excluded.source_task_id,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            rid,
            pid,
            str(channel or "").strip().lower(),
            str(delivery_target or "").strip(),
            text,
            float(due_at),
            str(status or "scheduled"),
            str(source_task_id or "").strip(),
            metadata_json,
            ts,
            ts,
        ),
    )
    conn.execute(
        "UPDATE principals SET updated_at = ? WHERE principal_id = ?",
        (ts, pid),
    )
    conn.commit()
    return rid


def list_due_reminders(
    conn: sqlite3.Connection,
    *,
    now_ts: Optional[float] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    ts = float(now_ts or time.time())
    rows = conn.execute(
        """
        SELECT reminder_id, principal_id, channel, delivery_target, message, due_at, status,
               source_task_id, metadata_json, created_at, updated_at
        FROM reminders
        WHERE status IN ('scheduled', 'awaiting_delivery_channel')
          AND due_at <= ?
        ORDER BY due_at ASC, created_at ASC
        LIMIT ?
        """,
        (ts, max(1, int(limit))),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw_meta = item.get("metadata_json")
        try:
            item["metadata"] = json.loads(raw_meta) if raw_meta else {}
        except Exception:
            item["metadata"] = {}
        out.append(item)
    return out


def update_reminder(
    conn: sqlite3.Connection,
    reminder_id: str,
    *,
    status: Optional[str] = None,
    delivery_target: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    rid = str(reminder_id or "").strip()
    if not rid:
        return
    row = conn.execute(
        "SELECT metadata_json FROM reminders WHERE reminder_id = ?",
        (rid,),
    ).fetchone()
    if not row:
        return
    try:
        merged = json.loads(row["metadata_json"] or "{}")
    except Exception:
        merged = {}
    if metadata:
        merged.update(metadata)
    ts = time.time()
    conn.execute(
        """
        UPDATE reminders
        SET status = COALESCE(?, status),
            delivery_target = COALESCE(?, delivery_target),
            metadata_json = ?,
            updated_at = ?
        WHERE reminder_id = ?
        """,
        (
            str(status).strip() if status is not None else None,
            str(delivery_target).strip() if delivery_target is not None else None,
            json.dumps(merged, ensure_ascii=False),
            ts,
            rid,
        ),
    )
    conn.commit()


def save_incident(conn: sqlite3.Connection, incident: Dict[str, Any]) -> str:
    payload = dict(incident)
    incident_id = str(payload.get("incident_id") or "").strip() or new_incident_id()
    ts = time.time()
    created_at = float(payload.get("timestamp") or payload.get("created_at") or ts)
    conn.execute(
        """
        INSERT INTO incidents(
            incident_id, source_task_id, source, error_type, status, summary, fingerprint,
            created_at, updated_at, incident_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(incident_id) DO UPDATE SET
            source_task_id = excluded.source_task_id,
            source = excluded.source,
            error_type = excluded.error_type,
            status = excluded.status,
            summary = excluded.summary,
            fingerprint = excluded.fingerprint,
            updated_at = excluded.updated_at,
            incident_json = excluded.incident_json
        """,
        (
            incident_id,
            str(payload.get("source_task_id") or "").strip(),
            str(payload.get("source") or "unknown").strip(),
            str(payload.get("error_type") or "unclear_or_unsafe").strip(),
            str(payload.get("status") or "open").strip(),
            _clip_text(payload.get("summary") or "", 500),
            str(payload.get("fingerprint") or "").strip(),
            created_at,
            ts,
            json.dumps({**payload, "incident_id": incident_id}, ensure_ascii=False, default=str),
        ),
    )
    conn.commit()
    return incident_id


def get_incident(conn: sqlite3.Connection, incident_id: str) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT incident_id, source_task_id, source, error_type, status, summary, fingerprint,
               created_at, updated_at, incident_json
        FROM incidents
        WHERE incident_id = ?
        """,
        (str(incident_id or "").strip(),),
    ).fetchone()
    if not row:
        return {}
    payload: Dict[str, Any]
    try:
        payload = json.loads(row["incident_json"] or "{}")
    except Exception:
        payload = {}
    return {
        **payload,
        "incident_id": str(row["incident_id"] or ""),
        "source_task_id": str(row["source_task_id"] or ""),
        "source": str(row["source"] or ""),
        "error_type": str(row["error_type"] or ""),
        "status": str(row["status"] or ""),
        "summary": str(row["summary"] or ""),
        "fingerprint": str(row["fingerprint"] or ""),
        "created_at": float(row["created_at"] or 0.0),
        "updated_at": float(row["updated_at"] or 0.0),
    }


def list_incidents(
    conn: sqlite3.Connection,
    *,
    status: str = "",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT incident_id FROM incidents
        WHERE (? = '' OR status = ?)
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (str(status or "").strip(), str(status or "").strip(), max(1, int(limit))),
    ).fetchall()
    return [get_incident(conn, str(row["incident_id"])) for row in rows]


def save_experience_run(conn: sqlite3.Connection, run: Dict[str, Any]) -> str:
    payload = dict(run)
    run_id = str(payload.get("run_id") or "").strip() or new_experience_run_id()
    ts = time.time()
    created_at = float(payload.get("created_at") or payload.get("started_at") or ts)
    checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
    conn.execute(
        """
        INSERT INTO experience_runs(
            run_id, actor, status, passed, summary, total_checks, failed_checks,
            average_score, created_at, updated_at, run_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(run_id) DO UPDATE SET
            actor = excluded.actor,
            status = excluded.status,
            passed = excluded.passed,
            summary = excluded.summary,
            total_checks = excluded.total_checks,
            failed_checks = excluded.failed_checks,
            average_score = excluded.average_score,
            updated_at = excluded.updated_at,
            run_json = excluded.run_json
        """,
        (
            run_id,
            str(payload.get("actor") or "").strip(),
            str(payload.get("status") or "completed").strip(),
            1 if bool(payload.get("passed")) else 0,
            str(payload.get("summary") or "").strip(),
            int(payload.get("total_checks") or len(checks)),
            int(payload.get("failed_checks") or 0),
            float(payload.get("average_score") or 0.0),
            created_at,
            ts,
            json.dumps({**payload, "run_id": run_id}, ensure_ascii=False, default=str),
        ),
    )
    conn.execute("DELETE FROM experience_checks WHERE run_id = ?", (run_id,))
    for idx, raw in enumerate(checks):
        row = dict(raw) if isinstance(raw, dict) else {"summary": str(raw)}
        scenario_id = str(row.get("scenario_id") or row.get("check_id") or f"scenario_{idx+1}").strip()
        check_key = f"{run_id}:{scenario_id or idx + 1}"
        row_created_at = float(row.get("started_at") or payload.get("started_at") or created_at)
        conn.execute(
            """
            INSERT INTO experience_checks(
                check_key, run_id, scenario_id, status, score, created_at, updated_at, check_json
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                check_key,
                run_id,
                scenario_id,
                "passed" if bool(row.get("passed")) else "failed",
                float(row.get("score") or 0.0),
                row_created_at,
                ts,
                json.dumps({**row, "check_key": check_key, "run_id": run_id}, ensure_ascii=False, default=str),
            ),
        )
    conn.commit()
    return run_id


def list_experience_checks(conn: sqlite3.Connection, run_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT check_key, created_at, updated_at, check_json
        FROM experience_checks
        WHERE run_id = ?
        ORDER BY updated_at DESC, scenario_id ASC
        """,
        (str(run_id or "").strip(),),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["check_json"] or "{}")
        except Exception:
            payload = {}
        payload["check_key"] = str(row["check_key"] or "")
        payload["created_at"] = float(row["created_at"] or 0.0)
        payload["updated_at"] = float(row["updated_at"] or 0.0)
        out.append(payload)
    return out


def get_experience_run(conn: sqlite3.Connection, run_id: str) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT run_id, actor, status, passed, summary, total_checks, failed_checks,
               average_score, created_at, updated_at, run_json
        FROM experience_runs
        WHERE run_id = ?
        LIMIT 1
        """,
        (str(run_id or "").strip(),),
    ).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(row["run_json"] or "{}")
    except Exception:
        payload = {}
    return {
        **payload,
        "run_id": str(row["run_id"] or ""),
        "actor": str(row["actor"] or ""),
        "status": str(row["status"] or ""),
        "passed": bool(row["passed"]),
        "summary": str(row["summary"] or ""),
        "total_checks": int(row["total_checks"] or 0),
        "failed_checks": int(row["failed_checks"] or 0),
        "average_score": float(row["average_score"] or 0.0),
        "created_at": float(row["created_at"] or 0.0),
        "updated_at": float(row["updated_at"] or 0.0),
        "checks": list_experience_checks(conn, str(row["run_id"] or "")),
    }


def list_experience_runs(
    conn: sqlite3.Connection,
    *,
    status: str = "",
    limit: int = 10,
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT run_id
        FROM experience_runs
        WHERE (? = '' OR status = ?)
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (str(status or "").strip(), str(status or "").strip(), max(1, int(limit))),
    ).fetchall()
    return [get_experience_run(conn, str(row["run_id"])) for row in rows]


def get_latest_experience_run(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = list_experience_runs(conn, limit=1)
    return rows[0] if rows else {}


def save_repair_attempt(conn: sqlite3.Connection, attempt: Dict[str, Any]) -> str:
    payload = dict(attempt)
    attempt_id = str(payload.get("attempt_id") or "").strip() or new_repair_attempt_id()
    ts = time.time()
    created_at = float(payload.get("created_at") or ts)
    conn.execute(
        """
        INSERT INTO repair_attempts(
            attempt_id, incident_id, attempt_no, stage, model_used, status, branch,
            worktree_path, created_at, updated_at, attempt_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(attempt_id) DO UPDATE SET
            incident_id = excluded.incident_id,
            attempt_no = excluded.attempt_no,
            stage = excluded.stage,
            model_used = excluded.model_used,
            status = excluded.status,
            branch = excluded.branch,
            worktree_path = excluded.worktree_path,
            updated_at = excluded.updated_at,
            attempt_json = excluded.attempt_json
        """,
        (
            attempt_id,
            str(payload.get("incident_id") or "").strip(),
            int(payload.get("attempt_number") or payload.get("attempt_no") or 0),
            str(payload.get("stage") or "").strip(),
            str(payload.get("model_used") or "").strip(),
            str(payload.get("status") or "pending").strip(),
            str(payload.get("branch") or "").strip(),
            str(payload.get("worktree_path") or "").strip(),
            created_at,
            ts,
            json.dumps({**payload, "attempt_id": attempt_id}, ensure_ascii=False, default=str),
        ),
    )
    conn.commit()
    return attempt_id


def list_repair_attempts(conn: sqlite3.Connection, incident_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT attempt_id, created_at, updated_at, attempt_json
        FROM repair_attempts
        WHERE incident_id = ?
        ORDER BY attempt_no ASC, updated_at ASC
        """,
        (str(incident_id or "").strip(),),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["attempt_json"] or "{}")
        except Exception:
            payload = {}
        payload["attempt_id"] = str(row["attempt_id"] or "")
        payload["created_at"] = float(row["created_at"] or 0.0)
        payload["updated_at"] = float(row["updated_at"] or 0.0)
        out.append(payload)
    return out


def save_repair_plan(conn: sqlite3.Connection, plan: Dict[str, Any]) -> str:
    payload = dict(plan)
    plan_id = str(payload.get("plan_id") or "").strip() or new_repair_plan_id()
    ts = time.time()
    created_at = float(payload.get("created_at") or ts)
    conn.execute(
        """
        INSERT INTO repair_plans(
            plan_id, incident_id, status, model_used, created_at, updated_at, plan_json
        ) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(plan_id) DO UPDATE SET
            incident_id = excluded.incident_id,
            status = excluded.status,
            model_used = excluded.model_used,
            updated_at = excluded.updated_at,
            plan_json = excluded.plan_json
        """,
        (
            plan_id,
            str(payload.get("incident_id") or "").strip(),
            str(payload.get("status") or "planned").strip(),
            str(payload.get("model_used") or "").strip(),
            created_at,
            ts,
            json.dumps({**payload, "plan_id": plan_id}, ensure_ascii=False, default=str),
        ),
    )
    conn.commit()
    return plan_id


def get_latest_repair_plan(conn: sqlite3.Connection, incident_id: str) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT plan_id, created_at, updated_at, plan_json
        FROM repair_plans
        WHERE incident_id = ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (str(incident_id or "").strip(),),
    ).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(row["plan_json"] or "{}")
    except Exception:
        payload = {}
    payload["plan_id"] = str(row["plan_id"] or "")
    payload["created_at"] = float(row["created_at"] or 0.0)
    payload["updated_at"] = float(row["updated_at"] or 0.0)
    return payload


def count_principals(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM principals").fetchone()
    return int(row["n"] or 0) if row else 0


def count_active_memories(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM principal_memories WHERE is_active = 1"
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def count_pending_reminders(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM reminders WHERE status IN ('scheduled', 'awaiting_delivery_channel')"
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def count_due_reminders(conn: sqlite3.Connection, *, now_ts: Optional[float] = None) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM reminders
        WHERE status IN ('scheduled', 'awaiting_delivery_channel')
          AND due_at <= ?
        """,
        (float(now_ts or time.time()),),
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def load_recent_telegram_history(
    conn: sqlite3.Connection,
    chat_id: Any,
    *,
    limit_turns: int = 6,
    exclude_task_id: Optional[str] = None,
) -> List[Dict[str, str]]:
    if chat_id is None:
        return []
    cid = str(chat_id).strip()
    params: List[Any] = [Channel.TELEGRAM.value, EventType.USER_MESSAGE.value, cid]
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
          AND CAST(json_extract(e.payload_json, '$.chat_id') AS TEXT) = ?
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
                    assistant_text = _clip_text(f"Completed: {payload.get('summary')}")
                    assistant_source = "openclaw_cursor"
                elif backend == "openclaw":
                    assistant_text = _clip_text(f"Completed: {payload.get('summary')}")
                    assistant_source = "openclaw"
                else:
                    assistant_text = _clip_text(f"Completed: {payload.get('summary')}")
                    assistant_source = "cursor"
            elif et_raw == EventType.JOB_FAILED.value:
                detail = payload.get("message") or payload.get("error")
                if detail:
                    backend = str(payload.get("backend") or "").strip()
                    if backend == "openclaw":
                        assistant_text = _clip_text(f"Could not finish: {detail}")
                        assistant_source = "openclaw"
                    else:
                        assistant_text = _clip_text(f"Could not finish: {detail}")
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

"""Command bus: validate commands, enforce idempotency, append events."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any, Dict, Optional, Set, Tuple

from .kill_switch import engage_kill_switch, is_kill_switch_engaged, release_kill_switch
from .policy import META_DIGEST_KEY, META_DIGEST_TS_KEY
from .schema import (
    Channel,
    CommandEnvelope,
    CommandType,
    EventType,
    new_task_id,
    normalize_scoped_idempotency_key,
    validate_command_type,
)
from .store import (
    SYSTEM_TASK_ID,
    append_event,
    claim_idempotency_and_create_task,
    claim_scoped_idempotency,
    ensure_system_task,
    set_meta,
    task_exists,
)

_ADMIN_COMMAND_TYPES: Set[CommandType] = {
    CommandType.PUBLISH_CAPABILITY_SNAPSHOT,
    CommandType.KILL_SWITCH_ENGAGE,
    CommandType.KILL_SWITCH_RELEASE,
}


def handle_command(conn: sqlite3.Connection, body: Dict[str, Any]) -> Dict[str, Any]:
    """Process one command envelope. Returns JSON-serializable result."""
    ct_raw = body.get("command_type") or body.get("type")
    if not ct_raw:
        return {"ok": False, "error": "missing command_type"}
    try:
        ctype = validate_command_type(str(ct_raw))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    ch_raw = body.get("channel") or "cli"
    try:
        channel = Channel(str(ch_raw))
    except ValueError:
        return {"ok": False, "error": f"unknown channel: {ch_raw}"}
    if ctype in _ADMIN_COMMAND_TYPES and channel != Channel.INTERNAL:
        return {"ok": False, "error": "admin commands require channel=internal"}
    if is_kill_switch_engaged(conn) and ctype not in (CommandType.KILL_SWITCH_RELEASE,):
        return {"ok": False, "error": "kill_switch_engaged"}
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    task_id_in = body.get("task_id")
    ext_id = body.get("external_id")
    idem_in = body.get("idempotency_key")

    env = CommandEnvelope(
        command_type=ctype,
        channel=channel,
        payload=payload,
        task_id=str(task_id_in) if task_id_in else None,
        idempotency_key=str(idem_in) if idem_in else None,
        external_id=str(ext_id) if ext_id else None,
    )
    idem = env.resolved_idempotency_key()

    if ctype == CommandType.REPORT_CURSOR_EVENT:
        return _handle_cursor_report(conn, env, idem)
    if ctype == CommandType.CREATE_CURSOR_JOB:
        return _handle_create_cursor_job(conn, env, idem)
    if ctype == CommandType.CURSOR_FOLLOWUP:
        return _handle_cursor_followup(conn, env)
    if ctype == CommandType.CURSOR_STOP:
        return _handle_cursor_stop(conn, env)
    if ctype == CommandType.SUBMIT_USER_MESSAGE:
        return _handle_user_message(conn, env, idem)
    if ctype == CommandType.CREATE_TASK:
        return _handle_create_task(conn, env, idem)
    if ctype == CommandType.ALEXA_UTTERANCE:
        return _handle_alexa(conn, env, idem)
    if ctype == CommandType.PUBLISH_CAPABILITY_SNAPSHOT:
        return _handle_publish_capability_snapshot(conn, env)
    if ctype == CommandType.KILL_SWITCH_ENGAGE:
        return _handle_kill_switch_engage(conn, env)
    if ctype == CommandType.KILL_SWITCH_RELEASE:
        return _handle_kill_switch_release(conn, env)
    return {"ok": False, "error": f"unhandled command_type: {ctype.value}"}


def _ensure_task(
    conn: sqlite3.Connection, env: CommandEnvelope, idem: str
) -> Tuple[Optional[str], bool, bool]:
    """
    Returns (task_id_or_none, created_new_task_body, deduped).
    """
    if env.task_id:
        tid = env.task_id
        if not task_exists(conn, tid):
            return None, False, False
        return tid, False, False

    candidate = new_task_id()
    tid, fresh = claim_idempotency_and_create_task(
        conn,
        idem,
        candidate,
        env.channel.value,
    )
    if not fresh:
        return tid, False, True
    return tid, True, False


def _append(
    conn: sqlite3.Connection, task_id: str, et: EventType, payload: Dict[str, Any]
) -> int:
    return append_event(conn, task_id, et, payload)


def _user_message_scoped_idempotency_key(env: CommandEnvelope) -> Optional[str]:
    """Stable dedupe key for SubmitUserMessage on an existing task (e.g. Telegram retries)."""
    if not env.task_id:
        return None
    if env.idempotency_key and str(env.idempotency_key).strip():
        return normalize_scoped_idempotency_key(
            str(env.task_id),
            str(env.idempotency_key).strip(),
            env.command_type.value,
        )
    ext = (env.external_id or "").strip()
    if ext:
        return normalize_scoped_idempotency_key(
            env.channel.value,
            f"{ext}|{env.task_id}",
            env.command_type.value,
        )
    return None


def _handle_create_task(
    conn: sqlite3.Connection, env: CommandEnvelope, idem: str
) -> Dict[str, Any]:
    tid, is_new, deduped = _ensure_task(conn, env, idem)
    if tid is None:
        return {"ok": False, "error": f"unknown task_id: {env.task_id}"}
    if deduped:
        _append(
            conn,
            tid,
            EventType.COMMAND_DEDUPED,
            {"command_type": env.command_type.value, "idempotency_key": idem},
        )
        return {"ok": True, "task_id": tid, "deduped": True}
    if is_new:
        _append(
            conn,
            tid,
            EventType.COMMAND_RECEIVED,
            {"command_type": env.command_type.value, "channel": env.channel.value},
        )
        _append(
            conn,
            tid,
            EventType.TASK_CREATED,
            {
                "summary": env.payload.get("summary", ""),
                "channel": env.channel.value,
            },
        )
    else:
        _append(
            conn,
            tid,
            EventType.COMMAND_RECEIVED,
            {"command_type": env.command_type.value, "channel": env.channel.value},
        )
    return {"ok": True, "task_id": tid, "deduped": False}


def _handle_user_message(
    conn: sqlite3.Connection, env: CommandEnvelope, idem: str
) -> Dict[str, Any]:
    if not env.task_id:
        # Without external_id / idempotency_key, do not collapse all channel messages into one task.
        idem_use = idem
        if not (env.external_id or "").strip() and not (env.idempotency_key or "").strip():
            idem_use = normalize_scoped_idempotency_key(
                "submit_user_message",
                uuid.uuid4().hex,
                env.command_type.value,
            )
        tid, is_new, deduped = _ensure_task(conn, env, idem_use)
        if tid is None:
            return {"ok": False, "error": "task resolution failed"}
        if deduped:
            _append(
                conn,
                tid,
                EventType.COMMAND_DEDUPED,
                {"command_type": env.command_type.value, "idempotency_key": idem_use},
            )
            return {"ok": True, "task_id": tid, "deduped": True}
        summary = str(
            env.payload.get("routing_text")
            or env.payload.get("text", "")
            or env.payload.get("summary", "")
        )[:120]
        if is_new:
            _append(
                conn,
                tid,
                EventType.COMMAND_RECEIVED,
                {"command_type": env.command_type.value, "channel": env.channel.value},
            )
            _append(
                conn,
                tid,
                EventType.TASK_CREATED,
                {"summary": summary, "channel": env.channel.value},
            )
        um: Dict[str, Any] = {
            "text": env.payload.get("text", ""),
            "routing_text": env.payload.get("routing_text", ""),
            "mention_targets": env.payload.get("mention_targets", []),
            "model_mentions": env.payload.get("model_mentions", []),
            "preferred_model_family": env.payload.get("preferred_model_family", ""),
            "preferred_model_label": env.payload.get("preferred_model_label", ""),
            "routing_hint": env.payload.get("routing_hint", ""),
            "collaboration_mode": env.payload.get("collaboration_mode", ""),
            "visibility_mode": env.payload.get("visibility_mode", ""),
            "channel": env.channel.value,
            "chat_id": env.payload.get("chat_id"),
            "chat_type": env.payload.get("chat_type"),
            "message_id": env.payload.get("message_id"),
            "from_user": env.payload.get("from_user"),
            "from_username": env.payload.get("from_username"),
        }
        if env.payload.get("message_thread_id") is not None:
            um["message_thread_id"] = env.payload.get("message_thread_id")
        if env.payload.get("telegram_continuation"):
            um["telegram_continuation"] = True
        if env.payload.get("telegram_continuation_anchor_message_id") is not None:
            um["telegram_continuation_anchor_message_id"] = env.payload.get(
                "telegram_continuation_anchor_message_id"
            )
        _append(conn, tid, EventType.USER_MESSAGE, um)
        if env.external_id:
            _append(
                conn,
                tid,
                EventType.EXTERNAL_REF,
                {"kind": f"{env.channel.value}_update", "ref": env.external_id},
            )
        queued_cursor_job = (
            env.channel == Channel.TELEGRAM
            and bool(env.payload.get("auto_cursor_job", False))
        )
        if queued_cursor_job:
            _append(
                conn,
                tid,
                EventType.JOB_QUEUED,
                {
                    "kind": "cursor",
                    "prompt_excerpt": str(
                        env.payload.get("routing_text") or env.payload.get("text", "")
                    )[:300],
                    "source": "telegram_default",
                },
            )
        return {
            "ok": True,
            "task_id": tid,
            "deduped": False,
            "queued_cursor_job": queued_cursor_job,
        }
    tid = env.task_id
    if not task_exists(conn, tid):
        return {"ok": False, "error": f"unknown task_id: {tid}"}
    scoped = _user_message_scoped_idempotency_key(env)
    if scoped:
        outcome = claim_scoped_idempotency(conn, scoped, tid)
        if outcome == "conflict":
            return {"ok": False, "error": "idempotency_key_conflict"}
        if outcome == "duplicate":
            _append(
                conn,
                tid,
                EventType.COMMAND_DEDUPED,
                {
                    "command_type": env.command_type.value,
                    "idempotency_key": scoped,
                    "scoped": True,
                },
            )
            return {"ok": True, "task_id": tid, "deduped": True}
    _append(
        conn,
        tid,
        EventType.COMMAND_RECEIVED,
        {"command_type": env.command_type.value},
    )
    um2: Dict[str, Any] = {
        "text": env.payload.get("text", ""),
        "routing_text": env.payload.get("routing_text", ""),
        "mention_targets": env.payload.get("mention_targets", []),
        "model_mentions": env.payload.get("model_mentions", []),
        "preferred_model_family": env.payload.get("preferred_model_family", ""),
        "preferred_model_label": env.payload.get("preferred_model_label", ""),
        "routing_hint": env.payload.get("routing_hint", ""),
        "collaboration_mode": env.payload.get("collaboration_mode", ""),
        "visibility_mode": env.payload.get("visibility_mode", ""),
        "channel": env.channel.value,
        "chat_id": env.payload.get("chat_id"),
        "chat_type": env.payload.get("chat_type"),
        "message_id": env.payload.get("message_id"),
        "from_user": env.payload.get("from_user"),
        "from_username": env.payload.get("from_username"),
    }
    if env.payload.get("message_thread_id") is not None:
        um2["message_thread_id"] = env.payload.get("message_thread_id")
    if env.payload.get("telegram_continuation"):
        um2["telegram_continuation"] = True
    if env.payload.get("telegram_continuation_anchor_message_id") is not None:
        um2["telegram_continuation_anchor_message_id"] = env.payload.get(
            "telegram_continuation_anchor_message_id"
        )
    _append(conn, tid, EventType.USER_MESSAGE, um2)
    if env.external_id:
        _append(
            conn,
            tid,
            EventType.EXTERNAL_REF,
            {
                "kind": f"{env.channel.value}_update",
                "ref": env.external_id,
            },
        )
    return {"ok": True, "task_id": tid, "deduped": False}


def _handle_create_cursor_job(
    conn: sqlite3.Connection, env: CommandEnvelope, idem: str
) -> Dict[str, Any]:
    tid, is_new, deduped = _ensure_task(conn, env, idem)
    if tid is None:
        return {"ok": False, "error": f"unknown task_id: {env.task_id}"}
    if deduped:
        _append(
            conn,
            tid,
            EventType.COMMAND_DEDUPED,
            {
                "command_type": env.command_type.value,
                "idempotency_key": idem,
            },
        )
        return {"ok": True, "task_id": tid, "deduped": True}
    if is_new:
        _append(
            conn,
            tid,
            EventType.COMMAND_RECEIVED,
            {"command_type": env.command_type.value},
        )
        _append(
            conn,
            tid,
            EventType.TASK_CREATED,
            {"summary": env.payload.get("summary", "cursor job"), "channel": env.channel.value},
        )
    agent_hint = env.payload.get("cursor_agent_id")
    _append(
        conn,
        tid,
        EventType.JOB_QUEUED,
        {
            "kind": "cursor",
            "prompt_excerpt": str(env.payload.get("prompt", ""))[:300],
            "cursor_agent_id": agent_hint,
        },
    )
    return {"ok": True, "task_id": tid, "deduped": False}


def _handle_cursor_followup(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    if not env.task_id:
        return {"ok": False, "error": "task_id required"}
    tid = env.task_id
    if not task_exists(conn, tid):
        return {"ok": False, "error": f"unknown task_id: {tid}"}
    _append(conn, tid, EventType.JOB_PROGRESS, {"message": "followup", "detail": env.payload})
    return {"ok": True, "task_id": tid}


def _handle_cursor_stop(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    if not env.task_id:
        return {"ok": False, "error": "task_id required"}
    tid = env.task_id
    if not task_exists(conn, tid):
        return {"ok": False, "error": f"unknown task_id: {tid}"}
    _append(conn, tid, EventType.JOB_FAILED, {"error": "stopped_by_user", "detail": env.payload})
    return {"ok": True, "task_id": tid}


def _handle_cursor_report(conn: sqlite3.Connection, env: CommandEnvelope, idem: str) -> Dict[str, Any]:
    """Internal: map cursor_openclaw lifecycle JSON into events."""
    tid = env.task_id or env.payload.get("task_id")
    if not tid:
        return {"ok": False, "error": "task_id required in payload or envelope"}
    tid = str(tid)
    if not task_exists(conn, tid):
        return {"ok": False, "error": f"unknown task_id: {tid}"}
    et_raw = env.payload.get("event_type") or env.payload.get("cursor_event")
    if not et_raw:
        return {"ok": False, "error": "payload.event_type required"}
    try:
        cet = EventType(str(et_raw))
    except ValueError:
        return {"ok": False, "error": f"invalid event_type: {et_raw}"}
    if env.payload.get("payload") is not None and not isinstance(env.payload.get("payload"), dict):
        return {"ok": False, "error": "payload.payload must be a JSON object"}
    inner = env.payload.get("payload") if isinstance(env.payload.get("payload"), dict) else {}
    inner_canon = json.dumps(inner, sort_keys=True, ensure_ascii=False, default=str)
    inner_hash = hashlib.sha256(inner_canon.encode("utf-8")).hexdigest()[:32]
    report_key = normalize_scoped_idempotency_key(
        str(tid), f"{cet.value}|{inner_hash}", "ReportCursorEvent"
    )
    outcome = claim_scoped_idempotency(conn, report_key, tid)
    if outcome == "conflict":
        return {"ok": False, "error": "cursor_report_idempotency_conflict"}
    if outcome == "duplicate":
        _append(
            conn,
            tid,
            EventType.COMMAND_DEDUPED,
            {
                "command_type": env.command_type.value,
                "idempotency_key": report_key,
                "scoped": True,
            },
        )
        return {"ok": True, "task_id": tid, "deduped": True}
    _append(conn, tid, EventType.COMMAND_RECEIVED, {"command_type": "ReportCursorEvent"})
    _append(conn, tid, cet, inner)
    return {"ok": True, "task_id": tid}


def _handle_alexa(conn: sqlite3.Connection, env: CommandEnvelope, idem: str) -> Dict[str, Any]:
    text = str(env.payload.get("utterance") or env.payload.get("text") or "").strip()
    routing_text = str(env.payload.get("routing_text") or text).strip()
    fake = CommandEnvelope(
        command_type=CommandType.CREATE_TASK,
        channel=Channel.ALEXA,
        payload={"summary": routing_text[:200] or text[:200] or "alexa"},
        external_id=env.external_id,
        idempotency_key=env.idempotency_key,
    )
    idem_use = fake.resolved_idempotency_key()
    tid, is_new, deduped = _ensure_task(conn, fake, idem_use)
    if tid is None:
        return {"ok": False, "error": "task resolution failed"}
    if deduped:
        _append(
            conn,
            tid,
            EventType.COMMAND_DEDUPED,
            {"command_type": env.command_type.value},
        )
        return {"ok": True, "task_id": tid, "deduped": True}
    if is_new:
        _append(
            conn,
            tid,
            EventType.COMMAND_RECEIVED,
            {"command_type": env.command_type.value, "channel": Channel.ALEXA.value},
        )
        _append(
            conn,
            tid,
            EventType.TASK_CREATED,
            {"summary": routing_text[:200] or text[:200] or "alexa", "channel": Channel.ALEXA.value},
        )
    if text:
        _append(
            conn,
            tid,
            EventType.USER_MESSAGE,
            {
                "text": text,
                "routing_text": routing_text,
                "channel": Channel.ALEXA.value,
                "session_id": env.payload.get("session_id"),
                "request_id": env.payload.get("request_id"),
                "intent_name": env.payload.get("intent_name"),
                "locale": env.payload.get("locale"),
                "user_id": env.payload.get("user_id"),
                "device_id": env.payload.get("device_id"),
            },
        )
    return {"ok": True, "task_id": tid, "deduped": False}


def _handle_publish_capability_snapshot(
    conn: sqlite3.Connection, env: CommandEnvelope
) -> Dict[str, Any]:
    ensure_system_task(conn)
    blob = dict(env.payload)
    blob["published_ts"] = time.time()
    set_meta(conn, META_DIGEST_KEY, json.dumps(blob, ensure_ascii=False))
    set_meta(conn, META_DIGEST_TS_KEY, str(blob["published_ts"]))
    rows = blob.get("rows") if isinstance(blob.get("rows"), list) else []
    excerpt = json.dumps({"summary": blob.get("summary"), "row_count": len(rows)})[
        :480
    ]
    _append(
        conn,
        SYSTEM_TASK_ID,
        EventType.CAPABILITY_SNAPSHOT,
        {"summary_json_excerpt": excerpt, "channel": env.channel.value},
    )
    return {"ok": True, "published_ts": blob["published_ts"]}


def _handle_kill_switch_engage(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    ensure_system_task(conn)
    engage_kill_switch(
        conn,
        reason=str(env.payload.get("reason") or ""),
        source=str(env.payload.get("source") or env.channel.value),
    )
    _append(
        conn,
        SYSTEM_TASK_ID,
        EventType.KILL_SWITCH_ENGAGED,
        {"reason": env.payload.get("reason", ""), "source": env.channel.value},
    )
    return {"ok": True, "kill_switch": True}


def _handle_kill_switch_release(conn: sqlite3.Connection, env: CommandEnvelope) -> Dict[str, Any]:
    ensure_system_task(conn)
    release_kill_switch(conn)
    _append(
        conn,
        SYSTEM_TASK_ID,
        EventType.KILL_SWITCH_RELEASED,
        {"source": env.channel.value},
    )
    return {"ok": True, "kill_switch": False}


def append_internal_event(
    conn: sqlite3.Connection,
    task_id: str,
    event_type: EventType,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    if not task_exists(conn, task_id):
        return {"ok": False, "error": f"unknown task_id: {task_id}"}
    seq = _append(conn, task_id, event_type, payload)
    return {"ok": True, "task_id": task_id, "seq": seq}

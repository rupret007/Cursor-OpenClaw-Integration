"""Telegram split-message continuation: coalesce follow-up chunks onto one active task."""
from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from .adapters.telegram import MENTION_RE
from .andrea_router import is_standalone_casual_social_turn
from .projector import project_task_dict
from .schema import TaskStatus
from .store import (
    get_task_principal_id,
    get_task_updated_at,
    list_recent_telegram_task_ids,
    list_telegram_task_ids_for_chat,
)

_ACTIVE_STATUSES = frozenset(
    {
        TaskStatus.CREATED,
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        TaskStatus.AWAITING_APPROVAL,
    }
)

_STR_MERGE_KEYS = (
    "routing_hint",
    "collaboration_mode",
    "visibility_mode",
    "requested_execution_mode",
    "preferred_model_family",
    "preferred_model_label",
)
_LIST_MERGE_KEYS = ("mention_targets", "model_mentions")


def _chat_matches(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    return str(a) == str(b)


def _user_matches(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _thread_matches(prev_telegram_meta: Dict[str, Any], new_payload: Dict[str, Any]) -> bool:
    """Forum topics: require same message_thread_id when either side has one."""
    p = prev_telegram_meta.get("message_thread_id")
    n = new_payload.get("message_thread_id")
    if p is not None and n is not None:
        return str(p) == str(n)
    if p is not None and n is None:
        return False
    if p is None and n is not None:
        return False
    return True


def _parse_status(raw: Any) -> Optional[TaskStatus]:
    if raw is None:
        return None
    try:
        return TaskStatus(str(raw))
    except ValueError:
        return None


def _should_continue_message(new_payload: Dict[str, Any], prev_telegram_meta: Dict[str, Any]) -> bool:
    """Heuristic: continuation chunk vs a brand-new routed request."""
    social_line = str(
        new_payload.get("routing_text") or new_payload.get("text") or ""
    ).strip()
    if is_standalone_casual_social_turn(social_line):
        return False
    new_text = str(new_payload.get("text") or "")
    reply_to_message_id = new_payload.get("reply_to_message_id")
    anchor_message_id = prev_telegram_meta.get("message_id")
    if reply_to_message_id is not None and anchor_message_id is not None:
        if str(reply_to_message_id) == str(anchor_message_id):
            return True
    # Question-like text with no mention: treat as new question unless explicitly replying to anchor.
    # Prevents "Is this OpenClaw?" from merging onto a just-created technical task.
    if "?" in new_text and not MENTION_RE.search(new_text):
        return False
    if not MENTION_RE.search(new_text):
        return True
    new_mentions = set(new_payload.get("mention_targets") or [])
    prev_raw = prev_telegram_meta.get("mention_targets")
    prev_mentions = set(prev_raw) if isinstance(prev_raw, list) else set()
    return new_mentions == prev_mentions


def _scan_candidates(
    conn: Any,
    ordered_ids: list[str],
    chat_id: Any,
    new_payload: Dict[str, Any],
    *,
    now: float,
    window_sec: float,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    from_user = new_payload.get("from_user")
    seen: set[str] = set()
    for tid in ordered_ids:
        if tid in seen:
            continue
        seen.add(tid)
        updated_at = get_task_updated_at(conn, tid)
        if updated_at is not None and (now - updated_at) > window_sec:
            continue
        proj = project_task_dict(conn, tid, "telegram")
        st = _parse_status(proj.get("status"))
        if st is None or st not in _ACTIVE_STATUSES:
            continue
        meta = proj.get("meta") if isinstance(proj.get("meta"), dict) else {}
        tg = meta.get("telegram")
        if not isinstance(tg, dict):
            continue
        if not _chat_matches(tg.get("chat_id"), chat_id):
            continue
        if not _user_matches(from_user, tg.get("from_user")):
            continue
        if not _thread_matches(tg, new_payload):
            continue
        if st is not TaskStatus.CREATED and not MENTION_RE.search(str(new_payload.get("text") or "")):
            reply_to_message_id = new_payload.get("reply_to_message_id")
            anchor_message_id = tg.get("message_id")
            if reply_to_message_id is not None and anchor_message_id is not None:
                if str(reply_to_message_id) != str(anchor_message_id):
                    continue
            elif "?" in str(new_payload.get("text") or ""):
                continue
        return tid, tg
    return None


def _find_continuation_candidate(
    conn: Any,
    chat_id: Any,
    new_payload: Dict[str, Any],
    *,
    now: float,
    window_sec: float,
    scan_limit: int,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    per_chat = list_telegram_task_ids_for_chat(conn, chat_id, limit=scan_limit)
    global_ids = list_recent_telegram_task_ids(conn, scan_limit)
    ordered: list[str] = []
    seen: set[str] = set()
    for tid in per_chat + global_ids:
        if tid not in seen:
            seen.add(tid)
            ordered.append(tid)
        if len(ordered) >= scan_limit * 2:
            break
    return _scan_candidates(
        conn, ordered[: scan_limit * 2], chat_id, new_payload, now=now, window_sec=window_sec
    )


def _merge_anchor_routing(payload: Dict[str, Any], prev_telegram_meta: Dict[str, Any]) -> None:
    """Preserve collaboration / routing from the first chunk of a split prompt."""
    default_values = {
        "routing_hint": "auto",
        "collaboration_mode": "auto",
        "visibility_mode": "summary",
        "requested_execution_mode": "",
        "preferred_model_family": "",
        "preferred_model_label": "",
    }
    for key in _STR_MERGE_KEYS:
        current = payload.get(key)
        default = default_values.get(key, "")
        if current is not None and str(current).strip() not in {"", default}:
            continue
        val = prev_telegram_meta.get(key)
        if val is not None and str(val).strip() != "":
            payload[key] = val
    for key in _LIST_MERGE_KEYS:
        current = payload.get(key)
        if isinstance(current, list) and current:
            continue
        val = prev_telegram_meta.get(key)
        if isinstance(val, list) and val:
            payload[key] = list(val)


def attach_continuation_if_applicable(conn: Any, cmd: Dict[str, Any]) -> bool:
    """
    If this Telegram SubmitUserMessage should continue the latest active task in the
    same chat, set cmd['task_id'] and merge routing metadata. Returns True when attached.
    """
    if str(cmd.get("command_type") or "") != "SubmitUserMessage":
        return False
    if str(cmd.get("channel") or "") != "telegram":
        return False
    if cmd.get("task_id"):
        return False
    payload = cmd.get("payload")
    if not isinstance(payload, dict):
        return False
    chat_id = payload.get("chat_id")
    if chat_id is None:
        return False

    social_line = str(payload.get("routing_text") or payload.get("text") or "").strip()
    if is_standalone_casual_social_turn(social_line):
        return False

    window_sec = float(os.environ.get("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS", "180"))
    scan_limit = int(os.environ.get("ANDREA_TELEGRAM_CONTINUATION_SCAN_LIMIT", "25"))
    now = time.time()
    found = _find_continuation_candidate(
        conn,
        chat_id,
        payload,
        now=now,
        window_sec=window_sec,
        scan_limit=scan_limit,
    )
    if not found:
        return False
    tid, prev_tg = found
    if not _should_continue_message(payload, prev_tg):
        return False

    _merge_anchor_routing(payload, prev_tg)
    payload["telegram_continuation"] = True
    anchor_mid = prev_tg.get("message_id")
    if anchor_mid is not None:
        payload["telegram_continuation_anchor_message_id"] = anchor_mid
    cmd["task_id"] = tid
    cmd["payload"] = payload
    try:
        from .schema import EventType
        from .store import append_event, insert_continuation_record

        cid = f"cont-{int(now * 1000)}-{uuid.uuid4().hex[:10]}"
        principal_id = str(get_task_principal_id(conn, tid) or "")
        insert_continuation_record(
            conn,
            continuation_id=cid,
            principal_id=principal_id,
            source_channel="telegram",
            source_task_id="",
            linked_task_id=tid,
            reason="telegram_thread_continuation",
            confidence_band="heuristic_v1",
            payload={
                "chat_id": chat_id,
                "anchor_message_id": anchor_mid,
            },
        )
        append_event(
            conn,
            tid,
            EventType.CONTINUATION_RECORDED,
            {
                "continuation_id": cid,
                "linked_task_id": tid,
                "principal_id": principal_id,
                "reason": "telegram_thread_continuation",
                "confidence_band": "heuristic_v1",
                "source_channel": "telegram",
            },
        )
        try:
            from .assistant_followthrough import on_telegram_continuation_recorded

            on_telegram_continuation_recorded(
                conn, task_id=tid, continuation_id=cid, principal_id=principal_id
            )
        except Exception:
            pass
    except Exception:
        pass
    return True

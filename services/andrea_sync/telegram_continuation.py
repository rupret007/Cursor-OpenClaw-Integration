"""Telegram split-message continuation: coalesce follow-up chunks onto one active task."""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional, Tuple

from .adapters.telegram import MENTION_RE
from .projector import project_task_dict
from .schema import TaskStatus
from .store import get_task_updated_at, list_recent_telegram_task_ids

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
    "preferred_model_family",
    "preferred_model_label",
)
_LIST_MERGE_KEYS = ("mention_targets", "model_mentions")


def _chat_matches(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    return str(a) == str(b)


def _user_matches(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return True
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _parse_status(raw: Any) -> Optional[TaskStatus]:
    if raw is None:
        return None
    try:
        return TaskStatus(str(raw))
    except ValueError:
        return None


def _should_continue_message(new_payload: Dict[str, Any], prev_telegram_meta: Dict[str, Any]) -> bool:
    """Heuristic: continuation chunk vs a brand-new routed request."""
    new_text = str(new_payload.get("text") or "")
    if not MENTION_RE.search(new_text):
        return True
    new_mentions = set(new_payload.get("mention_targets") or [])
    prev_raw = prev_telegram_meta.get("mention_targets")
    prev_mentions = set(prev_raw) if isinstance(prev_raw, list) else set()
    return new_mentions <= prev_mentions


def _find_continuation_candidate(
    conn: Any,
    chat_id: Any,
    from_user: Any,
    *,
    now: float,
    window_sec: float,
    scan_limit: int,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    for tid in list_recent_telegram_task_ids(conn, scan_limit):
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
        return tid, tg
    return None


def _merge_anchor_routing(payload: Dict[str, Any], prev_telegram_meta: Dict[str, Any]) -> None:
    """Preserve collaboration / routing from the first chunk of a split prompt."""
    for key in _STR_MERGE_KEYS:
        val = prev_telegram_meta.get(key)
        if val is not None and str(val).strip() != "":
            payload[key] = val
    for key in _LIST_MERGE_KEYS:
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

    window_sec = float(os.environ.get("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS", "180"))
    scan_limit = int(os.environ.get("ANDREA_TELEGRAM_CONTINUATION_SCAN_LIMIT", "25"))
    now = time.time()
    found = _find_continuation_candidate(
        conn,
        chat_id,
        payload.get("from_user"),
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
    return True

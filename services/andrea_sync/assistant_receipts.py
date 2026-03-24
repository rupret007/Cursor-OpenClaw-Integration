"""User-facing outcome receipts for the Trusted Daily Assistant pack (Stage A)."""
from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, Optional

import sqlite3

from .assistant_domain_rollout import (
    DAILY_ASSISTANT_SCENARIO_IDS,
    TRUSTED_DAILY_ASSISTANT_PACK_ID,
    is_daily_assistant_scenario,
)
from .schema import EventType


def receipts_enabled() -> bool:
    return os.environ.get("ANDREA_DAILY_PACK_RECEIPTS_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _new_receipt_id() -> str:
    return f"rcpt_{uuid.uuid4().hex[:18]}"


def record_user_facing_receipt(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    scenario_id: str,
    receipt_kind: str,
    summary: str,
    goal_id: str = "",
    proof_refs: Optional[Dict[str, Any]] = None,
    delivery_state: str = "",
    next_step: str = "",
    pass_hint: bool = True,
    payload: Optional[Dict[str, Any]] = None,
    reply_reason: str = "",
) -> Optional[Dict[str, Any]]:
    """Persist ledger row + task event USER_OUTCOME_RECEIPT_RECORDED."""
    if not receipts_enabled():
        return None
    sid = str(scenario_id or "").strip()
    if not is_daily_assistant_scenario(sid):
        return None
    from .store import insert_user_outcome_receipt, append_event

    rid = _new_receipt_id()
    ts = time.time()
    insert_user_outcome_receipt(
        conn,
        receipt_id=rid,
        task_id=str(task_id or ""),
        goal_id=str(goal_id or ""),
        scenario_id=sid,
        pack_id=TRUSTED_DAILY_ASSISTANT_PACK_ID,
        receipt_kind=str(receipt_kind or "")[:120],
        summary=str(summary or "")[:4000],
        proof_refs=proof_refs or {},
        delivery_state=str(delivery_state or "")[:120],
        next_step=str(next_step or "")[:2000],
        pass_hint=pass_hint,
        created_at=ts,
        payload=payload or {},
    )
    event_payload = {
        "receipt_id": rid,
        "task_id": str(task_id or ""),
        "goal_id": str(goal_id or ""),
        "scenario_id": sid,
        "pack_id": TRUSTED_DAILY_ASSISTANT_PACK_ID,
        "receipt_kind": str(receipt_kind or ""),
        "summary": str(summary or "")[:800],
        "delivery_state": str(delivery_state or ""),
        "next_step": str(next_step or "")[:400],
        "pass_hint": bool(pass_hint),
        "created_at": ts,
    }
    append_event(conn, str(task_id or ""), EventType.USER_OUTCOME_RECEIPT_RECORDED, event_payload)
    try:
        from .assistant_followthrough import sync_after_user_outcome_receipt

        rr = str(reply_reason or "").strip()
        if not rr and isinstance(payload, dict):
            rr = str(payload.get("reply_reason") or payload.get("reason") or "")
        sync_after_user_outcome_receipt(
            conn,
            receipt_id=rid,
            task_id=str(task_id or ""),
            goal_id=str(goal_id or ""),
            scenario_id=sid,
            pack_id=TRUSTED_DAILY_ASSISTANT_PACK_ID,
            receipt_kind=str(receipt_kind or ""),
            summary=str(summary or ""),
            delivery_state=str(delivery_state or ""),
            next_step=str(next_step or ""),
            proof_refs=proof_refs or {},
            payload=payload or {},
            reply_reason=rr,
        )
    except Exception:
        pass
    return {"receipt_id": rid, "event_payload": event_payload}


def receipt_from_scenario_resolution(
    *,
    scenario_payload: Dict[str, Any],
    reply_text: str,
    reply_route: str,
    reply_reason: str,
) -> tuple[str, str, Dict[str, Any], str, str]:
    """Derive receipt_kind, summary, proof_refs, delivery_state, next_step from routing outcome."""
    sid = str(scenario_payload.get("scenario_id") or "")
    goal_id = str(scenario_payload.get("goal_id") or "")
    proof_refs: Dict[str, Any] = {
        "scenario_id": sid,
        "route": reply_route,
        "reason": str(reply_reason or "")[:200],
        "confidence": scenario_payload.get("confidence"),
    }
    if goal_id:
        proof_refs["goal_id"] = goal_id

    delivery_state = "n/a"
    next_step = ""
    if sid == "statusFollowupContinue":
        kind = "status_followup"
        summary = f"Status / follow-up reply ({reply_reason})."
        next_step = "See assistant reply for latest status and next actions."
    elif sid == "goalContinuationAcrossSessions":
        kind = "goal_resume"
        summary = f"Goal continuation summary ({reply_reason})."
        if goal_id:
            proof_refs["linked_goal_id"] = goal_id
        next_step = "Continue from linked goal or clarify if the wrong goal was resumed."
    elif sid == "noteOrReminderCapture":
        kind = "note_or_reminder"
        summary = f"Note or reminder path ({reply_reason})."
        delivery_state = "depends_on_reminder_channel"
        next_step = "Confirm delivery target if reminder channel was missing."
    elif sid == "recentMessagesOrInboxLookup":
        kind = "inbox_lookup"
        summary = f"Recent messages / lookup ({reply_reason})."
        proof_refs["query_window"] = "see_reply"
        delivery_state = "read_only_summary"
        next_step = "Verify sources and time window in the reply."
    else:
        kind = "daily_domain"
        summary = f"Assistant outcome ({reply_reason})."

    clip = str(reply_text or "").strip()
    if len(clip) > 400:
        clip = clip[:397] + "..."
    proof_refs["reply_excerpt"] = clip
    return kind, summary, proof_refs, delivery_state, next_step


def try_record_assistant_receipt_for_direct_reply(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    scenario_payload: Dict[str, Any],
    reply_text: str,
    reply_meta: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    meta = reply_meta or {}
    kind, summary, proof_refs, delivery_state, next_step = receipt_from_scenario_resolution(
        scenario_payload=scenario_payload,
        reply_text=reply_text,
        reply_route=str(meta.get("route") or "direct"),
        reply_reason=str(meta.get("reason") or ""),
    )
    return record_user_facing_receipt(
        conn,
        task_id=task_id,
        scenario_id=str(scenario_payload.get("scenario_id") or ""),
        receipt_kind=kind,
        summary=summary,
        goal_id=str(scenario_payload.get("goal_id") or ""),
        proof_refs=proof_refs,
        delivery_state=delivery_state,
        next_step=next_step,
        pass_hint=True,
        payload={"source": "direct_assistant_reply", "reply_reason": str(meta.get("reason") or "")},
        reply_reason=str(meta.get("reason") or ""),
    )


def try_record_reminder_receipt(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    reminder_id: str,
    message: str,
    due_at: float,
    status: str,
    delivery_channel: str,
    delivery_target: str,
    principal_id: str = "",
) -> Optional[Dict[str, Any]]:
    """Receipt for CreateReminder / structured reminder capture (noteOrReminderCapture)."""
    proof = {
        "reminder_id": str(reminder_id or ""),
        "due_at": due_at,
        "delivery_channel": str(delivery_channel or ""),
        "delivery_target_present": bool(str(delivery_target or "").strip()),
        "principal_id": str(principal_id or ""),
    }
    delivery_state = (
        "scheduled"
        if str(status or "") == "scheduled"
        else "awaiting_delivery_channel"
    )
    pass_hint = delivery_state == "scheduled"
    summary = (
        f"Reminder recorded for due_at={due_at} status={status}."
        if pass_hint
        else "Reminder recorded but delivery channel is not yet confirmed."
    )
    return record_user_facing_receipt(
        conn,
        task_id=task_id,
        scenario_id="noteOrReminderCapture",
        receipt_kind="reminder_created",
        summary=summary,
        proof_refs=proof,
        delivery_state=delivery_state,
        next_step="Confirm Telegram/chat target if status is awaiting_delivery_channel.",
        pass_hint=pass_hint,
        payload={"source": "create_reminder_command"},
        reply_reason="reminder_created",
    )


__all__ = [
    "record_user_facing_receipt",
    "try_record_assistant_receipt_for_direct_reply",
    "try_record_reminder_receipt",
    "receipts_enabled",
]

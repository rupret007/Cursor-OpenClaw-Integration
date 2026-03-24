"""Deterministic closure and open-loop classification for the daily assistant pack (Stage A).

Pure functions — no I/O. See assistant_followthrough for ledger writes and events.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def classify_daily_pack_receipt(
    *,
    scenario_id: str,
    receipt_kind: str,
    delivery_state: str,
    next_step: str,
    proof_refs: Optional[Dict[str, Any]] = None,
    reply_reason: str = "",
) -> Dict[str, Any]:
    """
    Returns keys:
      loop_kind, open_loop_state (open|closed), closure_state, closure_reason,
      proof_kind, followthrough_kind, needs_continuation_signal, next_followup_due_at (0 if n/a)
    """
    sid = str(scenario_id or "").strip()
    refs = dict(proof_refs or {})
    ds = str(delivery_state or "").strip().lower()
    rk = str(receipt_kind or "").strip()
    rr = str(reply_reason or "").strip().lower()
    nxt = str(next_step or "").strip().lower()

    # Default: one-shot direct work is completed for trust surfacing (not "recorded == done" for reminders).
    if sid == "recentMessagesOrInboxLookup":
        return {
            "loop_kind": "inbox_lookup",
            "open_loop_state": "closed",
            "closure_state": "completed",
            "closure_reason": "read_only_lookup_delivered",
            "proof_kind": "read_only_summary",
            "followthrough_kind": "lookup",
            "needs_continuation_signal": False,
            "next_followup_due_at": 0.0,
            "risk_tier": "medium",
        }

    if sid == "statusFollowupContinue":
        if "plan_awaiting_approval" in rr or "awaiting_approval" in rr:
            return {
                "loop_kind": "approval_wait",
                "open_loop_state": "open",
                "closure_state": "awaiting_user",
                "closure_reason": "plan_step_awaiting_human_approval",
                "proof_kind": "approval_gate",
                "followthrough_kind": "approval_summary",
                "needs_continuation_signal": True,
                "next_followup_due_at": 0.0,
                "risk_tier": "high",
            }
        if "blocked" in nxt or "blocker" in nxt:
            return {
                "loop_kind": "status_followup",
                "open_loop_state": "open",
                "closure_state": "pending",
                "closure_reason": "follow_up_established_pending_dependency",
                "proof_kind": "assistant_reply",
                "followthrough_kind": "status",
                "needs_continuation_signal": True,
                "next_followup_due_at": 0.0,
                "risk_tier": "low",
            }
        return {
            "loop_kind": "status_followup",
            "open_loop_state": "closed",
            "closure_state": "completed",
            "closure_reason": "status_question_answered_direct_reply",
            "proof_kind": "assistant_reply",
            "followthrough_kind": "status",
            "needs_continuation_signal": False,
            "next_followup_due_at": 0.0,
            "risk_tier": "low",
        }

    if sid == "goalContinuationAcrossSessions":
        return {
            "loop_kind": "goal_resume",
            "open_loop_state": "open",
            "closure_state": "pending",
            "closure_reason": "goal_continuation_active_until_terminal_goal_state",
            "proof_kind": "goal_link_and_reply",
            "followthrough_kind": "goal",
            "needs_continuation_signal": True,
            "next_followup_due_at": 0.0,
            "risk_tier": "medium",
        }

    if sid == "noteOrReminderCapture":
        if rk == "reminder_created":
            if ds == "scheduled":
                due = float(refs.get("due_at") or 0.0)
                return {
                    "loop_kind": "reminder_delivery",
                    "open_loop_state": "open",
                    "closure_state": "awaiting_delivery",
                    "closure_reason": "reminder_scheduled_delivery_pending",
                    "proof_kind": "reminder_creation",
                    "followthrough_kind": "reminder",
                    "needs_continuation_signal": True,
                    "next_followup_due_at": due,
                    "risk_tier": "medium",
                }
            if "awaiting_delivery" in ds or ds == "awaiting_delivery_channel":
                return {
                    "loop_kind": "reminder_delivery",
                    "open_loop_state": "open",
                    "closure_state": "awaiting_user",
                    "closure_reason": "missing_delivery_target_or_channel",
                    "proof_kind": "reminder_creation_incomplete",
                    "followthrough_kind": "reminder",
                    "needs_continuation_signal": True,
                    "next_followup_due_at": 0.0,
                    "risk_tier": "medium",
                }
        return {
            "loop_kind": "note_or_reminder",
            "open_loop_state": "closed",
            "closure_state": "completed",
            "closure_reason": "note_or_non_scheduled_capture",
            "proof_kind": "assistant_reply",
            "followthrough_kind": "note",
            "needs_continuation_signal": False,
            "next_followup_due_at": 0.0,
            "risk_tier": "low",
        }

    return {
        "loop_kind": "daily_domain",
        "open_loop_state": "open",
        "closure_state": "pending",
        "closure_reason": "unclassified_daily_domain",
        "proof_kind": "assistant_reply",
        "followthrough_kind": "generic",
        "needs_continuation_signal": False,
        "next_followup_due_at": 0.0,
        "risk_tier": "low",
    }


def classify_reminder_delivery_outcome(
    *,
    event: str,
    reminder_id: str,
    task_id: str,
) -> Tuple[str, str, str, str]:
    """Returns closure_state, closure_reason, proof_kind, loop_kind."""
    _ = reminder_id
    ev = str(event or "").strip().lower()
    if ev == "delivered":
        return (
            "completed",
            "reminder_delivered_to_target",
            "delivery_event",
            "reminder_delivery",
        )
    if ev == "failed":
        return (
            "needs_repair",
            "reminder_delivery_failed",
            "delivery_failure",
            "reminder_delivery",
        )
    return ("pending", "reminder_delivery_in_progress", "delivery_pending", "reminder_delivery")


__all__ = ["classify_daily_pack_receipt", "classify_reminder_delivery_outcome"]

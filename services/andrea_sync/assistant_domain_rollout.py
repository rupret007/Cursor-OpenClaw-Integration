"""
Trusted Daily Assistant Continuity and Productivity Pack — rollout boundaries, operator snapshot,
and evidence gates (Stage A).

Reuses collaboration_rollout scenario_onboarding rows for per-scenario state; adds pack-level
decisions in domain_rollout_decisions and metrics from user_outcome_receipts.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

import sqlite3

from .schema import EventType

TRUSTED_DAILY_ASSISTANT_PACK_ID = "trusted_daily_continuity_v1"

# Low-risk daily scenarios in the first operator-visible pack (see product plan).
DAILY_ASSISTANT_SCENARIO_IDS: frozenset[str] = frozenset(
    {
        "statusFollowupContinue",
        "noteOrReminderCapture",
        "recentMessagesOrInboxLookup",
        "goalContinuationAcrossSessions",
    }
)

# Plan §9 — evidence to widen daily pack live behavior (receipt-quality gating).
DAILY_PACK_MIN_EVENTS = 30
DAILY_PACK_MIN_RECEIPT_PASS_RATE = 0.95
DAILY_PACK_MAX_FAILURE_RATE = 0.05

# Rollback / freeze hints (operator + automatic narrow paths may use these codes).
DAILY_PACK_ROLLBACK_FALSE_RECEIPT = "false_receipt"
DAILY_PACK_ROLLBACK_PRIVACY = "privacy_incident"
DAILY_PACK_ROLLBACK_RECEIPT_REGRESSION = "receipt_pass_rate_below_0.90"
DAILY_PACK_ROLLBACK_CONTINUATION_REGRESSION = "continuation_or_delivery_failure_above_0.10"


def is_daily_assistant_scenario(scenario_id: str) -> bool:
    return str(scenario_id or "").strip() in DAILY_ASSISTANT_SCENARIO_IDS


def _ensure_system_task(conn: sqlite3.Connection) -> None:
    from .store import SYSTEM_TASK_ID, create_task, task_exists

    if not task_exists(conn, SYSTEM_TASK_ID):
        create_task(conn, SYSTEM_TASK_ID, "internal")


def _append_pack_event(conn: sqlite3.Connection, event_type: EventType, payload: Dict[str, Any]) -> None:
    from .store import SYSTEM_TASK_ID, append_event

    _ensure_system_task(conn)
    append_event(conn, SYSTEM_TASK_ID, event_type, payload)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}"


def record_domain_pack_decision(
    conn: sqlite3.Connection,
    *,
    pack_id: str,
    decision: str,
    actor: str,
    reason: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append operator or system decision for the whole daily pack (audit + dashboard)."""
    from .store import insert_domain_rollout_decision_row

    act = str(actor or "").strip()
    if not act:
        return {"ok": False, "error": "actor_required"}
    dec = str(decision or "").strip()
    if not dec:
        return {"ok": False, "error": "decision_required"}
    pid = str(pack_id or TRUSTED_DAILY_ASSISTANT_PACK_ID).strip()
    did = _new_id("drd")
    insert_domain_rollout_decision_row(
        conn,
        decision_id=did,
        pack_id=pid,
        decision=dec,
        actor=act,
        reason=str(reason or "")[:2000],
        payload=payload or {},
    )
    _append_pack_event(
        conn,
        EventType.DOMAIN_ROLLOUT_DECISION_RECORDED,
        {
            "decision_id": did,
            "pack_id": pid,
            "decision": dec,
            "actor": act,
            "reason": str(reason or "")[:500],
        },
    )
    return {"ok": True, "decision_id": did, "pack_id": pid, "decision": dec}


def daily_pack_receipt_metrics(
    conn: sqlite3.Connection,
    *,
    pack_id: str = TRUSTED_DAILY_ASSISTANT_PACK_ID,
    window_seconds: float = 86400.0 * 7.0,
) -> Dict[str, Any]:
    from .store import count_user_outcome_receipts_window

    now = time.time()
    since = now - float(window_seconds or 0.0)
    total, passed = count_user_outcome_receipts_window(conn, pack_id=pack_id, since_ts=since)
    rate = (passed / total) if total else None
    return {
        "pack_id": pack_id,
        "window_start": since,
        "window_end": now,
        "receipt_count": total,
        "receipt_pass_count": passed,
        "receipt_pass_rate": rate,
    }


def daily_pack_live_evidence_report(
    conn: sqlite3.Connection,
    *,
    pack_id: str = TRUSTED_DAILY_ASSISTANT_PACK_ID,
) -> Dict[str, Any]:
    """
    Deterministic gate summary for operator-visible “live pack” promotion (receipt-truth first).

    Does not auto-widen behavior; surfaces whether measured evidence crosses plan thresholds.
    """
    m = daily_pack_receipt_metrics(conn, pack_id=pack_id)
    total = int(m.get("receipt_count") or 0)
    rate = m.get("receipt_pass_rate")
    rate_f = float(rate) if rate is not None else 0.0
    ok_count = total >= DAILY_PACK_MIN_EVENTS
    ok_rate = rate is not None and rate_f >= DAILY_PACK_MIN_RECEIPT_PASS_RATE

    ft_closure_rate = None
    ft_open_loops = 0
    ft_needs_repair = 0
    try:
        from .assistant_followthrough import followthrough_metrics_rollup

        ftm = followthrough_metrics_rollup(conn, window_seconds=86400.0 * 7.0)
        ft_closure_rate = ftm.get("closure_rate")
        ft_open_loops = int(ftm.get("open_loop_count") or 0)
        from .store import count_closure_decisions_window

        now = time.time()
        since = now - 86400.0 * 7.0
        ft_needs_repair = count_closure_decisions_window(
            conn, since_ts=since, closure_state="needs_repair"
        )
    except Exception:
        pass

    return {
        "ok": True,
        "pack_id": pack_id,
        "metrics": m,
        "followthrough_metrics": {
            "closure_rate_7d": ft_closure_rate,
            "open_loop_records_7d": ft_open_loops,
            "needs_repair_closures_7d": ft_needs_repair,
        },
        "thresholds": {
            "min_events": DAILY_PACK_MIN_EVENTS,
            "min_receipt_pass_rate": DAILY_PACK_MIN_RECEIPT_PASS_RATE,
            "max_continuation_or_delivery_failure_rate": DAILY_PACK_MAX_FAILURE_RATE,
        },
        "evidence_ok": bool(ok_count and ok_rate),
        "evidence_notes": (
            []
            if ok_count
            else [f"need_at_least_{DAILY_PACK_MIN_EVENTS}_receipt_events"]
        )
        + ([] if ok_rate else ["receipt_pass_rate_below_threshold"]),
    }


def daily_pack_metrics_for_learning(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Compact rollup for policy_learning / metric_log."""
    m = daily_pack_receipt_metrics(conn)
    ev = daily_pack_live_evidence_report(conn)
    ft = ev.get("followthrough_metrics") or {}
    return {
        "pack_id": m.get("pack_id"),
        "receipt_count_7d": m.get("receipt_count"),
        "receipt_pass_rate_7d": m.get("receipt_pass_rate"),
        "live_evidence_ok": ev.get("evidence_ok"),
        "followthrough_closure_rate_7d": ft.get("closure_rate_7d"),
        "followthrough_open_loops_7d": ft.get("open_loop_records_7d"),
        "followthrough_needs_repair_7d": ft.get("needs_repair_closures_7d"),
    }


def build_daily_pack_operator_snapshot(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Single object for dashboard + trusted_operator_summary."""
    from .collaboration_rollout import (
        effective_scenario_onboarding_state,
        scenario_onboarding_blocks_live_advisory,
    )
    from .store import list_recent_continuation_records, list_recent_domain_repair_outcomes

    scenarios: List[Dict[str, Any]] = []
    for sid in sorted(DAILY_ASSISTANT_SCENARIO_IDS):
        st = effective_scenario_onboarding_state(conn, sid)
        scenarios.append(
            {
                "scenario_id": sid,
                "effective_onboarding_state": st,
                "blocks_live_advisory": scenario_onboarding_blocks_live_advisory(conn, sid),
            }
        )

    metrics = daily_pack_receipt_metrics(conn)
    evidence = daily_pack_live_evidence_report(conn)

    cont_rows = list_recent_continuation_records(conn, limit=12)
    continuations = [
        {
            "continuation_id": str(r["continuation_id"] or ""),
            "linked_task_id": str(r["linked_task_id"] or ""),
            "reason": str(r["reason"] or "")[:200],
            "confidence_band": str(r["confidence_band"] or ""),
            "created_at": float(r["created_at"] or 0.0),
        }
        for r in cont_rows
    ]

    rep_rows = list_recent_domain_repair_outcomes(conn, limit=10)
    repairs = [
        {
            "repair_outcome_id": str(r["repair_outcome_id"] or ""),
            "domain_id": str(r["domain_id"] or ""),
            "scenario_id": str(r["scenario_id"] or ""),
            "repair_family": str(r["repair_family"] or ""),
            "result": str(r["result"] or "")[:200],
            "ts": float(r["ts"] or 0.0),
        }
        for r in rep_rows
    ]

    from .store import list_recent_domain_rollout_decisions

    decisions = []
    for r in list_recent_domain_rollout_decisions(
        conn, pack_id=TRUSTED_DAILY_ASSISTANT_PACK_ID, limit=8
    ):
        decisions.append(
            {
                "decision_id": str(r["decision_id"] or ""),
                "decision": str(r["decision"] or ""),
                "actor": str(r["actor"] or ""),
                "reason": str(r["reason"] or "")[:300],
                "created_at": float(r["created_at"] or 0.0),
            }
        )

    followthrough_board: Dict[str, Any] = {}
    try:
        from .assistant_followthrough import build_followthrough_operator_board

        followthrough_board = build_followthrough_operator_board(conn)
    except Exception:
        followthrough_board = {"ok": False, "error": "followthrough_board_unavailable"}

    return {
        "pack_id": TRUSTED_DAILY_ASSISTANT_PACK_ID,
        "scenario_ids": sorted(DAILY_ASSISTANT_SCENARIO_IDS),
        "scenarios": scenarios,
        "receipt_metrics": metrics,
        "live_rollout_evidence": evidence,
        "followthrough_board": followthrough_board,
        "live_rollout_slice": {
            "description": (
                "First live slice: direct-first handling for status follow-up, reminder/note capture, "
                "inbox/recent-message lookup, and cross-session goal continuation — with persisted "
                "user-facing receipts and default onboarding live_direct (collaboration advisory blocked "
                "until operator moves scenario to live_advisory)."
            ),
            "variants": ["baseline_direct", "shadow_enhanced", "live_enhanced"],
            "operator_endpoints": [
                "GET /v1/dashboard/summary (daily_assistant_pack + followthrough_board)",
                "GET /v1/internal/daily-assistant-pack (snapshot + evidence)",
                "POST /v1/internal/daily-assistant-pack (record_decision, snapshot, followthrough_snapshot, set_followthrough_pack_status)",
                "POST /v1/internal/rollout action=scenario_onboarding (per scenario)",
            ],
        },
        "evaluation_gates": evidence.get("thresholds"),
        "recent_continuations": continuations,
        "recent_domain_repairs": repairs,
        "recent_pack_decisions": decisions,
        "deferred_domains_doc": "docs/DAILY_ASSISTANT_PACK.md#deferred-and-high-risk-domains",
    }


def daily_pack_optimizer_hints(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Non-mutating hints for dashboard / optimizer attention."""
    hints: List[Dict[str, Any]] = []
    ev = daily_pack_live_evidence_report(conn)
    if not ev.get("evidence_ok"):
        hints.append(
            {
                "category": "daily_assistant_receipts",
                "severity": "medium",
                "title": "Daily pack receipt evidence below rollout threshold",
                "detail": "Collect more low-risk daily receipts or investigate receipt pass hints before widening.",
                "evidence": ev,
            }
        )
    m = daily_pack_receipt_metrics(conn, window_seconds=86400.0)
    if int(m.get("receipt_count") or 0) == 0:
        hints.append(
            {
                "category": "daily_assistant_receipts",
                "severity": "low",
                "title": "No daily-pack receipts in trailing window",
                "detail": "Traffic may be quiet, or receipt recording is disabled via ANDREA_DAILY_PACK_RECEIPTS_ENABLED.",
            }
        )
    try:
        from .assistant_followthrough import followthrough_metrics_rollup
        from .store import count_closure_decisions_window

        ft = followthrough_metrics_rollup(conn, window_seconds=86400.0 * 7.0)
        nr = int(
            count_closure_decisions_window(
                conn,
                since_ts=time.time() - 86400.0 * 7.0,
                closure_state="needs_repair",
            )
        )
    except Exception:
        ft = {}
        nr = 0
    else:
        if nr >= 3:
            hints.append(
                {
                    "category": "followthrough_closure",
                    "severity": "high",
                    "title": "Multiple reminder / delivery repair closures (7d)",
                    "detail": "Review delivery health and ANDREA_FOLLOWTHROUGH_PACK_STATUS; consider shadow-only until failure rate drops.",
                    "evidence": {"needs_repair_closures_7d": nr},
                }
            )
        cr = ft.get("closure_rate")
        if cr is not None and float(cr) < 0.5 and int(ft.get("closure_decision_count") or 0) >= 10:
            hints.append(
                {
                    "category": "followthrough_closure",
                    "severity": "medium",
                    "title": "Follow-through closure rate below 0.50 over trailing window",
                    "detail": "Compare open loops vs completions; tighten quiet follow-up gates before widening live slice.",
                    "evidence": ft,
                }
            )
    return hints

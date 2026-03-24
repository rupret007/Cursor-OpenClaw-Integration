"""
Trusted Follow-Through and Closure Manager — open-loop ledger, closure proofs, continuation signals.

Env:
  ANDREA_FOLLOWTHROUGH_ENABLED (default 1) — master switch for new ledger rows + events.
  ANDREA_FOLLOWTHROUGH_PACK_STATUS — tracked_only | shadow_followthrough | live_quiet_followthrough | frozen
  ANDREA_QUIET_FOLLOWTHROUGH_AUTO_EXEC (default 0) — when live_quiet_followthrough, record execution rows as executed=1
  ANDREA_FOLLOWTHROUGH_COLLAB_ON_REPAIR (default 0) — annotate activation payload when needs_repair loops exist
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, List, Optional

import sqlite3

from .assistant_domain_rollout import TRUSTED_DAILY_ASSISTANT_PACK_ID, is_daily_assistant_scenario
from .closure_rules import classify_daily_pack_receipt, classify_reminder_delivery_outcome
from .schema import EventType
from .scheduler import due_workflows


def followthrough_enabled() -> bool:
    return os.environ.get("ANDREA_FOLLOWTHROUGH_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def effective_followthrough_pack_status(conn: Optional[sqlite3.Connection] = None) -> str:
    allowed = frozenset(
        {"tracked_only", "shadow_followthrough", "live_quiet_followthrough", "frozen"}
    )
    if conn is not None:
        try:
            from .store import get_meta

            o = get_meta(conn, "followthrough_pack_status_override")
            if o and str(o).strip():
                v = str(o).strip().lower()
                if v in allowed:
                    return v
        except Exception:
            pass
    raw = os.environ.get("ANDREA_FOLLOWTHROUGH_PACK_STATUS", "shadow_followthrough").strip().lower()
    return raw if raw in allowed else "shadow_followthrough"


def quiet_followthrough_auto_exec_enabled() -> bool:
    return os.environ.get("ANDREA_QUIET_FOLLOWTHROUGH_AUTO_EXEC", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def followthrough_collab_on_repair_enabled() -> bool:
    return os.environ.get("ANDREA_FOLLOWTHROUGH_COLLAB_ON_REPAIR", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:10]}"


def _append_pack_event(conn: sqlite3.Connection, event_type: EventType, payload: Dict[str, Any]) -> None:
    from .store import SYSTEM_TASK_ID, append_event, ensure_system_task

    ensure_system_task(conn)
    append_event(conn, SYSTEM_TASK_ID, event_type, payload)


def sync_after_user_outcome_receipt(
    conn: sqlite3.Connection,
    *,
    receipt_id: str,
    task_id: str,
    goal_id: str,
    scenario_id: str,
    pack_id: str,
    receipt_kind: str,
    summary: str,
    delivery_state: str,
    next_step: str,
    proof_refs: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    reply_reason: str = "",
) -> Optional[Dict[str, Any]]:
    """After a user_outcome_receipt row exists: open loop, closure decision, optional triggers."""
    if not followthrough_enabled():
        return None
    sid = str(scenario_id or "").strip()
    if not is_daily_assistant_scenario(sid):
        return None
    status = effective_followthrough_pack_status(conn)
    if status == "frozen":
        return {"ok": False, "reason": "followthrough_frozen"}

    from .store import (
        append_event,
        insert_closure_decision_row,
        insert_closure_proof_row,
        insert_continuation_trigger_row,
        insert_followup_recommendation_row,
        insert_open_loop_record,
        insert_continuation_execution_row,
        update_user_outcome_receipt_followthrough,
    )

    pl = dict(payload or {})
    rr = str(
        reply_reason
        or pl.get("reply_reason")
        or (pl.get("source") if isinstance(pl.get("source"), str) else "")
        or ""
    )
    cls = classify_daily_pack_receipt(
        scenario_id=sid,
        receipt_kind=receipt_kind,
        delivery_state=delivery_state,
        next_step=next_step,
        proof_refs=proof_refs,
        reply_reason=rr,
    )

    loop_id = _new_id("loop")
    proof_id = _new_id("prf")
    decision_id = _new_id("cld")
    ts = time.time()

    insert_open_loop_record(
        conn,
        loop_id=loop_id,
        task_id=str(task_id or ""),
        goal_id=str(goal_id or ""),
        scenario_id=sid,
        pack_id=str(pack_id or TRUSTED_DAILY_ASSISTANT_PACK_ID),
        loop_kind=str(cls["loop_kind"]),
        open_loop_state=str(cls["open_loop_state"]),
        opened_reason=str(cls["closure_reason"]),
        opened_at=ts,
        due_at=float(cls.get("next_followup_due_at") or 0.0),
        owner_kind="user",
        receipt_id=str(receipt_id or ""),
        risk_tier=str(cls.get("risk_tier") or "low"),
        proof_refs={
            "receipt_id": receipt_id,
            "receipt_kind": receipt_kind,
            "delivery_state": delivery_state,
            **(proof_refs or {}),
        },
        payload={"followthrough_pack_status": status},
    )
    append_event(
        conn,
        str(task_id or ""),
        EventType.OPEN_LOOP_RECORDED,
        {
            "loop_id": loop_id,
            "task_id": task_id,
            "scenario_id": sid,
            "loop_kind": cls["loop_kind"],
            "open_loop_state": cls["open_loop_state"],
            "pack_id": pack_id,
            "receipt_id": receipt_id,
            "opened_at": ts,
        },
    )

    insert_closure_proof_row(
        conn,
        proof_id=proof_id,
        loop_id=loop_id,
        task_id=str(task_id or ""),
        proof_kind=str(cls["proof_kind"]),
        proof_refs={"receipt_id": receipt_id, "scenario_id": sid},
        verdict=str(cls["closure_state"]),
        summary=str(summary or "")[:800],
        created_at=ts,
        payload={"rule": "classify_daily_pack_receipt"},
    )

    insert_closure_decision_row(
        conn,
        decision_id=decision_id,
        loop_id=loop_id,
        task_id=str(task_id or ""),
        closure_state=str(cls["closure_state"]),
        reason=str(cls["closure_reason"]),
        proof_kind=str(cls["proof_kind"]),
        proof_refs={"proof_id": proof_id, "receipt_id": receipt_id},
        confidence_band="deterministic_v1",
        actor_or_rule="closure_rules.classify_daily_pack_receipt",
        created_at=ts,
        payload={"followthrough_pack_status": status},
    )
    append_event(
        conn,
        str(task_id or ""),
        EventType.CLOSURE_DECISION_RECORDED,
        {
            "decision_id": decision_id,
            "loop_id": loop_id,
            "task_id": task_id,
            "closure_state": cls["closure_state"],
            "reason": str(cls["closure_reason"])[:500],
            "proof_kind": cls["proof_kind"],
            "created_at": ts,
        },
    )

    update_user_outcome_receipt_followthrough(
        conn,
        receipt_id=str(receipt_id or ""),
        closure_state=str(cls["closure_state"]),
        closure_proof_id=proof_id,
        followthrough_kind=str(cls["followthrough_kind"]),
    )

    out: Dict[str, Any] = {
        "ok": True,
        "loop_id": loop_id,
        "decision_id": decision_id,
        "proof_id": proof_id,
        "closure_state": cls["closure_state"],
    }

    if status == "tracked_only":
        return out

    if cls.get("needs_continuation_signal"):
        trigger_id = _new_id("ctg")
        insert_continuation_trigger_row(
            conn,
            trigger_id=trigger_id,
            loop_id=loop_id,
            task_id=str(task_id or ""),
            trigger_type=str(cls["closure_state"]),
            due_at=float(cls.get("next_followup_due_at") or 0.0),
            eligibility="open_loop_evidence",
            evidence_snapshot={
                "scenario_id": sid,
                "receipt_kind": receipt_kind,
                "closure_state": cls["closure_state"],
            },
            created_at=ts,
            payload={"pack_status": status},
        )
        append_event(
            conn,
            str(task_id or ""),
            EventType.CONTINUATION_TRIGGER_RECORDED,
            {
                "trigger_id": trigger_id,
                "loop_id": loop_id,
                "task_id": task_id,
                "trigger_type": cls["closure_state"],
                "due_at": cls.get("next_followup_due_at") or 0.0,
                "created_at": ts,
            },
        )
        reco_id = _new_id("fur")
        shadow = status != "live_quiet_followthrough"
        insert_followup_recommendation_row(
            conn,
            recommendation_id=reco_id,
            loop_id=loop_id,
            task_id=str(task_id or ""),
            recommended_action="quiet_check_or_operator_review",
            channel="in_band",
            why_now=str(cls["closure_reason"])[:500],
            urgency="low",
            shadow_only=shadow,
            risk_notes="no_user_ping_without_live_quiet_gate",
            created_at=ts,
            payload={"pack_status": status},
        )
        append_event(
            conn,
            str(task_id or ""),
            EventType.FOLLOWUP_RECOMMENDATION_RECORDED,
            {
                "recommendation_id": reco_id,
                "loop_id": loop_id,
                "task_id": task_id,
                "shadow_only": shadow,
                "created_at": ts,
            },
        )
        out["trigger_id"] = trigger_id
        out["recommendation_id"] = reco_id

        if status == "live_quiet_followthrough":
            exec_id = _new_id("cex")
            executed = quiet_followthrough_auto_exec_enabled()
            insert_continuation_execution_row(
                conn,
                execution_id=exec_id,
                loop_id=loop_id,
                task_id=str(task_id or ""),
                action_kind="quiet_followthrough_placeholder",
                channel="none",
                executed=executed,
                result=("ledger_only" if not executed else "auto_exec_flag_set"),
                message_ref="",
                created_at=ts,
                payload={
                    "note": "User-visible send stays behind explicit channel integration + higher bar.",
                    "shadow_only": False,
                },
            )
            append_event(
                conn,
                str(task_id or ""),
                EventType.CONTINUATION_EXECUTION_RECORDED,
                {
                    "execution_id": exec_id,
                    "loop_id": loop_id,
                    "task_id": task_id,
                    "executed": executed,
                    "created_at": ts,
                },
            )
            out["execution_id"] = exec_id

    return out


def on_reminder_lifecycle_event(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    event_name: str,
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """REMINDER_DELIVERED / REMINDER_FAILED → closure decision + optional stale/repair signal."""
    if not followthrough_enabled():
        return None
    if effective_followthrough_pack_status(conn) == "frozen":
        return None

    from .store import append_event, insert_closure_decision_row, insert_stale_task_indicator_row

    reminder_id = str(payload.get("reminder_id") or "")
    ev = "delivered" if event_name == "delivered" else "failed"
    cstate, creason, pkind, lkind = classify_reminder_delivery_outcome(
        event=ev, reminder_id=reminder_id, task_id=str(task_id or "")
    )
    loop_id = _new_id("loop")
    decision_id = _new_id("cld")
    ts = time.time()

    insert_closure_decision_row(
        conn,
        decision_id=decision_id,
        loop_id=loop_id,
        task_id=str(task_id or ""),
        closure_state=cstate,
        reason=creason,
        proof_kind=pkind,
        proof_refs={"reminder_id": reminder_id, "event": event_name},
        confidence_band="deterministic_v1",
        actor_or_rule="closure_rules.classify_reminder_delivery_outcome",
        created_at=ts,
        payload={"source": "reminder_lifecycle"},
    )
    append_event(
        conn,
        str(task_id or ""),
        EventType.CLOSURE_DECISION_RECORDED,
        {
            "decision_id": decision_id,
            "loop_id": loop_id,
            "task_id": task_id,
            "closure_state": cstate,
            "reason": creason,
            "reminder_id": reminder_id,
            "created_at": ts,
        },
    )

    if cstate == "needs_repair":
        ind_id = _new_id("sti")
        insert_stale_task_indicator_row(
            conn,
            indicator_id=ind_id,
            loop_id=loop_id,
            task_id=str(task_id or ""),
            staleness_kind="reminder_delivery_failed",
            window_seconds=0.0,
            severity="medium",
            detected_at=ts,
            reason=creason,
            payload={"reminder_id": reminder_id},
        )
        append_event(
            conn,
            str(task_id or ""),
            EventType.STALE_TASK_INDICATED,
            {
                "indicator_id": ind_id,
                "loop_id": loop_id,
                "task_id": task_id,
                "staleness_kind": "reminder_delivery_failed",
                "created_at": ts,
            },
        )

    return {"ok": True, "decision_id": decision_id, "closure_state": cstate, "loop_kind": lkind}


def on_telegram_continuation_recorded(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    continuation_id: str,
    principal_id: str = "",
) -> None:
    """Link continuation ledger to a lightweight open-loop row (observability)."""
    if not followthrough_enabled():
        return
    if effective_followthrough_pack_status(conn) == "frozen":
        return
    from .store import append_event, insert_open_loop_record

    loop_id = _new_id("loop")
    ts = time.time()
    insert_open_loop_record(
        conn,
        loop_id=loop_id,
        task_id=str(task_id or ""),
        goal_id="",
        scenario_id="",
        pack_id=TRUSTED_DAILY_ASSISTANT_PACK_ID,
        loop_kind="telegram_continuation",
        open_loop_state="open",
        opened_reason="telegram_thread_continuation_recorded",
        opened_at=ts,
        owner_kind="user",
        receipt_id="",
        risk_tier="low",
        proof_refs={"continuation_id": continuation_id, "principal_id": principal_id},
        payload={"surface": "telegram_continuation"},
    )
    append_event(
        conn,
        str(task_id or ""),
        EventType.OPEN_LOOP_RECORDED,
        {
            "loop_id": loop_id,
            "task_id": task_id,
            "loop_kind": "telegram_continuation",
            "open_loop_state": "open",
            "continuation_id": continuation_id,
            "opened_at": ts,
        },
    )


def poll_due_workflows_for_followthrough(
    conn: sqlite3.Connection,
    *,
    limit: int = 12,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Emit stale-task indicators for due workflows (no user messaging)."""
    if not followthrough_enabled():
        return []
    status = effective_followthrough_pack_status(conn)
    if status == "tracked_only" or status == "frozen":
        return []

    from .store import SYSTEM_TASK_ID, append_event, insert_stale_task_indicator_row

    ts = time.time() if now is None else float(now)
    rows = due_workflows(conn, now=ts, limit=limit)
    out: List[Dict[str, Any]] = []
    for wf in rows:
        wid = str(wf.get("workflow_id") or wf.get("id") or "")
        # Workflows are principal-scoped; use system task for cross-cutting stale signals.
        tid = SYSTEM_TASK_ID
        pid = str(wf.get("principal_id") or "")
        ind_id = _new_id("sti")
        loop_id = _new_id("loop")
        insert_stale_task_indicator_row(
            conn,
            indicator_id=ind_id,
            loop_id=loop_id,
            task_id=tid,
            staleness_kind="workflow_next_run_due",
            window_seconds=0.0,
            severity="low",
            detected_at=ts,
            reason=f"workflow_due workflow_id={wid}",
            payload={"workflow": wf, "principal_id": pid},
        )
        append_event(
            conn,
            tid,
            EventType.STALE_TASK_INDICATED,
            {
                "indicator_id": ind_id,
                "loop_id": loop_id,
                "task_id": tid,
                "staleness_kind": "workflow_next_run_due",
                "workflow_id": wid,
                "created_at": ts,
            },
        )
        out.append({"indicator_id": ind_id, "workflow_id": wid, "task_id": tid})
    return out


def followthrough_metrics_rollup(
    conn: sqlite3.Connection,
    *,
    window_seconds: float = 86400.0 * 7.0,
) -> Dict[str, Any]:
    from .store import count_closure_decisions_window, count_open_loops_window

    now = time.time()
    since = now - float(window_seconds or 0.0)
    total_closure = count_closure_decisions_window(conn, since_ts=since)
    completed = count_closure_decisions_window(conn, since_ts=since, closure_state="completed")
    open_loops = count_open_loops_window(conn, since_ts=since)
    closure_rate = (completed / total_closure) if total_closure else None
    return {
        "window_start": since,
        "window_end": now,
        "open_loop_count": open_loops,
        "closure_decision_count": total_closure,
        "completed_closure_count": completed,
        "closure_rate": closure_rate,
        "followthrough_pack_status": effective_followthrough_pack_status(conn),
    }


def merge_followthrough_collaboration_gate(
    conn: sqlite3.Connection,
    activation: Dict[str, Any],
    *,
    task_id: str,
    scenario_id: str,
) -> Dict[str, Any]:
    """
    Non-mutating overlay: marks when daily follow-through sees repair-eligible state.
    Does not widen activation by default (ANDREA_FOLLOWTHROUGH_COLLAB_ON_REPAIR).
    """
    if not followthrough_collab_on_repair_enabled():
        activation = dict(activation)
        activation["followthrough_gate"] = {
            "repair_collab_eligible": False,
            "reason": "flag_disabled",
        }
        return activation
    sid = str(scenario_id or "").strip()
    if not is_daily_assistant_scenario(sid):
        activation = dict(activation)
        activation["followthrough_gate"] = {"repair_collab_eligible": False, "reason": "not_daily_pack"}
        return activation
    try:
        from .store import list_recent_closure_decisions

        eligible = False
        for row in list_recent_closure_decisions(conn, limit=24):
            if str(row["task_id"] or "") != str(task_id or ""):
                continue
            if str(row["closure_state"] or "") == "needs_repair":
                eligible = True
                break
    except Exception:
        eligible = False
    activation = dict(activation)
    activation["followthrough_gate"] = {
        "repair_collab_eligible": eligible,
        "reason": "needs_repair_on_recent_closure" if eligible else "no_recent_repair_closure",
    }
    if eligible:
        rc = list(activation.get("reason_codes") or [])
        if "followthrough_repair_eligible" not in rc:
            rc.append("followthrough_repair_eligible")
        activation["reason_codes"] = rc
    return activation


def set_followthrough_pack_status_override(
    conn: sqlite3.Connection,
    *,
    status: str,
    actor: str,
    reason: str = "",
) -> Dict[str, Any]:
    """Persist operator override (takes precedence over ANDREA_FOLLOWTHROUGH_PACK_STATUS env)."""
    allowed = frozenset(
        {"tracked_only", "shadow_followthrough", "live_quiet_followthrough", "frozen"}
    )
    st = str(status or "").strip().lower()
    if st not in allowed:
        return {"ok": False, "error": "invalid_followthrough_status", "allowed": sorted(allowed)}
    act = str(actor or "").strip()
    if not act:
        return {"ok": False, "error": "actor_required"}
    from .store import set_meta

    set_meta(conn, "followthrough_pack_status_override", st)
    from .assistant_domain_rollout import record_domain_pack_decision

    record_domain_pack_decision(
        conn,
        pack_id=TRUSTED_DAILY_ASSISTANT_PACK_ID,
        decision=f"followthrough_pack_status:{st}",
        actor=act,
        reason=str(reason or "")[:2000],
        payload={"followthrough_status": st},
    )
    return {"ok": True, "followthrough_pack_status": st}


def build_followthrough_operator_board(conn: sqlite3.Connection) -> Dict[str, Any]:
    from .store import (
        list_recent_closure_decisions,
        list_recent_followup_recommendations,
        list_recent_open_loop_records,
        list_recent_stale_task_indicators,
    )

    pack = TRUSTED_DAILY_ASSISTANT_PACK_ID
    loops = list_recent_open_loop_records(conn, pack_id=pack, limit=20)
    decisions = list_recent_closure_decisions(conn, limit=20)
    recos = list_recent_followup_recommendations(conn, limit=16)
    stales = list_recent_stale_task_indicators(conn, limit=16)
    metrics = followthrough_metrics_rollup(conn)

    def _rows(rs: List[Any]) -> List[Dict[str, Any]]:
        out = []
        for r in rs:
            out.append({k: r[k] for k in r.keys()})
        return out

    return {
        "pack_id": pack,
        "followthrough_pack_status": effective_followthrough_pack_status(conn),
        "quiet_auto_exec": quiet_followthrough_auto_exec_enabled(),
        "metrics": metrics,
        "recent_open_loops": _rows(loops),
        "recent_closure_decisions": _rows(decisions),
        "recent_followup_recommendations": _rows(recos),
        "recent_stale_indicators": _rows(stales),
        "live_quiet_slice": {
            "description": (
                "Operator-controlled live quiet follow-through: ledger + optional ANDREA_QUIET_FOLLOWTHROUGH_AUTO_EXEC. "
                "Default remains shadow/trigger-only; no outbound spam without explicit execution wiring."
            ),
            "env": {
                "ANDREA_FOLLOWTHROUGH_PACK_STATUS": effective_followthrough_pack_status(conn),
                "ANDREA_QUIET_FOLLOWTHROUGH_AUTO_EXEC": str(quiet_followthrough_auto_exec_enabled()).lower(),
            },
        },
        "selective_collaboration": {
            "description": (
                "Collaboration/repair strategist only when followthrough_gate.repair_collab_eligible and "
                "ANDREA_FOLLOWTHROUGH_COLLAB_ON_REPAIR=1 (bounded; does not bypass approval policy)."
            ),
            "env_flag": followthrough_collab_on_repair_enabled(),
        },
    }


__all__ = [
    "build_followthrough_operator_board",
    "set_followthrough_pack_status_override",
    "effective_followthrough_pack_status",
    "followthrough_collab_on_repair_enabled",
    "followthrough_enabled",
    "followthrough_metrics_rollup",
    "merge_followthrough_collaboration_gate",
    "on_reminder_lifecycle_event",
    "on_telegram_continuation_recorded",
    "poll_due_workflows_for_followthrough",
    "quiet_followthrough_auto_exec_enabled",
    "sync_after_user_outcome_receipt",
]

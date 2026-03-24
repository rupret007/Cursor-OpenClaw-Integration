"""
Operator-controlled collaboration rollout layer on top of collaboration_promotion.

Internal-token HTTP surfaces list candidates, record approve/freeze/rollback with actor
identity, scenario onboarding, persisted live-vs-shadow comparisons, and guarded expansion gates.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

import sqlite3

from .schema import EventType
from .scenario_registry import get_contract
from .scenario_schema import DRAFT_ONLY
from .assistant_domain_rollout import DAILY_ASSISTANT_SCENARIO_IDS

ROLLOUT_MANAGER_VERSION = "2026.03.rollout.v1"

ONBOARDING_STATES = frozenset(
    {"measured_only", "shadow_only", "live_direct", "live_advisory", "frozen"}
)

ROLLOUT_ONBOARDING_SCENARIOS = frozenset(
    {
        "verificationSensitiveAction",
        "multiStepTroubleshoot",
    }
)


def _ensure_system_task(conn: sqlite3.Connection) -> None:
    from .store import SYSTEM_TASK_ID, create_task, task_exists

    if not task_exists(conn, SYSTEM_TASK_ID):
        create_task(conn, SYSTEM_TASK_ID, "internal")


def _append_rollout_event(conn: sqlite3.Connection, event_type: EventType, payload: Dict[str, Any]) -> None:
    from .store import SYSTEM_TASK_ID, append_event

    _ensure_system_task(conn)
    append_event(conn, SYSTEM_TASK_ID, event_type, payload)


def default_scenario_onboarding_state(scenario_id: str) -> str:
    """Default before any explicit operator record.

    Draft catalog entries and explicit rollout-queue scenarios start measured-only.
    Established auto-supported scenarios (e.g. repo help) do not add an implicit block.
    """
    c = get_contract(str(scenario_id or ""))
    if c is not None and c.support_level == DRAFT_ONLY:
        return "measured_only"
    sid = str(scenario_id or "").strip()
    if sid in ROLLOUT_ONBOARDING_SCENARIOS:
        return "measured_only"
    if sid in DAILY_ASSISTANT_SCENARIO_IDS:
        return "live_direct"
    # No onboarding row and not in the measured-first queue -> do not constrain activation.
    return "live_advisory"


def effective_scenario_onboarding_state(conn: sqlite3.Connection, scenario_id: str) -> str:
    from .store import fetch_latest_scenario_onboarding

    row = fetch_latest_scenario_onboarding(conn, str(scenario_id or ""))
    if row:
        st = str(row["state"] or "").strip()
        if st in ONBOARDING_STATES:
            return st
    return default_scenario_onboarding_state(scenario_id)


def scenario_onboarding_blocks_live_advisory(conn: sqlite3.Connection, scenario_id: str) -> bool:
    st = effective_scenario_onboarding_state(conn, scenario_id)
    # live_direct: direct-first daily pack; collaboration advisory stays off until live_advisory.
    return st in ("measured_only", "shadow_only", "frozen", "live_direct")


def _draft_only_scenario(scenario_id: str) -> bool:
    c = get_contract(str(scenario_id or ""))
    return c is not None and c.support_level == DRAFT_ONLY


def record_scenario_onboarding(
    conn: sqlite3.Connection,
    *,
    scenario_id: str,
    state: str,
    actor: str,
    notes: str = "",
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    st = str(state or "").strip()
    if st not in ONBOARDING_STATES:
        return {"ok": False, "error": "invalid_onboarding_state", "allowed": sorted(ONBOARDING_STATES)}
    sid = str(scenario_id or "").strip()
    if not sid:
        return {"ok": False, "error": "scenario_id_required"}
    if _draft_only_scenario(sid) and st == "live_advisory":
        return {
            "ok": False,
            "error": "draft_only_scenario_cannot_enter_live_advisory",
            "scenario_id": sid,
        }
    if sid not in ROLLOUT_ONBOARDING_SCENARIOS and sid not in (
        "repoHelpVerified",
    ):
        # Still allow recording for catalog scenarios; primary focus is rollout scenarios.
        pass
    oid = f"onb-{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}"
    from .store import insert_scenario_onboarding_record

    insert_scenario_onboarding_record(
        conn,
        onboarding_id=oid,
        scenario_id=sid,
        state=st,
        actor=str(actor or "").strip()[:200],
        notes=str(notes or "")[:2000],
        evidence=evidence or {},
    )
    _append_rollout_event(
        conn,
        EventType.SCENARIO_ONBOARDING_RECORDED,
        {
            "onboarding_id": oid,
            "scenario_id": sid,
            "state": st,
            "actor": str(actor or "").strip()[:200],
            "rollout_manager_version": ROLLOUT_MANAGER_VERSION,
        },
    )
    return {"ok": True, "onboarding_id": oid, "scenario_id": sid, "state": st}


def list_scenario_onboarding_snapshot(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    combined = set(ROLLOUT_ONBOARDING_SCENARIOS) | set(DAILY_ASSISTANT_SCENARIO_IDS)
    for sid in sorted(combined):
        out.append(
            {
                "scenario_id": sid,
                "effective_state": effective_scenario_onboarding_state(conn, sid),
                "blocks_live_advisory": scenario_onboarding_blocks_live_advisory(conn, sid),
                "draft_only": _draft_only_scenario(sid),
                "daily_assistant_pack": sid in DAILY_ASSISTANT_SCENARIO_IDS,
            }
        )
    return out


def _new_action_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}"


def _record_operator_row(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action_kind: str,
    subject_key: str,
    scenario_id: str,
    trigger: str,
    requested_level: str,
    decision: str,
    revision_id: str,
    reason: str,
    payload: Optional[Dict[str, Any]] = None,
) -> str:
    from .store import insert_collaboration_operator_action

    aid = _new_action_id("op")
    insert_collaboration_operator_action(
        conn,
        action_id=aid,
        actor=str(actor or "").strip()[:200],
        action_kind=action_kind,
        subject_key=subject_key,
        scenario_id=scenario_id,
        trigger=trigger,
        requested_level=requested_level,
        decision=decision,
        revision_id=revision_id,
        reason=str(reason or "")[:2000],
        payload=payload or {},
    )
    return aid


def operator_approve_live_advisory(
    conn: sqlite3.Connection,
    *,
    scenario_id: str,
    trigger: str,
    actor: str,
    risk_notes: str = "",
    grant_subject: bool = False,
) -> Dict[str, Any]:
    from .collaboration_promotion import promote_subject_live_advisory, subject_key
    from .store import upsert_rollout_subject_grant

    act = str(actor or "").strip()
    if not act:
        return {"ok": False, "error": "actor_required"}
    sk = subject_key(scenario_id, trigger)
    if scenario_onboarding_blocks_live_advisory(conn, str(scenario_id or "")):
        return {
            "ok": False,
            "error": "scenario_onboarding_blocks_live",
            "scenario_id": str(scenario_id or ""),
            "onboarding_state": effective_scenario_onboarding_state(conn, str(scenario_id or "")),
        }
    if grant_subject:
        upsert_rollout_subject_grant(
            conn, subject_key=sk, actor=act, notes=str(risk_notes or "")[:2000]
        )
    res = promote_subject_live_advisory(
        conn,
        scenario_id=scenario_id,
        trigger=trigger,
        operator_ack=True,
        risk_notes=risk_notes,
        actor=act,
    )
    if not res.get("ok"):
        return res
    rid = str(res.get("revision_id") or "")
    aid = _record_operator_row(
        conn,
        actor=act,
        action_kind="approve_live_advisory",
        subject_key=sk,
        scenario_id=str(scenario_id or ""),
        trigger=str(trigger or ""),
        requested_level="live_advisory",
        decision="approved",
        revision_id=rid,
        reason=risk_notes,
        payload={"promotion_result": res},
    )
    _append_rollout_event(
        conn,
        EventType.OPERATOR_APPROVAL_RECORDED,
        {
            "action_id": aid,
            "actor": act,
            "subject_key": sk,
            "decision": "approve_live_advisory",
            "revision_id": rid,
            "rollout_manager_version": ROLLOUT_MANAGER_VERSION,
        },
    )
    _append_rollout_event(
        conn,
        EventType.ROLLOUT_DECISION_RECORDED,
        {
            "decision": "approve_live",
            "subject_key": sk,
            "revision_id": rid,
            "actor": act,
            "rollout_manager_version": ROLLOUT_MANAGER_VERSION,
        },
    )
    return {**res, "operator_action_id": aid}


def operator_freeze_subject(
    conn: sqlite3.Connection,
    *,
    scenario_id: str,
    trigger: str,
    actor: str,
    reason_codes: Optional[List[str]] = None,
    notes: str = "",
) -> Dict[str, Any]:
    from .collaboration_promotion import freeze_subject, subject_key

    act = str(actor or "").strip()
    if not act:
        return {"ok": False, "error": "actor_required"}
    codes = list(reason_codes or [])
    if not codes:
        codes = ["operator_freeze"]
    if str(notes or "").strip():
        codes.append(f"operator_note:{str(notes).strip()[:180]}")
    sk = subject_key(scenario_id, trigger)
    res = freeze_subject(conn, scenario_id=scenario_id, trigger=trigger, reason_codes=codes, actor=act)
    if not res.get("ok"):
        return res
    rid = str(res.get("revision_id") or "")
    aid = _record_operator_row(
        conn,
        actor=act,
        action_kind="freeze",
        subject_key=sk,
        scenario_id=str(scenario_id or ""),
        trigger=str(trigger or ""),
        requested_level="freeze",
        decision="frozen",
        revision_id=rid,
        reason=",".join(codes)[:2000],
    )
    _append_rollout_event(
        conn,
        EventType.OPERATOR_APPROVAL_RECORDED,
        {
            "action_id": aid,
            "actor": act,
            "subject_key": sk,
            "decision": "freeze",
            "revision_id": rid,
            "rollout_manager_version": ROLLOUT_MANAGER_VERSION,
        },
    )
    _append_rollout_event(
        conn,
        EventType.ROLLOUT_DECISION_RECORDED,
        {
            "decision": "freeze",
            "subject_key": sk,
            "revision_id": rid,
            "actor": act,
            "rollout_manager_version": ROLLOUT_MANAGER_VERSION,
        },
    )
    return {**res, "operator_action_id": aid}


def operator_rollback_subject(
    conn: sqlite3.Connection,
    *,
    scenario_id: str,
    trigger: str,
    actor: str,
    reason_codes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    from .collaboration_promotion import rollback_subject_operator, subject_key

    act = str(actor or "").strip()
    if not act:
        return {"ok": False, "error": "actor_required"}
    sk = subject_key(scenario_id, trigger)
    res = rollback_subject_operator(
        conn,
        scenario_id=scenario_id,
        trigger=trigger,
        reason_codes=reason_codes,
        actor=act,
    )
    if not res.get("ok"):
        return res
    aid = _record_operator_row(
        conn,
        actor=act,
        action_kind="rollback",
        subject_key=sk,
        scenario_id=str(scenario_id or ""),
        trigger=str(trigger or ""),
        requested_level="shadow_only",
        decision="rolled_back",
        revision_id="",
        reason=",".join(reason_codes or ["operator_rollback"])[:2000],
        payload=res,
    )
    _append_rollout_event(
        conn,
        EventType.OPERATOR_APPROVAL_RECORDED,
        {
            "action_id": aid,
            "actor": act,
            "subject_key": sk,
            "decision": "rollback",
            "rollout_manager_version": ROLLOUT_MANAGER_VERSION,
        },
    )
    _append_rollout_event(
        conn,
        EventType.ROLLOUT_DECISION_RECORDED,
        {
            "decision": "rollback",
            "subject_key": sk,
            "actor": act,
            "rollout_manager_version": ROLLOUT_MANAGER_VERSION,
        },
    )
    return {**res, "operator_action_id": aid}


def operator_promote_bounded_action(
    conn: sqlite3.Connection,
    *,
    scenario_id: str,
    trigger: str,
    action_family: str,
    actor: str,
    risk_notes: str = "",
) -> Dict[str, Any]:
    """Stronger evidence + explicit actor; subject must pass expansion_gate_report."""
    from .collaboration_promotion import promote_subject_bounded_action, subject_key

    gate = expansion_gate_report(conn, subject_key(scenario_id, trigger))
    if not gate.get("bounded_action_promotion_allowed"):
        return {"ok": False, "error": "expansion_gate_failed", "gate": gate}
    act = str(actor or "").strip()
    if not act:
        return {"ok": False, "error": "actor_required"}
    if scenario_onboarding_blocks_live_advisory(conn, str(scenario_id or "")):
        return {
            "ok": False,
            "error": "scenario_onboarding_blocks_live",
            "onboarding_state": effective_scenario_onboarding_state(conn, str(scenario_id or "")),
        }
    sk = subject_key(scenario_id, trigger)
    res = promote_subject_bounded_action(
        conn,
        scenario_id=scenario_id,
        trigger=trigger,
        action_family=action_family,
        operator_ack=True,
        risk_notes=risk_notes,
        actor=act,
    )
    if not res.get("ok"):
        return res
    rid = str(res.get("revision_id") or "")
    aid = _record_operator_row(
        conn,
        actor=act,
        action_kind="approve_bounded_action",
        subject_key=sk,
        scenario_id=str(scenario_id or ""),
        trigger=str(trigger or ""),
        requested_level="bounded_action",
        decision="approved",
        revision_id=rid,
        reason=risk_notes,
        payload={"action_family": str(action_family or ""), "gate": gate},
    )
    _append_rollout_event(
        conn,
        EventType.ROLLOUT_DECISION_RECORDED,
        {
            "decision": "approve_bounded_action",
            "subject_key": sk,
            "revision_id": rid,
            "action_family": str(action_family or ""),
            "actor": act,
            "rollout_manager_version": ROLLOUT_MANAGER_VERSION,
        },
    )
    return {**res, "operator_action_id": aid, "expansion_gate": gate}


def persist_live_shadow_comparison(
    conn: sqlite3.Connection,
    *,
    subject_key: str,
    revision_id: str = "",
    window_start: float = 0.0,
    window_end: float = 0.0,
    baseline: Optional[Dict[str, Any]] = None,
    shadow: Optional[Dict[str, Any]] = None,
    live: Optional[Dict[str, Any]] = None,
    deltas: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from .store import insert_live_shadow_comparison_record

    cid = _new_action_id("lsc")
    insert_live_shadow_comparison_record(
        conn,
        comparison_id=cid,
        subject_key=subject_key,
        revision_id=revision_id,
        window_start=window_start,
        window_end=window_end,
        baseline=baseline,
        shadow=shadow,
        live=live,
        deltas=deltas,
        payload=payload,
    )
    _append_rollout_event(
        conn,
        EventType.LIVE_SHADOW_COMPARISON_RECORDED,
        {
            "comparison_id": cid,
            "subject_key": str(subject_key or ""),
            "revision_id": str(revision_id or ""),
            "rollout_manager_version": ROLLOUT_MANAGER_VERSION,
        },
    )
    return {"ok": True, "comparison_id": cid}


def build_comparison_from_subject(
    conn: sqlite3.Connection, *, scenario_id: str, trigger: str
) -> Dict[str, Any]:
    from .collaboration_promotion import live_shadow_comparison_snapshot, subject_key
    from .store import fetch_active_promotion_revision

    sk = subject_key(scenario_id, trigger)
    snap = live_shadow_comparison_snapshot(conn, scenario_id=scenario_id, trigger=trigger)
    rev = fetch_active_promotion_revision(conn, sk)
    rid = str(rev["revision_id"] or "") if rev else ""
    now = time.time()
    base_stats = dict(snap.get("outcome_stats") or {})
    shadow_hint = {
        "activation_shadow_suppress_count": snap.get("activation_shadow_suppress_count"),
        "activation_live_planned_count": snap.get("activation_live_planned_count"),
    }
    live_hint = {"live_advisory_runs": base_stats.get("live_runs")}
    deltas = {
        "useful_rate": base_stats.get("useful_rate"),
        "harmful_rate": base_stats.get("harmful_rate"),
        "samples": base_stats.get("samples"),
    }
    return persist_live_shadow_comparison(
        conn,
        subject_key=sk,
        revision_id=rid,
        window_start=now - 86400.0 * 7,
        window_end=now,
        baseline={"outcome_stats": base_stats},
        shadow=shadow_hint,
        live=live_hint,
        deltas=deltas,
        payload={"source": "build_comparison_from_subject"},
    )


def expansion_gate_report(conn: sqlite3.Connection, subject_key: str) -> Dict[str, Any]:
    """
    Deterministic widening gate for bounded-action promotion (manual operator path only).
    """
    from .collaboration_promotion import (
        meets_bounded_action_promotion_evidence,
        meets_live_advisory_promotion_evidence,
        promotion_allowlist_subjects,
        effective_promotion_allowlist_subjects,
    )

    parts = str(subject_key or "").split("|", 1)
    scenario_id, trigger = (parts[0], parts[1]) if len(parts) == 2 else ("", "")
    base_allow = promotion_allowlist_subjects()
    eff_allow = effective_promotion_allowlist_subjects(conn)
    ok_adv, adv_stats, adv_errs = meets_live_advisory_promotion_evidence(
        conn, scenario_id=scenario_id, trigger=trigger
    )
    ok_ba, ba_meta, ba_errs = meets_bounded_action_promotion_evidence(
        conn, scenario_id=scenario_id, trigger=trigger
    )
    in_static_allowlist = str(subject_key or "").strip() in base_allow
    return {
        "subject_key": str(subject_key or ""),
        "scenario_id": scenario_id,
        "trigger": trigger,
        "in_static_allowlist": in_static_allowlist,
        "in_effective_allowlist": str(subject_key or "").strip() in eff_allow,
        "live_advisory_evidence_ok": ok_adv,
        "live_advisory_errors": adv_errs,
        "bounded_action_evidence_ok": ok_ba,
        "bounded_action_errors": ba_errs,
        "bounded_action_promotion_allowed": bool(ok_ba and in_static_allowlist),
        "notes": "Widening bounded actions requires static env allowlist plus evidence gates; "
        "dynamic grants apply to advisory subjects only.",
        "rollout_manager_version": ROLLOUT_MANAGER_VERSION,
    }


def build_rollout_workspace(conn: sqlite3.Connection) -> Dict[str, Any]:
    from .store import list_recent_operator_actions, list_recent_live_shadow_comparisons

    op_rows = list_recent_operator_actions(conn, limit=20)
    operator_actions: List[Dict[str, Any]] = []
    for r in op_rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        operator_actions.append(
            {
                "action_id": str(r["action_id"] or ""),
                "actor": str(r["actor"] or ""),
                "action_kind": str(r["action_kind"] or ""),
                "subject_key": str(r["subject_key"] or ""),
                "decision": str(r["decision"] or ""),
                "revision_id": str(r["revision_id"] or ""),
                "reason": str(r["reason"] or "")[:400],
                "ts": float(r["ts"] or 0.0),
                "payload": payload,
            }
        )

    cmp_rows = list_recent_live_shadow_comparisons(conn, limit=12)
    comparisons: List[Dict[str, Any]] = []
    for r in cmp_rows:
        try:
            deltas = json.loads(r["deltas_json"] or "{}")
        except json.JSONDecodeError:
            deltas = {}
        comparisons.append(
            {
                "comparison_id": str(r["comparison_id"] or ""),
                "subject_key": str(r["subject_key"] or ""),
                "revision_id": str(r["revision_id"] or ""),
                "ts": float(r["ts"] or 0.0),
                "deltas": deltas,
            }
        )

    grants = []
    try:
        from .store import list_active_rollout_subject_grants

        for g in list_active_rollout_subject_grants(conn):
            grants.append(
                {
                    "subject_key": str(g["subject_key"] or ""),
                    "actor": str(g["actor"] or ""),
                    "granted_at": float(g["granted_at"] or 0.0),
                    "notes": str(g["notes"] or "")[:300],
                }
            )
    except sqlite3.OperationalError:
        pass

    return {
        "rollout_manager_version": ROLLOUT_MANAGER_VERSION,
        "operator_actions_recent": operator_actions,
        "live_shadow_comparisons_recent": comparisons,
        "scenario_onboarding": list_scenario_onboarding_snapshot(conn),
        "dynamic_subject_grants": grants,
    }


def rollout_api_list_candidates(conn: sqlite3.Connection) -> Dict[str, Any]:
    from .collaboration_promotion import list_promotion_candidates, list_bounded_action_candidates

    live_c = list_promotion_candidates(conn)
    ba_c = list_bounded_action_candidates(conn)
    gated = []
    for c in ba_c:
        sk = str(c.get("subject_key") or "")
        gated.append({**c, "expansion_gate": expansion_gate_report(conn, sk)})
    return {
        "ok": True,
        "live_advisory_candidates": live_c,
        "bounded_action_candidates": gated,
        "rollout_manager_version": ROLLOUT_MANAGER_VERSION,
    }

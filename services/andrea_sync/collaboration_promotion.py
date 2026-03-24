"""
Evidence-gated live collaboration promotion controller.

Persisted promotion revisions, effective-policy hints for activation_policy,
deterministic rollback/freeze guardrails, and bounded-action promotion gates.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

import sqlite3

from .schema import EventType

PROMOTION_CONTROLLER_VERSION = "2026.03.promo.v1"

DEFAULT_PROMOTION_ALLOWLIST = frozenset(
    {
        "repoHelpVerified|verify_fail",
        "repoHelpVerified|trust_gate",
    }
)


def promotion_controller_enabled() -> bool:
    v = (os.environ.get("ANDREA_SYNC_COLLAB_PROMOTION_ENABLED") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def promotion_global_freeze() -> bool:
    v = (os.environ.get("ANDREA_SYNC_COLLAB_PROMOTION_FREEZE") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def promotion_allowlist_subjects() -> Set[str]:
    raw = (os.environ.get("ANDREA_SYNC_COLLAB_PROMOTION_ALLOWLIST") or "").strip()
    if not raw:
        return set(DEFAULT_PROMOTION_ALLOWLIST)
    parts = {p.strip() for p in raw.split(",") if p.strip()}
    return parts or set(DEFAULT_PROMOTION_ALLOWLIST)


def effective_promotion_allowlist_subjects(conn: Optional[sqlite3.Connection]) -> Set[str]:
    """Env/static allowlist plus operator-granted rollout subjects (persisted)."""
    base = promotion_allowlist_subjects()
    if conn is None:
        return base
    try:
        from .store import list_active_rollout_subject_grants

        extra = {
            str(r["subject_key"] or "").strip()
            for r in list_active_rollout_subject_grants(conn)
            if str(r["subject_key"] or "").strip()
        }
        return base | extra
    except sqlite3.OperationalError:
        return base


def promo_min_sample() -> int:
    try:
        return max(3, int(os.environ.get("ANDREA_SYNC_COLLAB_PROMOTION_MIN_SAMPLE", "20") or 20))
    except (TypeError, ValueError):
        return 20


def promo_min_useful_rate() -> float:
    try:
        return min(
            1.0,
            max(0.0, float(os.environ.get("ANDREA_SYNC_COLLAB_PROMOTION_MIN_USEFUL_RATE", "0.60") or 0.60)),
        )
    except (TypeError, ValueError):
        return 0.60


def promo_max_harm_rate_promote() -> float:
    try:
        return min(
            1.0,
            max(0.0, float(os.environ.get("ANDREA_SYNC_COLLAB_PROMOTION_MAX_HARM_RATE", "0.05") or 0.05)),
        )
    except (TypeError, ValueError):
        return 0.05


def rollback_enabled() -> bool:
    v = (os.environ.get("ANDREA_SYNC_COLLAB_ROLLBACK_ENABLED") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def rollback_harm_threshold() -> float:
    try:
        return min(
            1.0,
            max(0.0, float(os.environ.get("ANDREA_SYNC_COLLAB_ROLLBACK_MAX_HARM_RATE", "0.10") or 0.10)),
        )
    except (TypeError, ValueError):
        return 0.10


def rollback_regression_streak() -> int:
    try:
        return max(1, int(os.environ.get("ANDREA_SYNC_COLLAB_ROLLBACK_MAX_REGRESSION_STREAK", "2") or 2))
    except (TypeError, ValueError):
        return 2


def max_avg_role_delta_budget() -> Optional[float]:
    raw = (os.environ.get("ANDREA_SYNC_COLLAB_PROMOTION_MAX_AVG_ROLE_DELTA") or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def bounded_promotion_min_repair_executed() -> int:
    try:
        return max(3, int(os.environ.get("ANDREA_SYNC_COLLAB_BOUNDED_PROMOTION_MIN_REPAIR", "10") or 10))
    except (TypeError, ValueError):
        return 10


def subject_key(scenario_id: str, trigger: str) -> str:
    return f"{str(scenario_id or '').strip()}|{str(trigger or '').strip()}"


def fetch_pair_outcome_stats(
    conn: sqlite3.Connection, *, scenario_id: str, trigger: str
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "samples": 0,
        "useful": 0,
        "wasteful": 0,
        "harmful": 0,
        "informational": 0,
        "live_runs": 0,
        "avg_roles": 0.0,
        "useful_rate": 0.0,
        "harmful_rate": 0.0,
        "waste_rate": 0.0,
    }
    try:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS n,
              SUM(CASE WHEN canonical_class = 'useful' THEN 1 ELSE 0 END) AS useful,
              SUM(CASE WHEN canonical_class = 'wasteful' THEN 1 ELSE 0 END) AS wasteful,
              SUM(CASE WHEN canonical_class = 'harmful' THEN 1 ELSE 0 END) AS harmful,
              SUM(CASE WHEN canonical_class = 'informational' THEN 1 ELSE 0 END) AS informational,
              SUM(CASE WHEN live_advisory_ran THEN 1 ELSE 0 END) AS live_runs,
              AVG(role_invocation_delta) AS avg_roles
            FROM collaboration_outcomes
            WHERE scenario_id = ? AND trigger = ?
            """,
            (str(scenario_id or ""), str(trigger or "")),
        ).fetchone()
    except sqlite3.OperationalError:
        return out
    if not row:
        return out
    n = int(row["n"] or 0)
    out["samples"] = n
    useful = int(row["useful"] or 0)
    wasteful = int(row["wasteful"] or 0)
    harmful = int(row["harmful"] or 0)
    informational = int(row["informational"] or 0)
    out["useful"] = useful
    out["wasteful"] = wasteful
    out["harmful"] = harmful
    out["informational"] = informational
    out["live_runs"] = int(row["live_runs"] or 0)
    out["avg_roles"] = float(row["avg_roles"] or 0.0)
    if n > 0:
        out["useful_rate"] = round(float(useful) / float(n), 4)
        out["harmful_rate"] = round(float(harmful) / float(n), 4)
        out["waste_rate"] = round(float(wasteful + harmful) / float(n), 4)
    return out


def _recent_harmful_streak(
    conn: sqlite3.Connection, *, scenario_id: str, trigger: str, limit: int
) -> int:
    try:
        rows = conn.execute(
            """
            SELECT canonical_class
            FROM collaboration_outcomes
            WHERE scenario_id = ? AND trigger = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (str(scenario_id or ""), str(trigger or ""), max(1, int(limit))),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    streak = 0
    for r in rows or []:
        if str(r["canonical_class"] or "") == "harmful":
            streak += 1
        else:
            break
    return streak


def _repair_executed_stats(conn: sqlite3.Connection) -> Tuple[int, int]:
    """Return (executed_count, success_heuristic_count) for bounded-action evidence."""
    try:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS n,
              SUM(CASE WHEN executed = 1 THEN 1 ELSE 0 END) AS ex
            FROM repair_outcomes
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return 0, 0
    if not row:
        return 0, 0
    total = int(row["n"] or 0)
    executed = int(row["ex"] or 0)
    return executed, total


def _strategy_to_action_family(strategy: str) -> str:
    s = str(strategy or "").strip().lower().replace("-", "_")
    if s == "switch_lane":
        return "switch_lane"
    if s in ("retry_same", "retry_same_lane"):
        return "retry_same_lane"
    if s in ("invoke_repair_cycle", "incident_escalation_hint", "repair_cycle"):
        return "invoke_repair_cycle"
    return ""


def get_promotion_activation_overlay(
    conn: Optional[sqlite3.Connection], scenario_id: str, trigger: str
) -> Dict[str, Any]:
    """
    Fields consumed by activation_policy.evaluate_activation_policy.

    effective_shadow_only:
      None -> use env ANDREA_SYNC_COLLAB_POLICY_SHADOW_ONLY
      True/False -> override for this subject (promoted live advisory enforces adaptive suppress)
    """
    sid = str(scenario_id or "").strip()
    trig = str(trigger or "").strip()
    sk = subject_key(sid, trig)
    base: Dict[str, Any] = {
        "promotion_controller_version": PROMOTION_CONTROLLER_VERSION,
        "promotion_controller_enabled": promotion_controller_enabled(),
        "subject_key": sk,
        "effective_shadow_only": None,
        "freeze_live_advisory": False,
        "promotion_revision_id": "",
        "promotion_level": "shadow_only",
        "promotion_status": "none",
        "bounded_action_family": "",
        "promotion_overlay_reasons": [],
    }
    if not promotion_controller_enabled() or conn is None:
        return base

    if promotion_global_freeze():
        base["freeze_live_advisory"] = True
        base["promotion_overlay_reasons"].append("global_promotion_freeze")
        return base

    from .store import fetch_active_promotion_revision

    rev = fetch_active_promotion_revision(conn, sk)
    if not rev:
        base["promotion_status"] = "none"
        return base

    level = str(rev["promotion_level"] or "")
    status = str(rev["status"] or "")
    base["promotion_revision_id"] = str(rev["revision_id"] or "")
    base["promotion_level"] = level
    base["promotion_status"] = status

    try:
        payload = json.loads(rev["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        base["bounded_action_family"] = str(payload.get("action_family") or "")

    if status != "active":
        base["promotion_overlay_reasons"].append("inactive_revision")
        return base

    if level == "frozen":
        base["freeze_live_advisory"] = True
        base["promotion_overlay_reasons"].append("subject_frozen")
        return base

    if level in ("live_advisory", "bounded_action"):
        if not int(rev["operator_ack"] or 0):
            base["promotion_overlay_reasons"].append("missing_operator_ack")
            return base
        base["effective_shadow_only"] = False
        base["promotion_overlay_reasons"].append("promoted_enforce_adaptive")
        return base

    return base


def meets_live_advisory_promotion_evidence(
    conn: sqlite3.Connection, *, scenario_id: str, trigger: str
) -> Tuple[bool, Dict[str, Any], List[str]]:
    stats = fetch_pair_outcome_stats(conn, scenario_id=scenario_id, trigger=trigger)
    errs: List[str] = []
    n = int(stats.get("samples") or 0)
    if n < promo_min_sample():
        errs.append(f"samples_below_min:{n}<{promo_min_sample()}")
    useful_rate = float(stats.get("useful_rate") or 0.0)
    if useful_rate + 1e-9 < promo_min_useful_rate():
        errs.append(f"useful_rate_low:{useful_rate}<{promo_min_useful_rate()}")
    harmful_rate = float(stats.get("harmful_rate") or 0.0)
    if harmful_rate > promo_max_harm_rate_promote() + 1e-9:
        errs.append(f"harm_rate_high:{harmful_rate}>{promo_max_harm_rate_promote()}")
    budget = max_avg_role_delta_budget()
    if budget is not None and float(stats.get("avg_roles") or 0.0) > budget + 1e-9:
        errs.append(f"avg_role_delta_over_budget:{stats.get('avg_roles')}>{budget}")
    return (len(errs) == 0), stats, errs


def meets_bounded_action_promotion_evidence(
    conn: sqlite3.Connection, *, scenario_id: str, trigger: str
) -> Tuple[bool, Dict[str, Any], List[str]]:
    ok_adv, adv_stats, adv_errs = meets_live_advisory_promotion_evidence(
        conn, scenario_id=scenario_id, trigger=trigger
    )
    errs = list(adv_errs)
    executed, _total = _repair_executed_stats(conn)
    if executed < bounded_promotion_min_repair_executed():
        errs.append(
            f"repair_executed_below_min:{executed}<{bounded_promotion_min_repair_executed()}"
        )
    # Coarse success proxy: useful advisory rate already gates; add repair success rate if we can.
    ok = len(errs) == 0
    meta = {"advisory_stats": adv_stats, "repair_executed": executed}
    return ok, meta, errs


def list_promotion_candidates(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Subjects in allowlist that meet promotion evidence and are not already promoted."""
    from .store import fetch_active_promotion_revision

    out: List[Dict[str, Any]] = []
    for sk in sorted(promotion_allowlist_subjects()):
        parts = sk.split("|", 1)
        if len(parts) != 2:
            continue
        scenario_id, trigger = parts[0], parts[1]
        rev = fetch_active_promotion_revision(conn, sk)
        if rev and str(rev["promotion_level"] or "") in ("live_advisory", "bounded_action"):
            if int(rev["operator_ack"] or 0) and str(rev["status"] or "") == "active":
                continue
        ok, stats, errs = meets_live_advisory_promotion_evidence(
            conn, scenario_id=scenario_id, trigger=trigger
        )
        if ok:
            out.append(
                {
                    "subject_key": sk,
                    "scenario_id": scenario_id,
                    "trigger": trigger,
                    "kind": "live_advisory_promotion_candidate",
                    "stats": stats,
                }
            )
    return out


def list_bounded_action_candidates(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    from .store import fetch_active_promotion_revision

    out: List[Dict[str, Any]] = []
    for sk in sorted(effective_promotion_allowlist_subjects(conn)):
        parts = sk.split("|", 1)
        if len(parts) != 2:
            continue
        scenario_id, trigger = parts[0], parts[1]
        rev = fetch_active_promotion_revision(conn, sk)
        if rev and str(rev["promotion_level"] or "") == "bounded_action":
            if int(rev["operator_ack"] or 0) and str(rev["status"] or "") == "active":
                continue
        ok, meta, errs = meets_bounded_action_promotion_evidence(
            conn, scenario_id=scenario_id, trigger=trigger
        )
        if ok:
            out.append(
                {
                    "subject_key": sk,
                    "scenario_id": scenario_id,
                    "trigger": trigger,
                    "kind": "bounded_action_promotion_candidate",
                    "meta": meta,
                    "errors_if_any": errs,
                }
            )
    return out


def _ensure_system_task(conn: sqlite3.Connection) -> None:
    from .store import SYSTEM_TASK_ID, create_task, task_exists

    if not task_exists(conn, SYSTEM_TASK_ID):
        create_task(conn, SYSTEM_TASK_ID, "internal")


def _append_promotion_event(conn: sqlite3.Connection, event_type: EventType, payload: Dict[str, Any]) -> None:
    from .store import SYSTEM_TASK_ID, append_event

    _ensure_system_task(conn)
    append_event(conn, SYSTEM_TASK_ID, event_type, payload)


def promote_subject_live_advisory(
    conn: sqlite3.Connection,
    *,
    scenario_id: str,
    trigger: str,
    operator_ack: bool = True,
    risk_notes: str = "",
    actor: str = "",
) -> Dict[str, Any]:
    """Record a promotion revision after evidence + allowlist checks (operator tooling / tests)."""
    from .store import (
        fetch_active_promotion_revision,
        insert_collaboration_promotion_revision,
        supersede_active_promotion_revisions,
    )

    sk = subject_key(scenario_id, trigger)
    if sk not in effective_promotion_allowlist_subjects(conn):
        return {"ok": False, "error": "subject_not_in_allowlist", "subject_key": sk}
    ok, stats, errs = meets_live_advisory_promotion_evidence(conn, scenario_id=scenario_id, trigger=trigger)
    if not ok:
        return {"ok": False, "error": "evidence_gate_failed", "details": errs, "stats": stats}
    if not operator_ack:
        return {"ok": False, "error": "operator_ack_required"}

    prior = fetch_active_promotion_revision(conn, sk)
    fallback = str(prior["revision_id"] or "") if prior else ""
    supersede_active_promotion_revisions(conn, sk)
    new_id = f"prm-{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}"
    insert_collaboration_promotion_revision(
        conn,
        revision_id=new_id,
        subject_key=sk,
        promotion_level="live_advisory",
        status="active",
        operator_ack=1,
        fallback_revision_id=fallback,
        evidence_snapshot=dict(stats),
        risk_notes=risk_notes,
        payload={"promoted": "live_advisory"},
    )
    _append_promotion_event(
        conn,
        EventType.PROMOTION_DECISION_RECORDED,
        {
            "decision": "promote",
            "promotion_level": "live_advisory",
            "subject_key": sk,
            "revision_id": new_id,
            "operator_ack": True,
            "evidence_snapshot": stats,
            "controller_version": PROMOTION_CONTROLLER_VERSION,
            "actor": str(actor or "").strip()[:200],
        },
    )
    return {"ok": True, "revision_id": new_id, "subject_key": sk, "stats": stats}


def promote_subject_bounded_action(
    conn: sqlite3.Connection,
    *,
    scenario_id: str,
    trigger: str,
    action_family: str,
    operator_ack: bool = True,
    risk_notes: str = "",
    actor: str = "",
) -> Dict[str, Any]:
    from .store import (
        fetch_active_promotion_revision,
        insert_collaboration_promotion_revision,
        supersede_active_promotion_revisions,
    )

    sk = subject_key(scenario_id, trigger)
    if sk not in effective_promotion_allowlist_subjects(conn):
        return {"ok": False, "error": "subject_not_in_allowlist", "subject_key": sk}
    fam = str(action_family or "").strip()
    if fam not in ("switch_lane", "retry_same_lane", "invoke_repair_cycle"):
        return {"ok": False, "error": "invalid_action_family"}
    ok, meta, errs = meets_bounded_action_promotion_evidence(conn, scenario_id=scenario_id, trigger=trigger)
    if not ok:
        return {"ok": False, "error": "evidence_gate_failed", "details": errs, "meta": meta}
    if not operator_ack:
        return {"ok": False, "error": "operator_ack_required"}

    prior = fetch_active_promotion_revision(conn, sk)
    fallback = str(prior["revision_id"] or "") if prior else ""
    supersede_active_promotion_revisions(conn, sk)
    new_id = f"prm-{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}"
    insert_collaboration_promotion_revision(
        conn,
        revision_id=new_id,
        subject_key=sk,
        promotion_level="bounded_action",
        status="active",
        operator_ack=1,
        fallback_revision_id=fallback,
        evidence_snapshot=dict(meta),
        risk_notes=risk_notes,
        payload={"promoted": "bounded_action", "action_family": fam},
    )
    _append_promotion_event(
        conn,
        EventType.PROMOTION_DECISION_RECORDED,
        {
            "decision": "promote",
            "promotion_level": "bounded_action",
            "subject_key": sk,
            "revision_id": new_id,
            "action_family": fam,
            "operator_ack": True,
            "evidence_snapshot": meta,
            "controller_version": PROMOTION_CONTROLLER_VERSION,
            "actor": str(actor or "").strip()[:200],
        },
    )
    return {"ok": True, "revision_id": new_id, "subject_key": sk, "meta": meta}


def freeze_subject(
    conn: sqlite3.Connection,
    *,
    scenario_id: str,
    trigger: str,
    reason_codes: List[str],
    actor: str = "",
) -> Dict[str, Any]:
    from .store import (
        fetch_active_promotion_revision,
        insert_collaboration_promotion_revision,
        supersede_active_promotion_revisions,
    )

    sk = subject_key(scenario_id, trigger)
    prior = fetch_active_promotion_revision(conn, sk)
    fallback = str(prior["revision_id"] or "") if prior else ""
    supersede_active_promotion_revisions(conn, sk)
    new_id = f"frz-{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}"
    insert_collaboration_promotion_revision(
        conn,
        revision_id=new_id,
        subject_key=sk,
        promotion_level="frozen",
        status="active",
        operator_ack=0,
        fallback_revision_id=fallback,
        evidence_snapshot={"reason_codes": reason_codes},
        risk_notes=",".join(reason_codes)[:2000],
        payload={"freeze": True},
    )
    _append_promotion_event(
        conn,
        EventType.PROMOTION_DECISION_RECORDED,
        {
            "decision": "freeze",
            "subject_key": sk,
            "revision_id": new_id,
            "reason_codes": reason_codes,
            "controller_version": PROMOTION_CONTROLLER_VERSION,
            "actor": str(actor or "").strip()[:200],
        },
    )
    return {"ok": True, "revision_id": new_id}


def rollback_subject_operator(
    conn: sqlite3.Connection,
    *,
    scenario_id: str,
    trigger: str,
    reason_codes: Optional[List[str]] = None,
    actor: str = "",
) -> Dict[str, Any]:
    """Manual operator rollback from live_advisory or bounded_action to shadow_only."""
    from .store import fetch_active_promotion_revision

    sk = subject_key(scenario_id, trigger)
    prior = fetch_active_promotion_revision(conn, sk)
    if not prior:
        return {"ok": False, "error": "no_active_revision", "subject_key": sk}
    level = str(prior["promotion_level"] or "")
    if level not in ("live_advisory", "bounded_action"):
        return {
            "ok": False,
            "error": "rollback_not_applicable",
            "subject_key": sk,
            "promotion_level": level,
        }
    codes = list(reason_codes or ["operator_rollback"])
    _apply_rollback(
        conn,
        subject_key=sk,
        prior_row=prior,
        reason_codes=codes,
        trigger_type="operator",
        observed={"actor": str(actor or "").strip()[:200]},
    )
    return {"ok": True, "subject_key": sk, "prior_level": level}


def _apply_rollback(
    conn: sqlite3.Connection,
    *,
    subject_key: str,
    prior_row: sqlite3.Row,
    reason_codes: List[str],
    trigger_type: str,
    observed: Optional[Dict[str, Any]] = None,
) -> None:
    from .store import (
        insert_collaboration_promotion_revision,
        insert_collaboration_promotion_rollback,
        supersede_active_promotion_revisions,
    )

    rid = str(prior_row["revision_id"] or "")
    supersede_active_promotion_revisions(conn, subject_key)
    new_id = f"rbk-{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}"
    insert_collaboration_promotion_revision(
        conn,
        revision_id=new_id,
        subject_key=subject_key,
        promotion_level="shadow_only",
        status="active",
        operator_ack=0,
        fallback_revision_id=str(prior_row["fallback_revision_id"] or ""),
        evidence_snapshot={"rolled_back_from": rid, "reason_codes": reason_codes},
        risk_notes=",".join(reason_codes)[:2000],
        payload={"rollback": True, "prior_revision": rid},
    )
    insert_collaboration_promotion_rollback(
        conn,
        revision_id=rid,
        subject_key=subject_key,
        trigger_type=trigger_type,
        observed=observed or {},
        fallback_revision_id=new_id,
        reason_codes=reason_codes,
    )
    _append_promotion_event(
        conn,
        EventType.PROMOTION_ROLLBACK_RECORDED,
        {
            "subject_key": subject_key,
            "prior_revision_id": rid,
            "fallback_revision_id": new_id,
            "trigger_type": trigger_type,
            "reason_codes": reason_codes,
            "controller_version": PROMOTION_CONTROLLER_VERSION,
        },
    )


def evaluate_promotion_guardrails_after_outcome(
    conn: sqlite3.Connection,
    *,
    scenario_id: str,
    trigger: str,
    canonical_class: str,
) -> None:
    """Call after collaboration_outcomes insert; may rollback or freeze promoted subjects."""
    if not promotion_controller_enabled() or not rollback_enabled():
        return

    from .store import fetch_active_promotion_revision

    sk = subject_key(scenario_id, trigger)
    rev = fetch_active_promotion_revision(conn, sk)
    if not rev:
        return
    level = str(rev["promotion_level"] or "")
    if level not in ("live_advisory", "bounded_action"):
        return
    if str(rev["status"] or "") != "active":
        return

    cclass = str(canonical_class or "")
    if cclass == "harmful":
        _apply_rollback(
            conn,
            subject_key=sk,
            prior_row=rev,
            reason_codes=["trust_incident_harmful_outcome"],
            trigger_type="harmful_outcome",
            observed={"canonical_class": cclass},
        )
        return

    stats = fetch_pair_outcome_stats(conn, scenario_id=scenario_id, trigger=trigger)
    n = int(stats.get("samples") or 0)
    if n >= promo_min_sample():
        hr = float(stats.get("harmful_rate") or 0.0)
        if hr > rollback_harm_threshold() + 1e-9:
            _apply_rollback(
                conn,
                subject_key=sk,
                prior_row=rev,
                reason_codes=[f"harmful_rate_regression:{hr}>{rollback_harm_threshold()}"],
                trigger_type="harmful_rate_window",
                observed=dict(stats),
            )
            return

    streak_need = rollback_regression_streak()
    streak = _recent_harmful_streak(
        conn, scenario_id=scenario_id, trigger=trigger, limit=streak_need
    )
    if streak >= streak_need:
        _apply_rollback(
            conn,
            subject_key=sk,
            prior_row=rev,
            reason_codes=[f"harmful_streak:{streak}>={streak_need}"],
            trigger_type="harmful_streak",
            observed={"streak": streak},
        )
        return

    budget = max_avg_role_delta_budget()
    if budget is not None and n >= promo_min_sample():
        if float(stats.get("avg_roles") or 0.0) > budget + 1e-9:
            freeze_subject(
                conn,
                scenario_id=scenario_id,
                trigger=trigger,
                reason_codes=[f"cost_regression_avg_roles:{stats.get('avg_roles')}>{budget}"],
            )


def build_trusted_promotion_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    from .store import list_active_promotion_revisions, list_recent_promotion_rollbacks

    active_rows = list_active_promotion_revisions(conn)
    active: List[Dict[str, Any]] = []
    frozen_subjects: List[str] = []
    promoted_live: List[str] = []
    bounded: List[str] = []
    for r in active_rows:
        sk = str(r["subject_key"] or "")
        entry = {
            "subject_key": sk,
            "revision_id": str(r["revision_id"] or ""),
            "promotion_level": str(r["promotion_level"] or ""),
            "status": str(r["status"] or ""),
            "operator_ack": bool(int(r["operator_ack"] or 0)),
            "created_at": float(r["created_at"] or 0.0),
            "risk_notes": str(r["risk_notes"] or "")[:300],
        }
        active.append(entry)
        lvl = entry["promotion_level"]
        if lvl == "frozen":
            frozen_subjects.append(sk)
        elif lvl == "live_advisory":
            promoted_live.append(sk)
        elif lvl == "bounded_action":
            bounded.append(sk)

    roll_rows = list_recent_promotion_rollbacks(conn, limit=8)
    recent_rollbacks: List[Dict[str, Any]] = []
    for r in roll_rows:
        try:
            reasons = json.loads(r["reason_codes_json"] or "[]")
        except json.JSONDecodeError:
            reasons = []
        recent_rollbacks.append(
            {
                "subject_key": str(r["subject_key"] or ""),
                "revision_id": str(r["revision_id"] or ""),
                "trigger_type": str(r["trigger_type"] or ""),
                "reason_codes": reasons,
                "ts": float(r["ts"] or 0.0),
            }
        )

    candidates = list_promotion_candidates(conn)
    bounded_candidates = list_bounded_action_candidates(conn)

    return {
        "promotion_controller_version": PROMOTION_CONTROLLER_VERSION,
        "promotion_controller_enabled": promotion_controller_enabled(),
        "promotion_global_freeze": promotion_global_freeze(),
        "rollback_enabled": rollback_enabled(),
        "active_promotions": active,
        "promoted_live_advisory_subjects": promoted_live,
        "promoted_bounded_action_subjects": bounded,
        "frozen_subjects": frozen_subjects,
        "promotion_candidates": candidates,
        "bounded_action_promotion_candidates": bounded_candidates,
        "recent_rollbacks": recent_rollbacks,
        "allowlist": sorted(promotion_allowlist_subjects()),
        "effective_allowlist": sorted(effective_promotion_allowlist_subjects(conn)),
    }


def summarize_for_metrics(conn: sqlite3.Connection) -> Dict[str, Any]:
    snap = build_trusted_promotion_summary(conn)
    return {
        "promotion_enabled": snap.get("promotion_controller_enabled"),
        "active_count": len(snap.get("active_promotions") or []),
        "candidates_count": len(snap.get("promotion_candidates") or []),
        "rollbacks_recent": len(snap.get("recent_rollbacks") or []),
    }


def bounded_action_promotion_allows(
    conn: Optional[sqlite3.Connection],
    *,
    scenario_id: str,
    trigger: str,
    strategy: str,
) -> bool:
    """
    When promotion controller is enabled, bounded actions require an active bounded_action
    revision with matching action_family. When disabled, legacy env-only gates apply.
    """
    if not promotion_controller_enabled():
        return True
    if conn is None:
        return False
    from .store import fetch_active_promotion_revision

    sk = subject_key(scenario_id, trigger)
    rev = fetch_active_promotion_revision(conn, sk)
    if not rev:
        return False
    if str(rev["status"] or "") != "active":
        return False
    if str(rev["promotion_level"] or "") != "bounded_action":
        return False
    if not int(rev["operator_ack"] or 0):
        return False
    try:
        payload = json.loads(rev["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    allowed = str((payload or {}).get("action_family") or "").strip()
    fam = _strategy_to_action_family(strategy)
    if not fam:
        return False
    return allowed == fam


def live_shadow_comparison_snapshot(
    conn: sqlite3.Connection, *, scenario_id: str, trigger: str
) -> Dict[str, Any]:
    """Lightweight live-vs-shadow hint using activation decisions + outcomes (deterministic)."""
    stats = fetch_pair_outcome_stats(conn, scenario_id=scenario_id, trigger=trigger)
    shadow_suppress = 0
    live_planned = 0
    try:
        row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN json_extract(payload_json, '$.shadow_recommended_suppress_live') = 1
                  THEN 1 ELSE 0 END) AS s,
              SUM(CASE WHEN json_extract(payload_json, '$.executed_live_advisory_planned') = 1
                  THEN 1 ELSE 0 END) AS l
            FROM collaboration_activation_decisions
            WHERE scenario_id = ? AND trigger = ?
            """,
            (str(scenario_id or ""), str(trigger or "")),
        ).fetchone()
        if row:
            shadow_suppress = int(row["s"] or 0)
            live_planned = int(row["l"] or 0)
    except sqlite3.OperationalError:
        pass
    return {
        "subject_key": subject_key(scenario_id, trigger),
        "outcome_stats": stats,
        "activation_shadow_suppress_count": shadow_suppress,
        "activation_live_planned_count": live_planned,
    }

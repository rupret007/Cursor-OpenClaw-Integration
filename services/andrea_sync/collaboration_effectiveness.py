"""Canonical collaboration usefulness judgments, outcome payloads, and profile rollups."""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List, Optional

from .activation_policy import ACTIVATION_POLICY_VERSION

# Map measured conductor usefulness buckets -> canonical classes for policy learning.
_WASTEFUL_PREFIXES = ("wasteful_",)
_USEFUL_MARKERS = (
    "useful_",
    "useful_safety_escalation",
    "useful_strategy_shift",
)
_INFORMATIONAL = ("informational_",)


def canonical_usefulness_class(detail_bucket: str) -> str:
    b = str(detail_bucket or "").strip().lower()
    if not b:
        return "informational"
    if b.startswith(_WASTEFUL_PREFIXES) or "wasteful" in b:
        return "wasteful"
    if "harmful" in b:
        return "harmful"
    for m in _USEFUL_MARKERS:
        if b.startswith(m) or b == m.rstrip("_"):
            return "useful"
    if b.startswith(_INFORMATIONAL):
        return "informational"
    # Default conservative: unknown detailed buckets treated as informational signal.
    return "informational"


def build_collaboration_outcome_payload(
    *,
    task_id: str,
    goal_id: str,
    plan_id: str,
    step_id: str,
    collab_id: str,
    scenario_id: str,
    trigger: str,
    verdict_before: str,
    verification_method: str,
    advisory_source: str,
    usefulness_detail: str,
    final_strategy: str,
    bounded_action_type: str,
    live_advisory_ran: bool,
    role_invocation_delta: int,
    policy_version: str = ACTIVATION_POLICY_VERSION,
) -> Dict[str, Any]:
    cclass = canonical_usefulness_class(usefulness_detail)
    return {
        "task_id": task_id,
        "goal_id": goal_id,
        "plan_id": plan_id,
        "step_id": step_id,
        "collab_id": collab_id,
        "scenario_id": str(scenario_id or ""),
        "trigger": str(trigger or ""),
        "verdict_before": str(verdict_before or ""),
        "verification_method": str(verification_method or ""),
        "advisory_source": str(advisory_source or ""),
        "usefulness_detail": str(usefulness_detail or "")[:160],
        "canonical_class": cclass,
        "confidence_band": "deterministic_v1",
        "final_strategy": str(final_strategy or "")[:120],
        "bounded_action_type": str(bounded_action_type or "")[:80],
        "live_advisory_ran": bool(live_advisory_ran),
        "role_invocation_delta": int(role_invocation_delta or 0),
        "policy_version": policy_version,
        "recorded_at": time.time(),
    }


def build_repair_outcome_payload(
    *,
    task_id: str,
    plan_id: str,
    collab_id: str,
    action_type: str,
    executed: bool,
    from_lane: str = "",
    to_lane: str = "",
    dispatch_kind: str = "",
    verdict_after: str = "",
) -> Dict[str, Any]:
    return {
        "task_id": task_id,
        "plan_id": plan_id,
        "collab_id": collab_id,
        "action_type": str(action_type or "")[:80],
        "executed": bool(executed),
        "from_lane": str(from_lane or "")[:80],
        "to_lane": str(to_lane or "")[:80],
        "dispatch_kind": str(dispatch_kind or "")[:80],
        "verdict_after": str(verdict_after or "")[:80],
        "recorded_at": time.time(),
    }


def rollup_collaboration_policy_profiles(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Scenario / trigger rollups for dashboard + optimizer hints."""
    out: Dict[str, Any] = {
        "ok": True,
        "scenario_profiles": [],
        "role_profiles": [],
        "activation_counts": {},
        "recommendation_signals": [],
    }
    try:
        rows = conn.execute(
            """
            SELECT scenario_id, trigger,
              COUNT(*) AS n,
              SUM(CASE WHEN canonical_class = 'useful' THEN 1 ELSE 0 END) AS useful,
              SUM(CASE WHEN canonical_class = 'wasteful' THEN 1 ELSE 0 END) AS wasteful,
              SUM(CASE WHEN canonical_class = 'harmful' THEN 1 ELSE 0 END) AS harmful,
              SUM(CASE WHEN canonical_class = 'informational' THEN 1 ELSE 0 END) AS informational,
              SUM(CASE WHEN live_advisory_ran THEN 1 ELSE 0 END) AS live_runs,
              AVG(role_invocation_delta) AS avg_roles
            FROM collaboration_outcomes
            GROUP BY scenario_id, trigger
            ORDER BY n DESC
            LIMIT 24
            """
        ).fetchall()
    except sqlite3.OperationalError:
        out["ok"] = False
        out["error"] = "collaboration_outcomes_table_missing"
        return out

    for row in rows or []:
        n = int(row["n"] or 0)
        if n <= 0:
            continue
        w = int(row["wasteful"] or 0) + int(row["harmful"] or 0)
        out["scenario_profiles"].append(
            {
                "scenario_id": str(row["scenario_id"] or ""),
                "trigger": str(row["trigger"] or ""),
                "samples": n,
                "useful_rate": round(float(row["useful"] or 0) / float(n), 4),
                "wasteful_rate": round(float(w) / float(n), 4),
                "live_advisory_rate": round(float(row["live_runs"] or 0) / float(n), 4),
                "avg_role_delta": round(float(row["avg_roles"] or 0.0), 3),
            }
        )

    try:
        act_rows = conn.execute(
            """
            SELECT activation_mode, COUNT(*) AS c
            FROM collaboration_activation_decisions
            GROUP BY activation_mode
            """
        ).fetchall()
        for r in act_rows or []:
            m = str(r["activation_mode"] or "") or "unknown"
            out["activation_counts"][m] = int(r["c"] or 0)
    except sqlite3.OperationalError:
        pass

    # Deterministic recommendation signals (operator-visible; not auto-applied).
    for prof in out["scenario_profiles"]:
        if int(prof.get("samples") or 0) >= 12 and float(prof.get("wasteful_rate") or 0) > 0.55:
            out["recommendation_signals"].append(
                {
                    "subject": f"{prof['scenario_id']}|{prof['trigger']}",
                    "kind": "suppress_live_advisory",
                    "evidence_samples": prof["samples"],
                    "wasteful_rate": prof["wasteful_rate"],
                    "notes": "High waste rate in ledger; consider shadow review before auto-suppression.",
                }
            )
    return out


def trusted_operator_summary(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Short calm rollup for operator surfaces."""
    from .assistant_domain_rollout import build_daily_pack_operator_snapshot
    from .collaboration_promotion import build_trusted_promotion_summary
    from .collaboration_rollout import build_rollout_workspace

    roll = rollup_collaboration_policy_profiles(conn)
    return {
        "ok": bool(roll.get("ok")),
        "policy_version": ACTIVATION_POLICY_VERSION,
        "scenario_profiles": roll.get("scenario_profiles") or [],
        "activation_counts": roll.get("activation_counts") or {},
        "recommendation_signals": roll.get("recommendation_signals") or [],
        "promotion_state": build_trusted_promotion_summary(conn),
        "rollout_workspace": build_rollout_workspace(conn),
        "daily_assistant_pack": build_daily_pack_operator_snapshot(conn),
    }


def policy_recommendation_event_payload(
    *,
    run_id: str,
    subject: str,
    kind: str,
    evidence: Dict[str, Any],
    notes: str = "",
) -> Dict[str, Any]:
    """Optional OPTIMIZATION_PROPOSAL companion — kept JSON-safe and reviewable."""
    return {
        "run_id": run_id,
        "kind": "collaboration_policy_recommendation",
        "subject": str(subject or "")[:200],
        "recommendation_type": str(kind or "")[:120],
        "evidence": evidence if isinstance(evidence, dict) else {},
        "notes": str(notes or "")[:800],
        "auto_safe": False,
        "policy_version": ACTIVATION_POLICY_VERSION,
    }

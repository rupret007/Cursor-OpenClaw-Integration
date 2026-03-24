"""
Deterministic collaboration activation policy (Evidence-Driven Adaptive Collaboration).

Records when collaboration would run, should be suppressed, or stays metadata-only.
Adaptive advisory suppression uses ledger rollups; shadow mode keeps legacy live behavior
while logging the recommended decision.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple

# Bump when deterministic rules change (operator dashboards / evals key on this).
ACTIVATION_POLICY_VERSION = "2026.03.v1"

MEASURED_SCENARIOS = frozenset({"repoHelpVerified", "verificationSensitiveAction"})


def collab_policy_recording_enabled() -> bool:
    v = (os.environ.get("ANDREA_SYNC_COLLAB_POLICY_ENABLED") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def collab_policy_shadow_only() -> bool:
    v = (os.environ.get("ANDREA_SYNC_COLLAB_POLICY_SHADOW_ONLY") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def collab_policy_min_sample() -> int:
    try:
        return max(3, int(os.environ.get("ANDREA_SYNC_COLLAB_POLICY_MIN_SAMPLE", "12") or 12))
    except (TypeError, ValueError):
        return 12


def collab_policy_max_waste_rate() -> float:
    try:
        return min(1.0, max(0.0, float(os.environ.get("ANDREA_SYNC_COLLAB_POLICY_MAX_WASTE_RATE", "0.55") or 0.55)))
    except (TypeError, ValueError):
        return 0.55


def measured_allowlist() -> Set[str]:
    raw = (os.environ.get("ANDREA_SYNC_COLLAB_POLICY_ALLOWLIST") or "").strip()
    if not raw:
        return set(MEASURED_SCENARIOS)
    parts = {p.strip() for p in raw.split(",") if p.strip()}
    return parts or set(MEASURED_SCENARIOS)


def _scenario_in_measured_pack(scenario_id: str) -> bool:
    sid = str(scenario_id or "").strip()
    return sid in measured_allowlist()


def fetch_outcome_stats_for_pair(
    conn: sqlite3.Connection, *, scenario_id: str, trigger: str
) -> Tuple[int, int]:
    """Return (wasteful_or_harmful_count, total_count) from collaboration_outcomes ledger."""
    try:
        row = conn.execute(
            """
            SELECT
              COUNT(*) AS n,
              SUM(CASE WHEN canonical_class IN ('wasteful', 'harmful') THEN 1 ELSE 0 END) AS w
            FROM collaboration_outcomes
            WHERE scenario_id = ? AND trigger = ?
            """,
            (str(scenario_id or ""), str(trigger or "")),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0, 0
    if not row:
        return 0, 0
    total = int(row["n"] or 0)
    wasteful = int(row["w"] or 0)
    return wasteful, total


def adaptive_recommends_suppress_live_advisory(
    conn: Optional[sqlite3.Connection], *, scenario_id: str, trigger: str
) -> Tuple[bool, Dict[str, Any]]:
    """
    If enough samples exist and wasteful rate exceeds max, recommend suppressing live advisory
    (metadata-only collaboration still applies).
    """
    meta: Dict[str, Any] = {
        "samples": 0,
        "wasteful": 0,
        "waste_rate": 0.0,
        "min_sample": collab_policy_min_sample(),
        "max_waste_rate": collab_policy_max_waste_rate(),
        "recommend_suppress": False,
    }
    if conn is None:
        return False, meta
    wasteful, total = fetch_outcome_stats_for_pair(conn, scenario_id=scenario_id, trigger=trigger)
    meta["samples"] = total
    meta["wasteful"] = wasteful
    if total <= 0:
        return False, meta
    rate = float(wasteful) / float(total)
    meta["waste_rate"] = round(rate, 4)
    if total >= collab_policy_min_sample() and rate > collab_policy_max_waste_rate():
        meta["recommend_suppress"] = True
        return True, meta
    return False, meta


def evaluate_activation_policy(
    *,
    conn: Optional[sqlite3.Connection],
    task_id: str,
    plan_id: str,
    step_id: str,
    scenario_id: str,
    trigger: str,
    verdict: str,
    lane: str,
    collab_id: str,
    collaboration_layer_on: bool,
    will_attach_collaboration_bundle: bool,
    attach_blocked_reasons: List[str],
    base_live_advisory_eligible: bool,
    approval_blocked: bool,
) -> Dict[str, Any]:
    """
    Produce a deterministic activation decision payload (stored + emitted as event).

    activation_mode (execution intent):
      - suppressed: measured opportunity but collaboration bundle not attached
      - record_only: bundle attached, live advisory not run (deterministic strategist only)
      - advisory: live advisory round may run (subject to runtime + gating)
      - action_candidate: bounded action is structurally allowed (separate execution gate)
    """
    sid = str(scenario_id or "").strip()
    reasons: List[str] = []
    operator_blockers: List[str] = []
    expected_gain_band = "unknown"
    cost_band = "medium" if base_live_advisory_eligible else "low"

    if not collab_policy_recording_enabled():
        return {
            "task_id": task_id,
            "plan_id": plan_id,
            "step_id": step_id,
            "scenario_id": sid,
            "trigger": str(trigger or ""),
            "verdict": str(verdict or ""),
            "lane": str(lane or ""),
            "collab_id": str(collab_id or ""),
            "policy_version": ACTIVATION_POLICY_VERSION,
            "activation_mode": "suppressed",
            "reason_codes": ["policy_recording_disabled"],
            "expected_gain_band": expected_gain_band,
            "cost_band": cost_band,
            "operator_blockers": [],
            "shadow_recommended_suppress_live": False,
            "shadow_only": collab_policy_shadow_only(),
            "adaptive_stats": {},
            "promotion_overlay": {},
        }

    if not _scenario_in_measured_pack(sid):
        reasons.append("scenario_not_in_measured_allowlist")
        return {
            "task_id": task_id,
            "plan_id": plan_id,
            "step_id": step_id,
            "scenario_id": sid,
            "trigger": str(trigger or ""),
            "verdict": str(verdict or ""),
            "lane": str(lane or ""),
            "collab_id": str(collab_id or ""),
            "policy_version": ACTIVATION_POLICY_VERSION,
            "activation_mode": "suppressed",
            "reason_codes": reasons,
            "expected_gain_band": expected_gain_band,
            "cost_band": "none",
            "operator_blockers": [],
            "shadow_recommended_suppress_live": False,
            "shadow_only": collab_policy_shadow_only(),
            "adaptive_stats": {},
            "promotion_overlay": {},
        }

    if not collaboration_layer_on:
        reasons.append("collaboration_layer_disabled")
        return {
            "task_id": task_id,
            "plan_id": plan_id,
            "step_id": step_id,
            "scenario_id": sid,
            "trigger": str(trigger or ""),
            "verdict": str(verdict or ""),
            "lane": str(lane or ""),
            "collab_id": str(collab_id or ""),
            "policy_version": ACTIVATION_POLICY_VERSION,
            "activation_mode": "suppressed",
            "reason_codes": reasons,
            "expected_gain_band": expected_gain_band,
            "cost_band": "none",
            "operator_blockers": [],
            "shadow_recommended_suppress_live": False,
            "shadow_only": collab_policy_shadow_only(),
            "adaptive_stats": {},
            "promotion_overlay": {},
        }

    if not will_attach_collaboration_bundle:
        reasons.extend(attach_blocked_reasons or ["collaboration_bundle_not_attached"])
        return {
            "task_id": task_id,
            "plan_id": plan_id,
            "step_id": step_id,
            "scenario_id": sid,
            "trigger": str(trigger or ""),
            "verdict": str(verdict or ""),
            "lane": str(lane or ""),
            "collab_id": str(collab_id or ""),
            "policy_version": ACTIVATION_POLICY_VERSION,
            "activation_mode": "suppressed",
            "reason_codes": reasons,
            "expected_gain_band": expected_gain_band,
            "cost_band": "none",
            "operator_blockers": [],
            "shadow_recommended_suppress_live": False,
            "shadow_only": collab_policy_shadow_only(),
            "adaptive_stats": {},
            "promotion_overlay": {},
        }

    if approval_blocked:
        operator_blockers.append("approval_pending_or_blocked")
        expected_gain_band = "low"

    recommend_suppress, adaptive_meta = adaptive_recommends_suppress_live_advisory(
        conn, scenario_id=sid, trigger=str(trigger or "")
    )
    shadow_recommended_suppress_live = bool(recommend_suppress)

    from .collaboration_promotion import get_promotion_activation_overlay

    promo_overlay = get_promotion_activation_overlay(conn, sid, str(trigger or ""))
    eff_shadow = promo_overlay.get("effective_shadow_only")
    if eff_shadow is None:
        shadow_only_flag = collab_policy_shadow_only()
    else:
        shadow_only_flag = bool(eff_shadow)

    # Execution intent: advisory if base eligibility and adaptive does not suppress (or shadow overrides).
    run_live = bool(base_live_advisory_eligible)
    if shadow_recommended_suppress_live and not shadow_only_flag:
        run_live = False
        reasons.append("adaptive_suppress_live_advisory")

    if promo_overlay.get("freeze_live_advisory"):
        run_live = False
        reasons.append("promotion_freeze_live_advisory")

    if conn is not None:
        from .collaboration_rollout import scenario_onboarding_blocks_live_advisory

        if scenario_onboarding_blocks_live_advisory(conn, sid):
            run_live = False
            reasons.append("scenario_onboarding_blocks_live_advisory")

    if run_live:
        mode = "advisory"
        expected_gain_band = "medium_high" if str(trigger or "") in ("verify_fail", "trust_gate") else "medium"
        cost_band = "high"
    else:
        mode = "record_only"
        if not base_live_advisory_eligible:
            reasons.append("live_advisory_not_eligible")

    if shadow_recommended_suppress_live and shadow_only_flag:
        reasons.append("adaptive_would_suppress_live")
        if run_live:
            reasons.append("shadow_keeps_live_advisory_for_compare")

    base_action_candidate = sid == "repoHelpVerified" and str(trigger or "") in (
        "verify_fail",
        "trust_gate",
    )
    action_candidate = bool(base_action_candidate)
    if action_candidate:
        from .collaboration_promotion import promotion_controller_enabled as _promo_on
        from .collaboration_promotion import subject_key as _promo_subject_key

        if _promo_on() and conn is not None:
            from .store import fetch_active_promotion_revision

            rev_ba = fetch_active_promotion_revision(conn, _promo_subject_key(sid, str(trigger or "")))
            if (
                not rev_ba
                or str(rev_ba["promotion_level"] or "") != "bounded_action"
                or not int(rev_ba["operator_ack"] or 0)
                or str(rev_ba["status"] or "") != "active"
            ):
                action_candidate = False

    out: Dict[str, Any] = {
        "task_id": task_id,
        "plan_id": plan_id,
        "step_id": step_id,
        "scenario_id": sid,
        "trigger": str(trigger or ""),
        "verdict": str(verdict or ""),
        "lane": str(lane or ""),
        "collab_id": str(collab_id or ""),
        "policy_version": ACTIVATION_POLICY_VERSION,
        "activation_mode": mode,
        "reason_codes": reasons,
        "expected_gain_band": expected_gain_band,
        "cost_band": cost_band,
        "operator_blockers": operator_blockers,
        "shadow_recommended_suppress_live": shadow_recommended_suppress_live,
        "shadow_only": collab_policy_shadow_only(),
        "effective_shadow_only_for_activation": shadow_only_flag,
        "adaptive_stats": adaptive_meta,
        "base_live_advisory_eligible": base_live_advisory_eligible,
        "executed_live_advisory_planned": run_live,
        "action_candidate": bool(action_candidate),
        "promotion_overlay": promo_overlay,
    }
    return out


def operator_action_promotion_confirmed() -> bool:
    """Separate operator ack for bounded collaboration actions (in addition to ACTION_ENABLED)."""
    v = (os.environ.get("ANDREA_SYNC_COLLAB_OPERATOR_ACTION_PROMOTION") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")

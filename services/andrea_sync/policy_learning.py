"""Measured, reviewable learning hooks (Phase 6 blueprint; opt-in metrics)."""
from __future__ import annotations

import sqlite3
from typing import Any, Dict

from .observability import metric_log


def record_routing_alignment(
    *,
    task_id: str,
    chosen_lane: str,
    top_suggested_lane: str,
    success: bool | None = None,
) -> Dict[str, Any]:
    """Emit a metric line when structured metrics logging is enabled."""
    aligned = bool(
        chosen_lane and top_suggested_lane and chosen_lane == top_suggested_lane
    )
    payload = {
        "task_id": task_id,
        "chosen": chosen_lane,
        "top_suggested": top_suggested_lane,
        "aligned": aligned,
    }
    if success is not None:
        payload["success"] = bool(success)
    metric_log("routing_alignment", **payload)
    return payload


def record_collaboration_policy_snapshot(
    conn: sqlite3.Connection, *, task_id: str = ""
) -> Dict[str, Any]:
    """Emit structured metrics for collaboration activation/outcome rollups (opt-in)."""
    from .collaboration_effectiveness import rollup_collaboration_policy_profiles
    from .collaboration_promotion import summarize_for_metrics
    from .collaboration_rollout import build_rollout_workspace

    roll = rollup_collaboration_policy_profiles(conn)
    promo = summarize_for_metrics(conn)
    rw = build_rollout_workspace(conn)
    from .assistant_domain_rollout import daily_pack_metrics_for_learning

    dpm = daily_pack_metrics_for_learning(conn)
    cr = dpm.get("followthrough_closure_rate_7d")
    metric_log(
        "collaboration_policy_snapshot",
        task_id=task_id,
        ok=str(bool(roll.get("ok"))).lower(),
        profile_rows=str(len(roll.get("scenario_profiles") or [])),
        promotion_enabled=str(bool(promo.get("promotion_enabled"))).lower(),
        promotion_active_count=str(int(promo.get("active_count") or 0)),
        promotion_candidates_count=str(int(promo.get("candidates_count") or 0)),
        promotion_rollbacks_recent=str(int(promo.get("rollbacks_recent") or 0)),
        rollout_operator_actions=str(len(rw.get("operator_actions_recent") or [])),
        rollout_comparisons=str(len(rw.get("live_shadow_comparisons_recent") or [])),
        daily_pack_receipt_count_7d=str(dpm.get("receipt_count_7d") or 0),
        daily_pack_evidence_ok=str(bool(dpm.get("live_evidence_ok"))).lower(),
        followthrough_closure_rate_7d=str(cr) if cr is not None else "",
        followthrough_open_loops_7d=str(dpm.get("followthrough_open_loops_7d") or 0),
        followthrough_needs_repair_7d=str(dpm.get("followthrough_needs_repair_7d") or 0),
    )
    return roll

"""Measured, reviewable learning hooks (Phase 6 blueprint; opt-in metrics)."""
from __future__ import annotations

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

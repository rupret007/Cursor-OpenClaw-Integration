"""Shared resource and verification vocabulary across Andrea lanes.

Use these strings in projections, dashboards, and internal summaries so direct,
delegated, repair, and proactive flows describe outcomes consistently.
"""
from __future__ import annotations

from typing import Any, Dict

# Where work was primarily executed (high-level).
RESOURCE_LANE_DIRECT = "direct_assistant"
RESOURCE_LANE_OPENCLAW = "openclaw_hybrid"
RESOURCE_LANE_CURSOR = "cursor"
RESOURCE_LANE_INCIDENT_REPAIR = "incident_repair"
RESOURCE_LANE_OPTIMIZER = "optimizer_self_heal"
RESOURCE_LANE_PROACTIVE = "proactive"
RESOURCE_LANE_UNKNOWN = "unknown"

# Verification / truth states (align with repair/self-heal outcome language where applicable).
VERIFICATION_NOT_ATTEMPTED = "not_attempted"
VERIFICATION_SKIPPED = "skipped"
VERIFICATION_PASSED = "passed"
VERIFICATION_FAILED = "failed"
VERIFICATION_UNVERIFIED = "unverified"


def infer_resource_lane(
    execution: Dict[str, Any] | None,
    *,
    route_mode: str = "",
) -> str:
    """Infer a coarse resource lane from execution meta and optional route hint."""
    ex = execution if isinstance(execution, dict) else {}
    lane = str(ex.get("execution_lane") or ex.get("runner") or "").strip().lower()
    rm = str(route_mode or "").strip().lower()
    if rm == "direct":
        return RESOURCE_LANE_DIRECT
    if "cursor" in lane or ex.get("delegated_to_cursor"):
        return RESOURCE_LANE_CURSOR
    if "openclaw" in lane or "hybrid" in lane:
        return RESOURCE_LANE_OPENCLAW
    if lane:
        return str(lane)[:64]
    return RESOURCE_LANE_UNKNOWN


def verification_story_from_outcome(outcome: Dict[str, Any] | None) -> str:
    """Short verification label from projected task outcome (delegated tasks)."""
    o = outcome if isinstance(outcome, dict) else {}
    if o.get("blocked_reason"):
        return VERIFICATION_UNVERIFIED
    # Completed delegated work without explicit verify is still "unverified" code-wise.
    terminal = str(o.get("terminal_status") or "").strip().upper()
    if terminal and terminal not in ("", "FINISHED"):
        return VERIFICATION_FAILED
    return VERIFICATION_UNVERIFIED

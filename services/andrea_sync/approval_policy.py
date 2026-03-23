"""Risk tiers and approval hints (Phase 2 blueprint; policy-as-data seed)."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .scenario_schema import SUPPORTED_APPROVAL
from .tool_registry import manifest_for_lane


def risk_tier_for_lane(lane: str) -> int:
    m = manifest_for_lane(lane)
    try:
        return int(m.get("risk_tier", 1))
    except (TypeError, ValueError):
        return 1


def evaluate_plan_step_approval(
    *,
    lane: str,
    step_kind: str,
    command_type: str = "",
    force_approval: bool = False,
    scenario: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Live step evaluation for governed execution. Returns needs_approval, rationale, risk_tier.
    """
    if force_approval:
        return {
            "needs_approval": True,
            "rationale": "forced_by_env",
            "risk_tier": risk_tier_for_lane(lane),
        }
    if isinstance(scenario, dict):
        sl = str(scenario.get("support_level") or "").strip()
        am = str(scenario.get("approval_mode") or "").strip().lower()
        if sl == SUPPORTED_APPROVAL or am == "required":
            return {
                "needs_approval": True,
                "rationale": "scenario_requires_approval",
                "risk_tier": max(risk_tier_for_lane(lane), 3),
            }
    m = manifest_for_lane(lane)
    tier = risk_tier_for_lane(lane)
    mode = str(m.get("approval_mode") or "auto").strip().lower()
    if mode == "required":
        return {
            "needs_approval": True,
            "rationale": "manifest_required",
            "risk_tier": tier,
        }
    if mode == "blocked":
        return {
            "needs_approval": True,
            "rationale": "manifest_blocked_operator",
            "risk_tier": max(tier, 4),
        }
    if tier >= 4:
        return {
            "needs_approval": True,
            "rationale": "tier4_operator_only",
            "risk_tier": tier,
        }
    if tier >= 3:
        return {
            "needs_approval": True,
            "rationale": "high_risk_tier",
            "risk_tier": tier,
        }
    rec, reason = approval_hint(lane=lane, command_type=command_type)
    if rec and tier >= 2 and lane not in {"openclaw_hybrid", "cursor"}:
        return {"needs_approval": True, "rationale": reason, "risk_tier": tier}
    if rec and "financial" in reason:
        return {"needs_approval": True, "rationale": reason, "risk_tier": tier}
    return {"needs_approval": False, "rationale": "within_auto_policy", "risk_tier": tier}


def approval_hint(*, lane: str, command_type: str = "") -> Tuple[bool, str]:
    """
    Returns (approval_recommended, reason).
    Conservative: delegation lanes suggest human visibility for Tier >= 2.
    """
    tier = risk_tier_for_lane(lane)
    if tier >= 3:
        return True, "high_risk_tier"
    if tier >= 2 and lane in {"openclaw_hybrid", "cursor"}:
        return False, "standard_delegation_traceable"
    if "financial" in command_type.lower() or "billing" in command_type.lower():
        return True, "financial_keyword"
    return False, "within_auto_policy"


def summarize_policy() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "tiers": {
            "0": "read_only",
            "1": "reversible_local",
            "2": "user_visible_external_low",
            "3": "sensitive_requires_approval",
            "4": "blocked_operator_only",
        },
    }

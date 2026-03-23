"""Typed tool / capability manifests (Phase 2 seed).

Expand from docs/ANDREA_CAPABILITY_MATRIX.md over time.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

TOOL_MANIFESTS: Dict[str, Dict[str, Any]] = {
    "direct_assistant": {
        "capability": "conversation",
        "risk_tier": 0,
        "side_effects": "none",
        "fallback_group": "assistant",
        "approval_mode": "auto",
        "verification_mode": "none",
        "reversible": True,
        "data_sensitivity": "low",
        "cost_class": "low",
        "scenario_support": "mixed",
        "proof_capabilities": ["none"],
        "action_classes": ["conversation"],
    },
    "openclaw_hybrid": {
        "capability": "delegated_openclaw",
        "risk_tier": 2,
        "side_effects": "repo_optional",
        "fallback_group": "delegation",
        "approval_mode": "auto",
        "verification_mode": "repo_checks",
        "reversible": True,
        "data_sensitivity": "medium",
        "cost_class": "medium",
        "scenario_support": "repo_and_hybrid",
        "proof_capabilities": ["repo_checks", "artifact_presence", "human_confirm"],
        "action_classes": ["repo_change", "mixed_lane"],
    },
    "cursor_agent": {
        "capability": "delegated_cursor",
        "risk_tier": 2,
        "side_effects": "repo",
        "fallback_group": "delegation",
        "approval_mode": "auto",
        "verification_mode": "repo_checks",
        "reversible": True,
        "data_sensitivity": "medium",
        "cost_class": "medium",
        "scenario_support": "repo_primary",
        "proof_capabilities": ["repo_checks", "artifact_presence", "human_confirm"],
        "action_classes": ["repo_change"],
    },
    "recent_text_messages": {
        "capability": "messaging_read",
        "risk_tier": 1,
        "side_effects": "read_external",
        "fallback_group": "messaging",
        "scenario_support": "inbox_lookup",
        "proof_capabilities": ["none"],
        "action_classes": ["structured_lookup"],
    },
}


_KEYWORD_TAGS: List[tuple[str, str]] = [
    (r"\b(skill|openclaw|claw)\b", "skill"),
    (r"\b(cursor|agent)\b", "cursor"),
    (r"\b(git|repo|pull request|pr)\b", "repo"),
    (r"\b(remind|remember)\b", "memory"),
]


def capability_tags_for_text(text: str) -> List[str]:
    t = (text or "").lower()
    out: List[str] = []
    for pattern, tag in _KEYWORD_TAGS:
        if re.search(pattern, t, re.I) and tag not in out:
            out.append(tag)
    return out


def manifest_for_lane(lane: str) -> Dict[str, Any]:
    lane_norm = str(lane or "").strip()
    if lane_norm in ("direct_cursor", "cursor_direct"):
        lane_norm = "cursor"
    if lane_norm == "openclaw_hybrid":
        return dict(TOOL_MANIFESTS["openclaw_hybrid"])
    if lane_norm == "cursor":
        return dict(TOOL_MANIFESTS["cursor_agent"])
    return dict(TOOL_MANIFESTS["direct_assistant"])

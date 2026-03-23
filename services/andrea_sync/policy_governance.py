"""Versioned policy / schema anchors for observability (Phase 6 blueprint)."""
from __future__ import annotations

from typing import Any, Dict

GOAL_SCHEMA_VERSION = 1
WORKFLOW_SCHEMA_VERSION = 1
TOOL_MANIFEST_SCHEMA_VERSION = 1
ROUTING_POLICY_VERSION = 1
MEMORY_POLICY_VERSION = 1


def governance_snapshot() -> Dict[str, Any]:
    return {
        "goal_schema_version": GOAL_SCHEMA_VERSION,
        "workflow_schema_version": WORKFLOW_SCHEMA_VERSION,
        "tool_manifest_schema_version": TOOL_MANIFEST_SCHEMA_VERSION,
        "routing_policy_version": ROUTING_POLICY_VERSION,
        "memory_policy_version": MEMORY_POLICY_VERSION,
    }

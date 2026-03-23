"""Unified delegated-lifecycle view for OpenClaw + Cursor (contract v1).

This module is projection-only: it does not change event semantics; it summarizes
``meta.openclaw``, ``meta.cursor``, and ``meta.execution`` into one operator- and
API-friendly shape for dashboards and follow-up tooling.
"""
from __future__ import annotations

from typing import Any, Dict, List

CONTRACT_VERSION = 1

# Documented lifecycle primitives (skills / cursor_openclaw align to these names).
CURSOR_LIFECYCLE_ACTIONS = ("status", "followup", "conversation", "artifacts")
DELEGATION_PHASES = ("plan", "critique", "execution", "synthesis")


def build_delegated_lifecycle_contract(meta: Dict[str, Any] | None) -> Dict[str, Any]:
    """Return a normalized delegated-lifecycle contract from task ``meta``."""
    m = meta if isinstance(meta, dict) else {}
    oc = m.get("openclaw") if isinstance(m.get("openclaw"), dict) else {}
    cur = m.get("cursor") if isinstance(m.get("cursor"), dict) else {}
    ex = m.get("execution") if isinstance(m.get("execution"), dict) else {}
    out = m.get("outcome") if isinstance(m.get("outcome"), dict) else {}
    goal_meta = m.get("goal") if isinstance(m.get("goal"), dict) else {}

    agent_id = str(
        cur.get("cursor_agent_id")
        or cur.get("agent_id")
        or cur.get("execution_agent_id")
        or ""
    ).strip()
    terminal = str(
        cur.get("terminal_status") or cur.get("status") or ""
    ).strip()
    next_actions: List[str] = []
    if agent_id and not terminal:
        next_actions.append("status")
    if agent_id and terminal and terminal.upper() == "FINISHED":
        next_actions.extend(["conversation", "artifacts"])

    return {
        "contract_version": CONTRACT_VERSION,
        "openclaw": {
            "session_id": str(oc.get("session_id") or "").strip(),
            "run_id": str(oc.get("run_id") or "").strip(),
            "provider": str(oc.get("provider") or "").strip(),
            "model": str(oc.get("model") or "").strip(),
        },
        "cursor": {
            "agent_id": agent_id,
            "agent_url": str(cur.get("agent_url") or "").strip(),
            "pr_url": str(cur.get("pr_url") or "").strip(),
            "terminal_status": terminal,
            "kind": str(cur.get("kind") or "").strip(),
            "cursor_strategy": str(cur.get("cursor_strategy") or "").strip(),
        },
        "execution": {
            "lane": str(ex.get("lane") or ex.get("execution_lane") or ex.get("runner") or "").strip(),
            "attempt_id": str(ex.get("attempt_id") or "").strip(),
            "delegated_to_cursor": bool(ex.get("delegated_to_cursor")),
            "backend": str(ex.get("backend") or "").strip(),
            "sync_source": str(ex.get("sync_source") or "").strip(),
            "continuation_state": str(ex.get("continuation_state") or "").strip(),
        },
        "orchestration": {
            "current_phase": str(out.get("current_phase") or "").strip(),
            "current_phase_lane": str(out.get("current_phase_lane") or "").strip(),
            "completed_phases": list(out.get("completed_phases") or [])
            if isinstance(out.get("completed_phases"), list)
            else [],
        },
        "recommended_next_actions": next_actions,
        "lifecycle_actions_catalog": list(CURSOR_LIFECYCLE_ACTIONS),
        "phase_catalog": list(DELEGATION_PHASES),
        "goal": {
            "goal_id": str(goal_meta.get("goal_id") or "").strip(),
            "summary": str(goal_meta.get("summary") or "").strip(),
            "status": str(goal_meta.get("status") or "").strip(),
        },
    }

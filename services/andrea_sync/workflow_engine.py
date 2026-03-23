"""Minimal workflow graph helper (Phase 5 blueprint)."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .store import create_workflow, get_workflow, update_workflow


def define_linear_workflow(
    conn: Any,
    principal_id: str,
    name: str,
    steps: List[str],
) -> str:
    definition: Dict[str, Any] = {
        "kind": "linear",
        "steps": [{"id": s, "status": "pending"} for s in steps if s],
        "completed_steps": [],
    }
    return create_workflow(conn, principal_id, name, definition=definition, status="ready")


def next_pending_step(workflow_row: Dict[str, Any]) -> Optional[str]:
    raw = workflow_row.get("definition_json") or "{}"
    try:
        definition = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(definition, dict):
        return None
    steps = definition.get("steps")
    if not isinstance(steps, list):
        return None
    done = definition.get("completed_steps")
    done_list = done if isinstance(done, list) else []
    for step in steps:
        if not isinstance(step, dict):
            continue
        sid = str(step.get("id") or "")
        if sid and sid not in done_list:
            return sid
    return None


def mark_step_done(conn: Any, workflow_id: str, step_id: str) -> bool:
    wf = get_workflow(conn, workflow_id)
    if not wf:
        return False
    raw = wf.get("definition_json") or "{}"
    try:
        definition = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except json.JSONDecodeError:
        definition = {}
    if not isinstance(definition, dict):
        definition = {}
    done = definition.get("completed_steps")
    done_list = list(done) if isinstance(done, list) else []
    if step_id and step_id not in done_list:
        done_list.append(step_id)
    definition["completed_steps"] = done_list
    return update_workflow(conn, workflow_id, definition=definition, status="running")

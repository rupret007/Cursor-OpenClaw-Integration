"""Goal + session runtime: durable goals linked to tasks and delegated lifecycle (Phase 1)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .delegated_lifecycle import build_delegated_lifecycle_contract
from .scenario_registry import get_contract
from .projector import project_task_dict
from .schema import EventType
from .store import (
    append_goal_event,
    append_event,
    create_goal,
    get_active_execution_plan_for_task,
    get_goal,
    get_goal_id_for_task,
    get_task_channel,
    get_task_principal_id,
    link_task_to_goal,
    list_goals_for_principal,
    list_tasks_for_goal,
)

_GOAL_STATUS_PATTERNS = re.compile(
    r"(?i)\b("
    r"status|what'?s\s+the\s+status|where\s+are\s+we|what\s+happened|"
    r"continue|follow\s*up|any\s+update|progress|"
    r"needs?\s+(my|our)\s+approval|awaiting\s+(my|our)\s+approval|"
    r"pending\s+(my|our)\s+approval|waiting\s+on\s+(my|our)\s+approval|"
    r"what\s+still\s+needs\s+(my|our)\s+approval"
    r")\b"
)


def try_goal_status_nl_reply(
    conn: Any,
    task_id: str,
    user_text: str,
) -> Optional[str]:
    """
    If the user is asking for status/continue-style help and there is an active goal
    for this principal, answer from goal + delegated lifecycle (not chat guesswork).
    """
    if not user_text or not _GOAL_STATUS_PATTERNS.search(user_text):
        return None
    return build_goal_continuity_reply(conn, task_id)


def build_goal_continuity_reply(
    conn: Any,
    task_id: str,
) -> Optional[str]:
    """
    Build a compact continuity/status answer from goal + plan + projection state.
    Unlike try_goal_status_nl_reply, this does not apply intent regex gating.
    """
    principal_id = get_task_principal_id(conn, task_id)
    if not principal_id:
        return None
    linked_goal_id = get_goal_id_for_task(conn, task_id)
    if linked_goal_id:
        goal_row = get_goal(conn, linked_goal_id) or {}
        goal_id = linked_goal_id
        exec_task = task_id
    else:
        active = list_goals_for_principal(conn, principal_id, status="active", limit=3)
        if not active:
            return None
        goal_row = active[0]
        goal_id = str(goal_row["goal_id"])
        linked = list_tasks_for_goal(conn, goal_id, limit=25)
        exec_task = task_id if task_id in linked else (linked[0] if linked else "")
    if not exec_task:
        summary = str(goal_row.get("summary") or "").strip()
        return (
            f"Active goal `{goal_id}`"
            + (f": {summary}" if summary else "")
            + " — no execution tasks linked yet."
        )
    channel = get_task_channel(conn, exec_task) or "cli"
    projection = project_task_dict(conn, exec_task, channel)
    meta = projection.get("meta") if isinstance(projection.get("meta"), dict) else {}
    contract = build_delegated_lifecycle_contract(meta)
    status = str(projection.get("status") or "")
    gsummary = str(goal_row.get("summary") or "").strip()
    lines: List[str] = [
        f"Goal `{goal_id}`" + (f" — {gsummary}" if gsummary else ""),
        f"Tracked task `{exec_task}` status: **{status}**.",
    ]
    plan_row = get_active_execution_plan_for_task(conn, exec_task)
    if plan_row:
        pid = str(plan_row.get("plan_id") or "")
        pst = str(plan_row.get("status") or "")
        vs = str(plan_row.get("verification_state") or "")
        ps = plan_row.get("summary") if isinstance(plan_row.get("summary"), dict) else {}
        sid = str(ps.get("scenario_id") or "").strip()
        if sid:
            cmeta = get_contract(sid)
            slab = (cmeta.user_facing_label if cmeta else "") or sid
            rs = str(ps.get("receipt_state") or "").strip()
            proof = str(ps.get("proof_class") or "").strip()
            scen_bits = [f"**{slab}** (`{sid}`)"]
            if proof:
                scen_bits.append(f"proof: {proof}")
            if rs:
                scen_bits.append(f"receipt: **{rs}**")
            lines.append("Scenario: " + " — ".join(scen_bits) + ".")
        if pid:
            lines.append(
                f"Active plan `{pid}`: **{pst}**"
                + (f" (verification: {vs})" if vs else "")
                + "."
            )
    oc = contract.get("openclaw") if isinstance(contract.get("openclaw"), dict) else {}
    cur = contract.get("cursor") if isinstance(contract.get("cursor"), dict) else {}
    if oc.get("run_id"):
        lines.append(f"OpenClaw run: `{oc.get('run_id')}`")
    if cur.get("agent_id"):
        lines.append(f"Cursor agent: `{cur.get('agent_id')}` (terminal: {cur.get('terminal_status') or 'n/a'})")
    rec = contract.get("recommended_next_actions") or []
    if isinstance(rec, list) and rec:
        lines.append("Suggested next: " + ", ".join(str(x) for x in rec[:4]))
    return "\n".join(lines)


def ensure_delegate_goal_link(
    conn: Any,
    task_id: str,
    *,
    user_summary: str,
    channel: str,
    auto_create: bool,
) -> Optional[str]:
    """
    Return goal_id for this task after optional auto-create + link.
    When auto_create is False, only returns existing mapping.
    """
    existing = get_goal_id_for_task(conn, task_id)
    if existing:
        return existing
    principal_id = get_task_principal_id(conn, task_id)
    if not principal_id:
        return None
    if not auto_create:
        return None
    active = list_goals_for_principal(conn, principal_id, status="active", limit=1)
    if active:
        gid = str(active[0]["goal_id"])
    else:
        snippet = (user_summary or "").strip()[:240] or "Delegated work"
        gid = create_goal(
            conn,
            principal_id,
            snippet,
            channel=channel,
            metadata={"source": "auto_delegate"},
        )
        append_goal_event(conn, gid, "auto_created", {"task_id": task_id})
    link_task_to_goal(conn, task_id, gid)
    g = get_goal(conn, gid) or {}
    append_event(
        conn,
        task_id,
        EventType.TASK_GOAL_LINKED,
        {
            "goal_id": gid,
            "goal_summary": str(g.get("summary") or ""),
            "goal_status": str(g.get("status") or "active"),
        },
    )
    append_goal_event(conn, gid, "task_linked", {"task_id": task_id})
    return gid

"""Goal-centered execution continuity: same-run handles, sync, and follow-up."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

from .cursor_plan_execute import (
    fetch_agent_status_payload,
    submit_agent_followup_payload,
    summarize_agent_terminal_state_from_response,
)
from .observability import metric_log, structured_log
from .schema import EventType
from .store import append_event, get_active_execution_attempt_for_task, update_execution_attempt_handles

REPO_ROOT = Path(__file__).resolve().parents[2]


def cursor_repo_path() -> Path:
    raw = (os.environ.get("ANDREA_CURSOR_REPO") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return REPO_ROOT


def _parse_handles(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        h = json.loads(row.get("handle_json") or "{}")
    except json.JSONDecodeError:
        h = {}
    return h if isinstance(h, dict) else {}


def continue_cursor_followup_for_task(
    conn: Any, task_id: str, followup_text: str
) -> Dict[str, Any]:
    """Submit a same-run Cursor follow-up for the active execution attempt."""
    row = get_active_execution_attempt_for_task(conn, task_id)
    if not row:
        return {"ok": False, "error": "no_active_execution_attempt"}
    handles = _parse_handles(row)
    agent_id = str(handles.get("cursor_agent_id") or "").strip()
    if not agent_id:
        return {"ok": False, "error": "no_cursor_agent_id_on_attempt"}
    attempt_id = str(row["exec_attempt_id"])
    result = submit_agent_followup_payload(
        repo_path=cursor_repo_path(),
        agent_id=agent_id,
        prompt=followup_text,
    )
    ok = bool(result.get("ok"))
    append_event(
        conn,
        task_id,
        EventType.JOB_PROGRESS,
        {
            "message": "cursor_followup_submitted" if ok else "cursor_followup_failed",
            "backend": "cursor",
            "runner": "cursor",
            "attempt_id": attempt_id,
            "sync_source": "cursor_followup_api",
            "cursor_agent_id": agent_id,
            "followup_http_ok": ok,
            "followup_returncode": result.get("returncode"),
        },
    )
    structured_log(
        "execution_followup_submitted",
        task_id=task_id,
        attempt_id=attempt_id,
        agent_id=agent_id,
        ok=ok,
    )
    metric_log("execution_followup_submitted", task_id=task_id, ok=ok)
    return {
        "ok": ok,
        "followup_ok": ok,
        "attempt_id": attempt_id,
        "subprocess": result,
    }


def sync_execution_status_for_task(conn: Any, task_id: str) -> Dict[str, Any]:
    """Refresh Cursor agent status into the attempt row and append a sync event."""
    row = get_active_execution_attempt_for_task(conn, task_id)
    if not row:
        return {"ok": False, "error": "no_active_execution_attempt"}
    handles = _parse_handles(row)
    agent_id = str(handles.get("cursor_agent_id") or "").strip()
    if not agent_id:
        return {"ok": False, "error": "no_cursor_agent_id_on_attempt"}
    attempt_id = str(row["exec_attempt_id"])
    st = fetch_agent_status_payload(repo_path=cursor_repo_path(), agent_id=agent_id)
    resp = st.get("response") if isinstance(st.get("response"), dict) else {}
    norm = summarize_agent_terminal_state_from_response(resp)
    patch = {
        "cursor_agent_id": agent_id,
        "agent_url": norm.get("agent_url") or handles.get("agent_url") or "",
        "pr_url": norm.get("pr_url") or handles.get("pr_url") or "",
        "last_polled_status": norm.get("status") or "",
    }
    update_execution_attempt_handles(conn, attempt_id, patch, touch_last_synced=True)
    append_event(
        conn,
        task_id,
        EventType.JOB_PROGRESS,
        {
            "message": "execution_status_synced",
            "backend": "cursor",
            "runner": "cursor",
            "attempt_id": attempt_id,
            "sync_source": "cursor_agent_status",
            "terminal_status": norm.get("terminal_status") or norm.get("status"),
            "cursor_agent_id": agent_id,
            "agent_url": norm.get("agent_url") or None,
            "pr_url": norm.get("pr_url") or None,
            "status_poll_ok": bool(st.get("ok")),
        },
    )
    structured_log(
        "execution_status_sync",
        task_id=task_id,
        attempt_id=attempt_id,
        status=norm.get("status"),
    )
    metric_log("execution_status_sync", task_id=task_id, ok=bool(st.get("ok")))
    return {"ok": True, "attempt_id": attempt_id, "status": norm, "subprocess": st}


def summarize_execution_attempt_for_user(conn: Any, task_id: str) -> Dict[str, Any]:
    """Lightweight operator/user summary from the active attempt row (no provider call)."""
    row = get_active_execution_attempt_for_task(conn, task_id)
    if not row:
        return {"ok": False, "error": "no_active_execution_attempt"}
    handles = _parse_handles(row)
    now = time.time()
    last_sync = float(row.get("last_synced_at") or 0.0)
    return {
        "ok": True,
        "attempt_id": str(row["exec_attempt_id"]),
        "lane": str(row.get("lane") or ""),
        "backend": str(row.get("backend") or ""),
        "status": str(row.get("status") or ""),
        "cursor_agent_id": str(handles.get("cursor_agent_id") or ""),
        "last_sync_age_seconds": (now - last_sync) if last_sync > 0 else None,
    }

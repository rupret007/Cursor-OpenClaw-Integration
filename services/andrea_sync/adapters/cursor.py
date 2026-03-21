"""Helpers to report Cursor / cursor_openclaw lifecycle into the event store."""
from __future__ import annotations

from typing import Any, Dict


def cursor_event_command(
    task_id: str,
    event_type: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a ReportCursorEvent command body for bus.handle_command."""
    return {
        "command_type": "ReportCursorEvent",
        "channel": "cursor",
        "task_id": task_id,
        "payload": {
            "event_type": event_type,
            "payload": payload,
        },
    }


# Convenience mappings for typical lifecycle names
def job_started(agent_id: str) -> Dict[str, Any]:
    return {"cursor_agent_id": agent_id, "phase": "started"}


def job_completed(summary: str) -> Dict[str, Any]:
    return {"summary": summary, "phase": "completed"}


def job_failed(error: str) -> Dict[str, Any]:
    return {"error": error, "phase": "failed"}

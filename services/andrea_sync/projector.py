"""Project task state from append-only events."""
from __future__ import annotations

import sqlite3
from typing import Any, Dict

from .schema import EventType, TaskProjection, TaskStatus, fold_projection, validate_event_type
from .store import load_events_for_task


def project_task(conn: sqlite3.Connection, task_id: str, channel: str) -> TaskProjection:
    events = load_events_for_task(conn, task_id)
    proj = TaskProjection(
        task_id=task_id,
        status=TaskStatus.CREATED,
        channel=channel,
        seq_applied=0,
    )
    for seq, _ts, et_raw, payload in events:
        try:
            et = validate_event_type(et_raw)
        except ValueError:
            proj.meta.setdefault("warnings", []).append(f"unknown_event:{et_raw}")
            continue
        fold_projection(proj, et, payload)
        proj.seq_applied = seq
    return proj


def project_task_dict(conn: sqlite3.Connection, task_id: str, channel: str) -> Dict[str, Any]:
    return project_task(conn, task_id, channel).as_dict()

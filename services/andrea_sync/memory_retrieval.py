"""Retrieval helpers for episodic / preference memory (Phase 3)."""
from __future__ import annotations

import json
from typing import Any, List

from .schema import EventType
from .store import load_events_for_task, list_tasks


def preference_lines_for_principal(conn: Any, principal_id: str) -> List[str]:
    from .store import get_principal_preferences

    prefs = get_principal_preferences(conn, principal_id)
    lines: List[str] = []
    for key, raw in (prefs or {}).items():
        if not key:
            continue
        val = raw if isinstance(raw, str) else json.dumps(raw, default=str)
        lines.append(f"[preference:{key}] {val[:500]}")
    return lines[:12]


def episodic_snippets_for_principal(
    conn: Any,
    principal_id: str,
    *,
    limit: int = 3,
) -> List[str]:
    """Best-effort recent completed-task summaries for this principal."""
    from .store import get_task_principal_id

    snippets: List[str] = []
    for row in list_tasks(conn, limit=80):
        tid = str(row.get("task_id") or "")
        if not tid:
            continue
        if get_task_principal_id(conn, tid) != principal_id:
            continue
        events = load_events_for_task(conn, tid)
        if not events:
            continue
        for _seq, _ts, et, payload in reversed(events):
            if et != EventType.JOB_COMPLETED.value:
                continue
            if not isinstance(payload, dict):
                continue
            summary = str(payload.get("summary") or payload.get("message") or "").strip()
            if summary:
                snippets.append(f"[prior_task:{tid[:10]}…] {summary[:240]}")
            break
        if len(snippets) >= limit:
            break
    return snippets[:limit]

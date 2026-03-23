"""Artifact persistence facade (Phase 4); uses goal_artifacts table."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .store import list_goal_artifacts, record_goal_artifact


def store_artifact(
    conn: Any,
    goal_id: str,
    *,
    task_id: str = "",
    kind: str = "file",
    label: str = "",
    uri: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    return record_goal_artifact(
        conn,
        goal_id,
        task_id=task_id,
        kind=kind,
        label=label,
        uri=uri,
        metadata=metadata,
    )


def artifacts_for_goal(conn: Any, goal_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
    return list_goal_artifacts(conn, goal_id, limit=limit)

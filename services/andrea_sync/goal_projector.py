"""Read-side helpers for goal timeline (Phase 1)."""
from __future__ import annotations

import json
from typing import Any, Dict, List


def load_goal_events(conn: Any, goal_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT seq, ts, event_type, payload_json
        FROM goal_events
        WHERE goal_id = ?
        ORDER BY seq DESC
        LIMIT ?
        """,
        (goal_id, limit),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        out.append(
            {
                "seq": r["seq"],
                "ts": r["ts"],
                "event_type": r["event_type"],
                "payload": payload,
            }
        )
    return list(reversed(out))


def summarize_goal_row(goal: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "goal_id": goal.get("goal_id"),
        "status": goal.get("status"),
        "summary": goal.get("summary"),
        "principal_id": goal.get("principal_id"),
        "channel": goal.get("channel"),
    }

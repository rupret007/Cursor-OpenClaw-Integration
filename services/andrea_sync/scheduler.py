"""Due workflow polling helper (Phase 5 blueprint; wall-clock)."""
from __future__ import annotations

import time
from typing import Any, Dict, List

from .store import list_workflows_for_principal


def due_workflows(
    conn: Any,
    *,
    principal_id: str | None = None,
    now: float | None = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Return workflows with next_run_at > 0 and <= now."""
    ts = time.time() if now is None else float(now)
    if principal_id:
        rows = list_workflows_for_principal(conn, principal_id, limit=limit)
    else:
        rows = conn.execute(
            """
            SELECT * FROM workflows
            WHERE status NOT IN ('completed', 'cancelled')
              AND next_run_at > 0
              AND next_run_at <= ?
            ORDER BY next_run_at ASC
            LIMIT ?
            """,
            (ts, limit),
        ).fetchall()
        rows = [dict(r) for r in rows]
    out: List[Dict[str, Any]] = []
    for row in rows:
        nrun = float(row.get("next_run_at") or 0)
        if nrun and nrun <= ts:
            out.append(dict(row))
    return out[:limit]

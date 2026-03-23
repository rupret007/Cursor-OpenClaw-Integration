"""Personal memory assembly (Phase 3 blueprint)."""
from __future__ import annotations

import json
from typing import Any, List

from .memory_retrieval import episodic_snippets_for_principal, preference_lines_for_principal
from .persona_policy import memory_injection_cap
from .store import list_principal_memories


def build_memory_notes_for_principal(
    conn: Any,
    principal_id: str,
    *,
    task_id: str = "",
    limit_memories: int = 6,
) -> List[str]:
    """
    Working + preference + episodic + semantic (principal_memories) in stable order.
    Respects a soft cap on injected lines for direct lane hygiene.
    """
    notes: List[str] = []
    for line in preference_lines_for_principal(conn, principal_id):
        if line:
            notes.append(line)
    for snip in episodic_snippets_for_principal(conn, principal_id, limit=3):
        if snip:
            notes.append(snip)
    rows = list_principal_memories(conn, principal_id, limit=limit_memories)
    for row in rows:
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        kind = str(row.get("kind") or "note").strip()
        src = str(row.get("source") or "").strip()
        prefix = f"[{kind}]" + (f" ({src})" if src else "")
        notes.append(f"{prefix} {content}")
    cap = memory_injection_cap()
    return [n for n in notes if n][:cap]


def memory_provenance_record(
    *,
    kind: str,
    content: str,
    source: str,
    task_id: str,
) -> dict:
    return {
        "kind": kind,
        "content": content,
        "source": source,
        "source_task_id": task_id,
        "schema_version": 1,
    }


def safe_json_metadata(meta: dict) -> str:
    try:
        return json.dumps(meta, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        return "{}"

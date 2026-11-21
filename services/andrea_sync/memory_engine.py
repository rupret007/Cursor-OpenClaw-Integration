"""Personal memory assembly (Phase 3 blueprint)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, List

from .memory_retrieval import episodic_snippets_for_principal, preference_lines_for_principal
from .persona_policy import memory_injection_cap
from .store import list_principal_memories

MEMORY_FENCE_LIMIT = 500
_FENCE = None


def _fence_module():
    global _FENCE
    if _FENCE is None:
        path = Path(__file__).resolve().parents[2] / "first-officer" / "guard" / "sanitize.py"
        spec = importlib.util.spec_from_file_location("fo_guard_sanitize", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("The memory fence is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _FENCE = module
    return _FENCE


def _fenced_note(text: str) -> str:
    """Return a fenced note, or empty when the note cannot be fenced.

    An empty result means the note is omitted. The raw text is not returned.
    """
    body = str(text or "").strip()[:MEMORY_FENCE_LIMIT]
    if not body:
        return ""
    try:
        return _fence_module().fence("memory", body, MEMORY_FENCE_LIMIT)
    except ValueError:
        return ""


def build_memory_notes_for_principal(
    conn: Any,
    principal_id: str,
    *,
    task_id: str = "",
    limit_memories: int = 6,
) -> List[str]:
    """
    Working + preference + episodic + semantic (principal_memories) in stable order.
    Each returned line is fenced. A line that hides characters or closes the
    fence is omitted. The cap is applied before fencing, so a refused line
    does not make room for a later one.
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
    fenced: List[str] = []
    for note in [n for n in notes if n][:cap]:
        wrapped = _fenced_note(note)
        if wrapped:
            fenced.append(wrapped)
    return fenced


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

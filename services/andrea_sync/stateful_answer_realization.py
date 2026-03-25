"""Bounded LLM-backed realization for stateful assistant answers."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, List, Sequence

from .assistant_answer_composer import (
    _cursor_recall_output_should_force_clean_fallback,
    gather_cursor_recall_evidence_pack,
    is_continuation_fallback_family_text,
    is_strict_cursor_domain_recall_question,
)
from .turn_intelligence import TurnPlan
from .user_surface import sanitize_user_surface_text

_STOP_WORDS = frozenset(
    {
        "the",
        "this",
        "that",
        "with",
        "from",
        "your",
        "there",
        "about",
        "have",
        "been",
        "into",
        "only",
        "after",
        "when",
        "what",
        "where",
        "which",
        "while",
        "would",
        "could",
        "should",
        "still",
        "just",
        "than",
        "then",
    }
)


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def stateful_realization_enabled() -> bool:
    if not _env_truthy("ANDREA_STATEFUL_REALIZATION_ENABLED", True):
        return False
    if not _env_truthy("OPENAI_API_ENABLED", False):
        return False
    return bool((os.environ.get("OPENAI_API_KEY") or "").strip())


def _allowed_sources() -> set[str]:
    raw = (os.environ.get("ANDREA_STATEFUL_REALIZATION_SOURCES") or "").strip()
    if not raw:
        return {
            "cursor_continuity_recall",
            "cursor_heavy_lift_context",
            "blocked_state_reply",
            "goal_status",
            "goal_continuity",
        }
    return {s.strip() for s in raw.split(",") if s.strip()}


def _tokenize(value: str) -> set[str]:
    out: set[str] = set()
    for tok in re.split(r"[^a-zA-Z0-9_]+", str(value or "").lower()):
        if len(tok) < 5 or tok in _STOP_WORDS:
            continue
        out.add(tok)
    return out


def _evidence_anchor_overlap(reply: str, evidence_lines: Sequence[str]) -> bool:
    r = _tokenize(reply)
    if not r:
        return False
    anchor = set()
    for ln in evidence_lines:
        anchor |= _tokenize(ln)
    if not anchor:
        return False
    return bool(r & anchor)


def _openai_json_chat(*, system: str, user: str, model: str, timeout_seconds: int) -> dict[str, Any]:
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("stateful_realization_missing_key")
    payload: dict[str, Any] = {
        "model": model,
        "temperature": 0.35,
        "max_tokens": 260,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"stateful_realization_http_{err.code}:{raw[:240]}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"stateful_realization_transport:{err}") from err
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("stateful_realization_no_choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    content = str(message.get("content") or "").strip()
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise RuntimeError("stateful_realization_json_not_object")
    return parsed


@dataclass(frozen=True)
class StatefulRealizationInput:
    source: str
    deterministic_reply: str
    fallback_reply: str
    user_text: str
    turn_domain: str
    continuity_focus: str
    evidence_lines: tuple[str, ...]


def _split_structured_text_lines(text: str) -> List[str]:
    out: List[str] = []
    for raw in re.split(r"[\r\n]+", str(text or "")):
        clean = str(raw or "").strip().lstrip("-* ").strip()
        if clean:
            out.append(clean)
    return out


def _bundle_evidence_for_source(
    conn: Any, task_id: str, *, source: str, user_text: str, deterministic_reply: str
) -> List[str]:
    lines: List[str] = []
    if source == "cursor_continuity_recall":
        pack = gather_cursor_recall_evidence_pack(conn, task_id, user_message=user_text)
        lines.extend(list(pack.source_truth_narrative_lines)[:4])
        lines.extend(list(pack.source_truth_receipt_lines)[:2])
        if pack.outcome_phase_summary:
            lines.append(f"Phase summary: {pack.outcome_phase_summary}")
        if pack.outcome_blocked_reason:
            lines.append(f"Blocked reason: {pack.outcome_blocked_reason}")
        lines.extend(list(pack.support_execution_lines)[:1])
    if source in {
        "cursor_continuity_recall",
        "cursor_heavy_lift_context",
        "blocked_state_reply",
        "goal_status",
        "goal_continuity",
    }:
        lines.extend(_split_structured_text_lines(deterministic_reply))
    if not lines:
        lines.extend(_split_structured_text_lines(deterministic_reply))
    if not lines:
        lines.append(deterministic_reply)
    out: List[str] = []
    seen: set[str] = set()
    for ln in lines:
        safe = sanitize_user_surface_text(ln, fallback="", limit=340)
        key = safe.lower()
        if safe and key not in seen:
            seen.add(key)
            out.append(safe)
    return out[:8]


def maybe_realize_stateful_reply(
    conn: Any,
    task_id: str,
    *,
    source: str,
    deterministic_reply: str,
    fallback_reply: str,
    user_text: str,
    turn_plan: TurnPlan,
) -> str | None:
    """Return a naturalized stateful reply or None to keep deterministic output."""
    if not stateful_realization_enabled():
        return None
    if source not in _allowed_sources():
        return None
    evidence = _bundle_evidence_for_source(
        conn, task_id, source=source, user_text=user_text, deterministic_reply=deterministic_reply
    )
    inp = StatefulRealizationInput(
        source=str(source or ""),
        deterministic_reply=str(deterministic_reply or "").strip(),
        fallback_reply=str(fallback_reply or "").strip(),
        user_text=str(user_text or "").strip(),
        turn_domain=str(turn_plan.domain or ""),
        continuity_focus=str(turn_plan.continuity_focus or ""),
        evidence_lines=tuple(evidence),
    )
    if not inp.deterministic_reply or not inp.evidence_lines:
        return None
    model = (os.environ.get("ANDREA_DIRECT_OPENAI_MODEL") or "gpt-4o-mini").strip()
    timeout_seconds = max(
        5,
        int((os.environ.get("ANDREA_STATEFUL_REALIZATION_TIMEOUT_SECONDS") or "18").strip()),
    )
    system = (
        "You are Andrea. Rewrite state-backed assistant replies to sound natural and concise.\n"
        "Rules:\n"
        "1) Use ONLY facts from EVIDENCE_LINES.\n"
        "2) Do NOT invent entities, files, IDs, approvals, blockers, or outcomes.\n"
        "3) Preserve domain intent: recall asks recap; continuation asks continuation.\n"
        "4) If evidence is weak, return the provided fallback.\n"
        "5) Never output runtime internals or configuration names.\n"
        "Return JSON object with keys: reply (string), grounded (boolean), used_fallback (boolean)."
    )
    user_payload = json.dumps(
        {
            "user_text": inp.user_text,
            "turn_domain": inp.turn_domain,
            "continuity_focus": inp.continuity_focus,
            "candidate_source": inp.source,
            "deterministic_reply": inp.deterministic_reply,
            "fallback_reply": inp.fallback_reply,
            "evidence_lines": list(inp.evidence_lines),
        },
        ensure_ascii=False,
    )
    try:
        parsed = _openai_json_chat(
            system=system,
            user=user_payload,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        return None
    raw = str(parsed.get("reply") or "").strip()
    safe = sanitize_user_surface_text(raw, fallback="", limit=1200)
    if not safe:
        return None
    if source == "cursor_continuity_recall":
        if is_strict_cursor_domain_recall_question(inp.user_text):
            if _cursor_recall_output_should_force_clean_fallback(safe):
                return inp.fallback_reply or None
        if is_continuation_fallback_family_text(safe):
            return inp.fallback_reply or None
    if not bool(parsed.get("grounded")):
        return None
    if not _evidence_anchor_overlap(safe, inp.evidence_lines):
        return None
    return safe


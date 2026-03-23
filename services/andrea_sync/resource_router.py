"""Policy-style execution lane scoring (Phase 2 blueprint).

Ranks candidate lanes using lightweight heuristics; intended for observability and
future router-first routing. Does not replace ``andrea_router`` yet.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Tuple

from .tool_registry import capability_tags_for_text

LANES: Tuple[str, ...] = (
    "direct",
    "openclaw_hybrid",
    "cursor",
    "background",
)


def _score_lane(
    lane: str,
    *,
    text: str,
    routing_hint: str,
    tags: Sequence[str],
) -> float:
    t = text.lower()
    hint = (routing_hint or "auto").strip().lower()
    score = 0.0
    if lane == "direct":
        score += 3.0 if len(t.split()) < 12 else 1.0
        if hint == "andrea":
            score += 4.0
        if "remember" in t or "remind me" in t:
            score += 2.0
    if lane == "openclaw_hybrid":
        score += 2.0 if len(t.split()) >= 8 else 0.5
        if any(k in t for k in ("repo", "branch", "pr", "commit", "skill", "openclaw")):
            score += 3.0
        if hint in {"openclaw", "auto"}:
            score += 1.5
        if "skill" in tags:
            score += 2.0
    if lane == "cursor":
        score += 2.5 if re.search(r"\b(refactor|migration|typescript|python)\b", t) else 0.0
        if hint == "cursor":
            score += 5.0
        if "@cursor" in t.lower():
            score += 4.0
    if lane == "background":
        score += 1.0 if "later" in t or "background" in t else 0.0
    return score


def rank_execution_lanes(
    text: str,
    *,
    chosen_lane: str = "",
    routing_hint: str = "auto",
) -> List[Tuple[str, float]]:
    """Return (lane, score) pairs sorted by descending score."""
    tags = capability_tags_for_text(text)
    ranked: List[Tuple[str, float]] = []
    for lane in LANES:
        ranked.append(
            (
                lane,
                _score_lane(lane, text=text or "", routing_hint=routing_hint, tags=tags),
            )
        )
    ranked.sort(key=lambda x: x[1], reverse=True)
    if chosen_lane and chosen_lane in LANES:
        boosted: List[Tuple[str, float]] = []
        for lane, sc in ranked:
            boost = 0.5 if lane == chosen_lane else 0.0
            boosted.append((lane, sc + boost))
        boosted.sort(key=lambda x: x[1], reverse=True)
        return boosted
    return ranked


def routing_explanation(
    text: str,
    *,
    chosen_lane: str,
    routing_hint: str = "auto",
) -> Dict[str, Any]:
    """Structured, operator-safe explanation payload."""
    ranks = rank_execution_lanes(text, chosen_lane=chosen_lane, routing_hint=routing_hint)
    return {
        "lanes": [{"lane": lane, "score": round(score, 3)} for lane, score in ranks],
        "chosen": chosen_lane,
        "hint": routing_hint,
    }

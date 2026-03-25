"""Semantic answer selection for bounded conversational/stateful turns."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Dict, Optional

from .assistant_answer_composer import (
    build_blocked_state_reply_from_state,
    build_recent_outcome_history_reply_from_state,
    cursor_followup_context_reply_with_fallback,
    is_strict_cursor_domain_recall_question,
)
from .semantic_continuity import user_message_suggests_anaphoric_cursor_continue
from .goal_runtime import build_goal_continuity_reply, try_goal_status_nl_reply
from .turn_intelligence import TurnPlan
from .user_surface import sanitize_user_surface_text

_TOOLING_IDENTITY_Q_RE = re.compile(
    r"^\s*(?:"
    r"is\s+this\s+openclaw|is\s+this\s+cursor|"
    r"what\s+is\s+openclaw|what\s+is\s+cursor|"
    r"are\s+you\s+openclaw|are\s+you\s+cursor"
    r")\s*\??\s*$",
    re.I,
)


@dataclass(frozen=True)
class TurnInterpretation:
    scenario_id: str
    domain: str
    continuity_focus: str
    prefer_state_reply: bool
    force_delegate: bool
    confidence: float = 0.85


@dataclass(frozen=True)
class SemanticAnswerResult:
    reply_text: str
    reason: str
    source: str
    interpretation: TurnInterpretation
    score: int

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "score": self.score,
            "reason": self.reason,
            "scenario_id": self.interpretation.scenario_id,
            "domain": self.interpretation.domain,
            "continuity_focus": self.interpretation.continuity_focus,
            "prefer_state_reply": self.interpretation.prefer_state_reply,
            "force_delegate": self.interpretation.force_delegate,
            "confidence": self.interpretation.confidence,
        }


def _looks_thin_cursor_recap(text: str) -> bool:
    low = str(text or "").strip().lower()
    if not low:
        return True
    return "not finding a strong stored summary" in low


def _looks_metadata_led(text: str) -> bool:
    low = str(text or "").strip().lower()
    if not low:
        return True
    if low.startswith("where things stand:"):
        return True
    lead_tokens = ("task status", "phase:", "result:", "delegated execution")
    return any(low.startswith(tok) for tok in lead_tokens)


def _narrative_richness(text: str) -> int:
    t = str(text or "").strip()
    if not t:
        return 0
    richness = 0
    if "\n" in t:
        richness += 4
    if "cursor recap:" in t.lower():
        richness += 6
    if "next step:" in t.lower():
        richness += 2
    richness += min(8, len(t) // 80)
    return richness


def _score_candidate(source: str, text: str) -> int:
    base = {
        "cursor_continuity_recall": 96,
        "cursor_heavy_lift_context": 92,
        "blocked_state_reply": 90,
        "goal_status": 82,
        "goal_continuity": 76,
    }.get(source, 40)
    score = base + _narrative_richness(text)
    if source == "cursor_continuity_recall" and _looks_thin_cursor_recap(text):
        score -= 38
    if _looks_metadata_led(text):
        score -= 12
    return score


def choose_semantic_state_reply(
    conn: Any,
    task_id: str,
    *,
    user_text: str,
    turn_plan: TurnPlan,
    scenario_id: str,
) -> Optional[SemanticAnswerResult]:
    """
    Choose a direct state-backed answer for bounded conversational status turns.
    Returns None when semantic state selection should not override legacy routing.
    """
    interpretation = TurnInterpretation(
        scenario_id=str(scenario_id or ""),
        domain=str(turn_plan.domain or ""),
        continuity_focus=str(turn_plan.continuity_focus or ""),
        prefer_state_reply=bool(turn_plan.prefer_state_reply),
        force_delegate=bool(turn_plan.force_delegate),
    )
    if interpretation.force_delegate:
        return None
    if interpretation.domain not in {"project_status", "approval_state"}:
        return None
    if not bool(turn_plan.allow_goal_continuity_repair):
        return None

    text = str(user_text or "")
    if _TOOLING_IDENTITY_Q_RE.match(text.strip()):
        return None
    candidates: Dict[str, str] = {}

    if interpretation.continuity_focus == "blocked_state":
        candidates["blocked_state_reply"] = build_blocked_state_reply_from_state(conn, task_id)
    elif interpretation.continuity_focus == "recent_outcome_history":
        candidates["cursor_continuity_recall"] = build_recent_outcome_history_reply_from_state(
            conn, task_id, user_message=text
        )
    elif interpretation.continuity_focus == "cursor_followup_heavy_lift":
        candidates["cursor_heavy_lift_context"] = cursor_followup_context_reply_with_fallback(
            conn, task_id, user_message=text
        )

    goal_status = try_goal_status_nl_reply(conn, task_id, text)
    if goal_status:
        candidates["goal_status"] = goal_status
    goal_continuity = build_goal_continuity_reply(conn, task_id, user_text=text)
    if goal_continuity:
        candidates["goal_continuity"] = goal_continuity

    if interpretation.continuity_focus == "recent_outcome_history" and is_strict_cursor_domain_recall_question(
        text
    ):
        candidates = {
            k: v for k, v in candidates.items() if k == "cursor_continuity_recall"
        }
    elif interpretation.continuity_focus == "cursor_followup_heavy_lift" and user_message_suggests_anaphoric_cursor_continue(
        text
    ):
        candidates = {k: v for k, v in candidates.items() if k == "cursor_heavy_lift_context"}

    best: Optional[SemanticAnswerResult] = None
    for source, raw_text in candidates.items():
        cleaned = sanitize_user_surface_text(str(raw_text or "").strip(), limit=1200)
        if not cleaned:
            continue
        score = _score_candidate(source, cleaned)
        if best is None or score > best.score:
            best = SemanticAnswerResult(
                reply_text=cleaned,
                reason=f"semantic_state_{source}",
                source=source,
                interpretation=interpretation,
                score=score,
            )
    if best is None:
        return None
    if best.score < 70:
        return None
    return best

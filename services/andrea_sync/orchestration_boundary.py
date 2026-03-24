"""Direct–state–delegate boundary: decide when to answer vs escalate to heavy execution.

This module is intentionally lightweight and regex-driven; it feeds routing hints
(`classify_route`) rather than replacing scenario contracts or planner policy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Status / history / continuity questions that should stay on the direct path even
# when repo-ish words appear, unless the user clearly asks for implementation work.
ANSWER_SHAPED_STATUS_OR_HISTORY_RE = re.compile(
    r"(?i)\b("
    r"what'?s\s+blocked|blocked\s+right\s+now|"
    r"what\s+happened\s+(?:with\s+)?(?:that\s+)?(?:task|run|job|delegation)|"
    r"what\s+happened\s+earlier|"
    r"where\s+are\s+we|what'?s\s+the\s+status|"
    r"what\s+are\s+we\s+working\s+on(?:\s+right\s+now|\s+with\s+andrea)?|"
    r"working\s+on\s+right\s+now|working\s+on\s+with\s+andrea|"
    r"needs?\s+(?:my|our)\s+approval|"
    r"what\s+still\s+needs\s+(?:my|our)\s+approval|"
    r"what\s+still\s+needs"
    r")\b",
)

# Clear intent to do repo work — do not demote to direct for these.
_HEAVY_IMPLEMENTATION_INTENT_RE = re.compile(
    r"(?i)\b("
    r"implement|refactor|write\s+(?:the\s+)?code|"
    r"debug\s+(?:the\s+)?|patch\s+|"
    r"fix\s+(?:the\s+)?(?:code|bug|tests?|failing)|"
    r"inspect\s+(?:the\s+)?repo|"
    r"open\s+(?:a\s+)?pr|pull\s+request|"
    r"add\s+(?:a\s+)?(?:function|test|fixture)|"
    r"change\s+(?:the\s+)?(?:code|file|files)"
    r")\b",
)


def _normalize(text: str) -> str:
    raw = str(text or "").strip()
    raw = raw.replace("’", "'").replace("`", "'")
    return re.sub(r"\s+", " ", raw).lower()


@dataclass(frozen=True)
class DecisionProfile:
    """Lightweight routing hints for one user turn."""

    answer_first: bool
    state_first: bool
    heavy_lift_hint: bool
    reason: str


def build_decision_profile(
    text: str,
    *,
    turn_domain: str = "",
    scenario_force_delegate: bool = False,
) -> DecisionProfile:
    """
    Score whether this turn should prefer answering from state vs delegating.

    ``scenario_force_delegate`` is True when the scenario is already execution-only
    (e.g. `repoHelpVerified`) — never downgrade to answer-first.
    """
    if scenario_force_delegate:
        return DecisionProfile(
            answer_first=False,
            state_first=False,
            heavy_lift_hint=True,
            reason="scenario_force_delegate",
        )
    clean = (text or "").strip()
    if not clean:
        return DecisionProfile(
            answer_first=True,
            state_first=False,
            heavy_lift_hint=False,
            reason="empty",
        )
    heavy = bool(_HEAVY_IMPLEMENTATION_INTENT_RE.search(clean))
    status_shaped = bool(ANSWER_SHAPED_STATUS_OR_HISTORY_RE.search(clean))

    if heavy:
        return DecisionProfile(
            answer_first=False,
            state_first=False,
            heavy_lift_hint=True,
            reason="heavy_implementation_intent",
        )
    if status_shaped:
        return DecisionProfile(
            answer_first=True,
            state_first=True,
            heavy_lift_hint=False,
            reason="answer_shaped_status_or_history",
        )
    return DecisionProfile(
        answer_first=False,
        state_first=False,
        heavy_lift_hint=False,
        reason="default",
    )


def should_answer_before_delegate(text: str) -> bool:
    """
    True when routing should prefer direct assistant reply over delegation even if
    ``DELEGATE_KEYWORDS`` or PATH heuristics would otherwise fire.

    Only matches explicit status/history-shaped wording — *not* the whole
    ``project_status`` domain (which can include implementation asks).
    """
    raw = str(text or "").strip()
    if not raw:
        return False
    if _HEAVY_IMPLEMENTATION_INTENT_RE.search(raw):
        return False
    return bool(ANSWER_SHAPED_STATUS_OR_HISTORY_RE.search(raw))


def is_cursor_worthy_heavy_lift(text: str) -> bool:
    """True when the message reads like repo execution / verification, not status Q&A."""
    clean = str(text or "").strip()
    if not clean:
        return False
    return bool(_HEAVY_IMPLEMENTATION_INTENT_RE.search(clean))

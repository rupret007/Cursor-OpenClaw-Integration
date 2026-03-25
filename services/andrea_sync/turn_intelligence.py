"""Turn-level domain and context-boundary planning for answer quality."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


ContinuityFocus = Literal[
    "none",
    "blocked_state",
    "recent_outcome_history",
    "cursor_followup_heavy_lift",
]


TurnDomain = Literal[
    "casual_conversation",
    "personal_agenda",
    "attention_today",
    "project_status",
    "approval_state",
    "external_information",
    "opinion_reflection",
    "technical_execution",
]


_APPROVAL_RE = re.compile(
    r"\b(needs?\s+(my|our)\s+approval|awaiting\s+(my|our)\s+approval|"
    r"pending\s+(my|our)\s+approval|waiting\s+on\s+(my|our)\s+approval)\b",
    re.I,
)
# Agenda/day-plan language only — bare "today" alone must not imply personal_agenda
# (e.g. "What's the news today?" in a status-shaped scenario).
_AGENDA_RE = re.compile(
    r"\b("
    r"agenda|"
    r"day'?s\s+plan|plan\s+for\s+today|"
    r"what'?s\s+on\s+(?:the\s+)?agenda|"
    r"anything\s+on\s+(?:the\s+)?agenda|"
    r"what'?s\s+planned\s+(?:for\s+)?today|"
    r"what\s+is\s+planned\s+(?:for\s+)?today|"
    r"planned\s+for\s+today"
    r")\b",
    re.I,
)
# Triage / attention — distinct from generic status or bare calendar asks.
_ATTENTION_TODAY_RE = re.compile(
    r"\b("
    r"pay\s+attention|need\s+to\s+pay\s+attention|what\s+to\s+pay\s+attention|"
    r"what\s+do\s+i\s+need\s+to\s+pay\s+attention|"
    r"what\s+needs\s+(?:my\s+)?attention|"
    r"attention\s+today|"
    r"what\s+should\s+i\s+focus\s+on\s+today"
    r")\b",
    re.I,
)
# News/headlines intent inside a status-followup scenario should stay external_information,
# not personal_agenda via a loose "today" match.
_STATUS_EXTERNAL_NEWS_RE = re.compile(
    r"\b(news|headlines?|what'?s\s+in\s+the\s+news)\b",
    re.I,
)
_OPINION_RE = re.compile(
    r"\b(what(?:'s|s|\s+do)\s+you\s+think|your\s+(opinion|view)|what(?:'s|s)\s+your\s+take)\b",
    re.I,
)
# Blocker / stuck — deterministic state-first lane (must beat generic status fallbacks).
_BLOCKED_STATE_RE = re.compile(
    r"\b("
    r"what'?s\s+blocked|blocked\s+right\s+now|what\s+is\s+blocking|"
    r"main\s+blocker|what\s+are\s+you\s+blocked\s+on|where\s+are\s+we\s+stuck|"
    r"what'?s\s+the\s+blocker|anything\s+blocking"
    r")\b",
    re.I,
)
# Recent trajectory / outcome — prefer receipts and task history over “no active work”.
_RECENT_OUTCOME_HISTORY_RE = re.compile(
    r"\b("
    r"what\s+happened\s+with\s+(?:that\s+)?task|what\s+happened\s+to\s+that\s+task|"
    r"what\s+happened\s+with\s+that\s+work|"
    r"what\s+happened\s+there|what\s+happened\s+with\s+that\b|"
    r"what\s+about\s+that\s+one|what\s+was\s+the\s+result|"
    r"last\s+task|that\s+task\s+earlier|task\s+earlier|"
    r"what\s+was\s+the\s+outcome|what\s+did\s+cursor\s+say|what\s+did\s+cursor\s+do|"
    r"what\s+did\s+it\s+do|"
    r"what\s+happened\s+in\s+(?:the\s+)?cursor\s+thread|"
    r"what\s+did\s+openclaw\s+do|"
    r"recap\s+(?:that\s+)?task|outcome\s+of\s+that|recap\s+that"
    r")\b",
    re.I,
)
# Heavy-lift / Cursor thread follow-ups (orchestration language, not raw plumbing).
_CURSOR_FOLLOWUP_HEAVY_RE = re.compile(
    r"@cursor|\bopenclaw\b|"
    r"continue\s+(?:that|the|this)\s+cursor\s+task|"
    r"continue\s+(?:the\s+)?cursor\s+task|"
    r"resume\s+(?:the\s+)?cursor\s+task|"
    r"continue\s+(?:that|the|this)\s+(?:cursor|heavy)[\s-]?(?:run|task|work)?|"
    r"heavy[\s-]?lift|repo[\s-]?wide",
    re.I,
)


def classify_continuity_focus(text: str) -> ContinuityFocus:
    """Sub-intent for status/continuity turns (priority: blocked > history > cursor)."""
    clean = str(text or "").strip()
    if _BLOCKED_STATE_RE.search(clean):
        return "blocked_state"
    if _RECENT_OUTCOME_HISTORY_RE.search(clean):
        return "recent_outcome_history"
    if _CURSOR_FOLLOWUP_HEAVY_RE.search(clean):
        return "cursor_followup_heavy_lift"
    return "none"


def _policy_flags_for_domain(domain: TurnDomain) -> tuple[bool, bool]:
    """
    (allow_goal_continuity_repair, inject_durable_memory)
    """
    if domain == "external_information":
        return False, False
    if domain in {"project_status", "approval_state"}:
        return True, True
    if domain == "attention_today":
        return False, True
    return False, True


@dataclass(frozen=True)
class TurnPlan:
    domain: TurnDomain
    context_boundary: str
    prefer_state_reply: bool
    force_delegate: bool
    should_repair_generic: bool
    allow_goal_continuity_repair: bool
    inject_durable_memory: bool
    continuity_focus: ContinuityFocus


def build_turn_plan(
    text: str,
    *,
    scenario_id: str,
    projection_has_continuity_state: bool,
    same_chat_delegation_score: int = 0,
) -> TurnPlan:
    clean = str(text or "").strip()
    sid = str(scenario_id or "").strip()
    domain: TurnDomain = "casual_conversation"
    context_boundary = "casual_only"
    prefer_state_reply = False
    force_delegate = False
    should_repair_generic = True
    continuity_focus: ContinuityFocus = classify_continuity_focus(clean)

    if sid in {"repoHelpVerified", "multiStepTroubleshoot", "verificationSensitiveAction"}:
        domain = "technical_execution"
        context_boundary = "technical_execution_only"
        force_delegate = True
    elif sid in {"statusFollowupContinue", "goalContinuationAcrossSessions"}:
        if _STATUS_EXTERNAL_NEWS_RE.search(clean):
            domain = "external_information"
            context_boundary = "external_world_only"
            prefer_state_reply = False
        elif _APPROVAL_RE.search(clean):
            domain = "approval_state"
            context_boundary = "approval_and_plan_state"
        elif _ATTENTION_TODAY_RE.search(clean):
            domain = "attention_today"
            context_boundary = "attention_and_triage_state"
        elif _AGENDA_RE.search(clean):
            domain = "personal_agenda"
            context_boundary = "personal_agenda_state"
        elif _OPINION_RE.search(clean):
            domain = "opinion_reflection"
            context_boundary = "recent_thread_only"
        else:
            domain = "project_status"
            context_boundary = "project_continuity_state"
        # personal_agenda / attention_today must not prefer goal-thread continuity replies.
        if domain in {"project_status", "approval_state"}:
            prefer_state_reply = projection_has_continuity_state
        if domain == "external_information":
            prefer_state_reply = False
    elif sid == "researchSummary":
        domain = "external_information"
        context_boundary = "external_world_only"
    elif sid in {"noteOrReminderCapture", "recentMessagesOrInboxLookup"}:
        domain = "personal_agenda"
        context_boundary = "personal_runtime_state"
    elif sid == "mixedResourceGoal":
        if _STATUS_EXTERNAL_NEWS_RE.search(clean):
            domain = "external_information"
            context_boundary = "external_world_only"
        elif _APPROVAL_RE.search(clean):
            domain = "approval_state"
            context_boundary = "approval_and_plan_state"
            prefer_state_reply = (
                projection_has_continuity_state or same_chat_delegation_score >= 38
            )
        elif _ATTENTION_TODAY_RE.search(clean):
            domain = "attention_today"
            context_boundary = "attention_and_triage_state"
        elif _AGENDA_RE.search(clean):
            domain = "personal_agenda"
            context_boundary = "personal_agenda_state"
        elif _OPINION_RE.search(clean):
            domain = "opinion_reflection"
            context_boundary = "recent_thread_only"
        elif continuity_focus in (
            "recent_outcome_history",
            "blocked_state",
            "cursor_followup_heavy_lift",
        ) and (
            projection_has_continuity_state or same_chat_delegation_score >= 38
        ):
            domain = "project_status"
            context_boundary = "project_continuity_state"
            prefer_state_reply = True
    elif _OPINION_RE.search(clean):
        domain = "opinion_reflection"
        context_boundary = "recent_thread_only"
    elif _ATTENTION_TODAY_RE.search(clean):
        domain = "attention_today"
        context_boundary = "attention_and_triage_state"
    elif _AGENDA_RE.search(clean):
        domain = "personal_agenda"
        context_boundary = "personal_agenda_state"

    if domain == "external_information":
        prefer_state_reply = False

    allow_goal_continuity_repair, inject_durable_memory = _policy_flags_for_domain(domain)
    return TurnPlan(
        domain=domain,
        context_boundary=context_boundary,
        prefer_state_reply=prefer_state_reply,
        force_delegate=force_delegate,
        should_repair_generic=should_repair_generic,
        allow_goal_continuity_repair=allow_goal_continuity_repair,
        inject_durable_memory=inject_durable_memory,
        continuity_focus=continuity_focus,
    )

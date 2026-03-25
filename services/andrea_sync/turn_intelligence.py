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


_APPROVAL_OWNER_FRAGMENT = r"(?:\s+(?:my|our))?"
_APPROVAL_FAMILY_RE = re.compile(
    r"\b("
    r"needs?" + _APPROVAL_OWNER_FRAGMENT + r"\s+approval|"
    r"awaiting" + _APPROVAL_OWNER_FRAGMENT + r"\s+approval|"
    r"pending" + _APPROVAL_OWNER_FRAGMENT + r"\s+approval|"
    r"waiting\s+(?:for|on)" + _APPROVAL_OWNER_FRAGMENT + r"\s+approval|"
    r"what\s+still\s+needs" + _APPROVAL_OWNER_FRAGMENT + r"\s+approval|"
    r"what\s+is\s+(?:waiting|awaiting)(?:\s+(?:for|on))?" + _APPROVAL_OWNER_FRAGMENT + r"\s+approval|"
    r"what(?:'s|s)\s+(?:waiting|awaiting)(?:\s+(?:for|on))?" + _APPROVAL_OWNER_FRAGMENT + r"\s+approval|"
    r"do\s+i\s+have\s+anything\s+pending" + _APPROVAL_OWNER_FRAGMENT + r"\s+approval|"
    r"what\s+approvals?\s+are\s+waiting"
    r")\b",
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
_GENERIC_RECENT_OUTCOME_HISTORY_RE = re.compile(
    r"\b("
    r"what\s+happened\s+with\s+(?:that\s+)?task|what\s+happened\s+to\s+that\s+task|"
    r"what\s+happened\s+with\s+that\s+work|"
    r"last\s+task|that\s+task\s+earlier|task\s+earlier|"
    r"what\s+was\s+the\s+outcome|"
    r"recap\s+(?:that\s+)?task|outcome\s+of\s+that"
    r")\b",
    re.I,
)
_CURSOR_RECALL_EXPLICIT_RE = re.compile(
    r"\b("
    r"what\s+did\s+(?:cursor|openclaw)\s+(?:say|do)|"
    r"what\s+happened\s+(?:in|to|with)\s+(?:the\s+)?(?:cursor|openclaw)(?:\s+thread)?"
    r")\b",
    re.I,
)
_ANAPHORIC_OUTCOME_RE = re.compile(
    r"\b("
    r"what\s+happened\s+there|what\s+happened\s+with\s+that(?!\s+task\b)|"
    r"what\s+about\s+that\s+one|what\s+was\s+the\s+result|"
    r"what\s+did\s+it\s+do|"
    r"recap\s+that"
    r")\b",
    re.I,
)
# Heavy-lift / Cursor thread follow-ups (orchestration language, not raw plumbing).
# Short identity questions about the tooling — not heavy-lift continuation.
_TOOLING_IDENTITY_Q_RE = re.compile(
    r"^\s*(?:"
    r"is\s+this\s+openclaw|is\s+this\s+cursor|"
    r"what\s+is\s+openclaw|what\s+is\s+cursor|"
    r"are\s+you\s+openclaw|are\s+you\s+cursor"
    r")\s*\??\s*$",
    re.I,
)

_CURSOR_FOLLOWUP_HEAVY_RE = re.compile(
    r"@cursor|\bopenclaw\b|"
    r"continue\s+(?:that|the|this)\s+cursor\s+task|"
    r"continue\s+(?:the\s+)?cursor\s+task|"
    r"resume\s+(?:the\s+)?cursor\s+task|"
    r"continue\s+(?:that|the|this)\s+(?:cursor|heavy)[\s-]?(?:run|task|work)?|"
    r"heavy[\s-]?lift|repo[\s-]?wide",
    re.I,
)


def is_approval_state_question(text: str) -> bool:
    """True for approval inventory / pending-approval questions across close paraphrases."""
    return bool(_APPROVAL_FAMILY_RE.search(str(text or "").strip()))


def is_explicit_cursor_recall_question(text: str) -> bool:
    """True for explicit Cursor/OpenClaw recap asks that should share strict recall rails."""
    return bool(_CURSOR_RECALL_EXPLICIT_RE.search(str(text or "").strip()))


def is_anaphoric_outcome_recall_question(text: str) -> bool:
    """True for short follow-ups that point at a recent outcome via continuity context."""
    return bool(_ANAPHORIC_OUTCOME_RE.search(str(text or "").strip()))


def is_cursor_recall_family_question(text: str, *, include_anaphora: bool = True) -> bool:
    """Shared Cursor recall family detector used by routing, ranking, and recall rails."""
    if is_explicit_cursor_recall_question(text):
        return True
    return include_anaphora and is_anaphoric_outcome_recall_question(text)


def is_recent_outcome_history_question(text: str) -> bool:
    """Outcome/history family: generic task history plus Cursor recall variants."""
    clean = str(text or "").strip()
    return bool(_GENERIC_RECENT_OUTCOME_HISTORY_RE.search(clean)) or is_cursor_recall_family_question(
        clean,
        include_anaphora=True,
    )


def classify_continuity_focus(text: str) -> ContinuityFocus:
    """Sub-intent for status/continuity turns (priority: blocked > history > cursor)."""
    clean = str(text or "").strip()
    if _BLOCKED_STATE_RE.search(clean):
        return "blocked_state"
    if is_recent_outcome_history_question(clean):
        return "recent_outcome_history"
    if _TOOLING_IDENTITY_Q_RE.match(clean):
        return "none"
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
        elif is_approval_state_question(clean):
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
        # Inbox / recent-text scenarios should not inherit schedule-oriented agenda hints.
        context_boundary = (
            "messaging_read_lane"
            if sid == "recentMessagesOrInboxLookup"
            else "personal_runtime_state"
        )
    elif sid == "mixedResourceGoal":
        if _STATUS_EXTERNAL_NEWS_RE.search(clean):
            domain = "external_information"
            context_boundary = "external_world_only"
        elif is_approval_state_question(clean):
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

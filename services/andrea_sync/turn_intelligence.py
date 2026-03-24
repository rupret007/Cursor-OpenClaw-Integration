"""Turn-level domain and context-boundary planning for answer quality."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


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
    r"anything\s+on\s+(?:the\s+)?agenda"
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
_ATTENTION_RE = re.compile(
    r"\b("
    r"what\s+should\s+i\s+focus\s+on(?:\s+today)?|"
    r"what\s+needs?\s+my\s+attention(?:\s+today)?|"
    r"where\s+should\s+i\s+focus(?:\s+today)?|"
    r"top\s+priorit(?:y|ies)\s+today"
    r")\b",
    re.I,
)


def _policy_flags_for_domain(domain: TurnDomain) -> tuple[bool, bool]:
    """
    (allow_goal_continuity_repair, inject_durable_memory)
    """
    if domain == "external_information":
        return False, False
    if domain in {"project_status", "approval_state"}:
        return True, True
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


def build_turn_plan(
    text: str,
    *,
    scenario_id: str,
    projection_has_continuity_state: bool,
) -> TurnPlan:
    clean = str(text or "").strip()
    sid = str(scenario_id or "").strip()
    domain: TurnDomain = "casual_conversation"
    context_boundary = "casual_only"
    prefer_state_reply = False
    force_delegate = False
    should_repair_generic = True

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
        elif _ATTENTION_RE.search(clean):
            domain = "attention_today"
            context_boundary = "attention_runtime_state"
        elif _AGENDA_RE.search(clean):
            domain = "personal_agenda"
            context_boundary = "personal_agenda_state"
        elif _OPINION_RE.search(clean):
            domain = "opinion_reflection"
            context_boundary = "recent_thread_only"
        else:
            domain = "project_status"
            context_boundary = "project_continuity_state"
        # personal_agenda must not prefer goal-thread continuity replies (no calendar source yet).
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
    elif _OPINION_RE.search(clean):
        domain = "opinion_reflection"
        context_boundary = "recent_thread_only"
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
    )

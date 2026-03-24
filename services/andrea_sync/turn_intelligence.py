"""Turn-level domain and context-boundary planning for answer quality."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


TurnDomain = Literal[
    "casual_conversation",
    "personal_agenda",
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
_AGENDA_RE = re.compile(
    r"\b(agenda|today|day'?s\s+plan|plan\s+for\s+today)\b",
    re.I,
)
_OPINION_RE = re.compile(
    r"\b(what(?:'s|s|\s+do)\s+you\s+think|your\s+(opinion|view)|what(?:'s|s)\s+your\s+take)\b",
    re.I,
)


@dataclass(frozen=True)
class TurnPlan:
    domain: TurnDomain
    context_boundary: str
    prefer_state_reply: bool
    force_delegate: bool
    should_repair_generic: bool


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
        if _APPROVAL_RE.search(clean):
            domain = "approval_state"
            context_boundary = "approval_and_plan_state"
        elif _AGENDA_RE.search(clean):
            domain = "personal_agenda"
            context_boundary = "personal_agenda_state"
        else:
            domain = "project_status"
            context_boundary = "project_continuity_state"
        prefer_state_reply = projection_has_continuity_state
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
    return TurnPlan(
        domain=domain,
        context_boundary=context_boundary,
        prefer_state_reply=prefer_state_reply,
        force_delegate=force_delegate,
        should_repair_generic=should_repair_generic,
    )


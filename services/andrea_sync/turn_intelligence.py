"""Turn-level domain and context-boundary planning for answer quality."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from .user_surface import is_internal_runtime_text, is_stale_openclaw_narrative


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
    "technical_guidance",
    "opinion_reflection",
    "technical_execution",
]

OpenClawSourceRole = Literal[
    "non_openclaw",
    "internal_runtime",
    "stale_narrative",
    "collaboration_summary",
    "collaboration_blocker",
]

OpenClawRoleDecision = Literal["allow", "demote", "exclude"]

AnswerFamilyName = Literal[
    "general_status",
    "cursor_recall",
    "cursor_continuation",
    "blocked_state",
    "approval_state",
    "grounded_research",
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
_OPENCLAW_COLLAB_ASK_RE = re.compile(
    r"\b("
    r"what\s+did\s+(?:openclaw|cursor)\s+(?:do|say)|"
    r"what\s+happened\s+(?:with|to|in)\s+(?:openclaw|cursor)|"
    r"why\s+(?:is|was)\s+(?:openclaw|cursor)\s+blocked|"
    r"openclaw\s+(?:blocked|failure|failed|error)|"
    r"cross[-\s]?model\s+handoff|"
    r"collaboration\s+(?:state|status|blocked|failure)"
    r")\b",
    re.I,
)

_TECHNICAL_GUIDANCE_RE = re.compile(
    r"\b("
    r"how\s+do\s+i|"
    r"what\s+does\s+this\s+(?:[\w-]+\s+)?error|"
    r"what\s+does\s+this\s+(?:ssl|tls|certificate)\s+(?:error|warning|issue)\s+mean|"
    r"why\s+(?:is|does)\s+.*(?:error|fail|failing|timeout|crash)|"
    r"why\s+am\s+i\s+seeing\s+.*(?:ssl|tls|certificate|warning|error)|"
    r"how\s+should\s+i\s+configure|"
    r"what\s+is\s+the\s+usual\s+fix|"
    r"best\s+way\s+to\s+(?:fix|configure|debug)|"
    r"explain\s+(?:this|that)\s+(?:error|issue|warning)|"
    r"(?:ssl|tls|certificate)\s+(?:error|warning|issue)"
    r")\b",
    re.I,
)

_CASUAL_SOCIAL_ONLY_RE = re.compile(
    r"^\s*("
    r"hi|hello|hey|"
    r"good\s+(?:morning|afternoon|evening)|"
    r"thanks|thank\s+you|"
    r"how(?:'s|\s+is)\s+it\s+going|"
    r"how\s+are\s+things|"
    r"how(?:'s|\s+is)\s+everything|"
    r"how\s+are\s+you|how're\s+you"
    r")\s*[?.!]*\s*$",
    re.I,
)
_DIRECT_SUBSTANTIVE_HINT_RE = re.compile(
    r"\b("
    r"how|why|what|which|when|where|"
    r"error|timeout|warning|issue|failing|crash|"
    r"configure|configuration|setup|retry|backoff|"
    r"debug|diagnose|cause|mitigation|"
    r"tool|system|code|concept|difference|"
    r"explain|meaning|mean|latest|news|headline|"
    r"fix|best\s+practice|recommend"
    r")\b",
    re.I,
)
_EXECUTION_HEAVY_OR_REPO_RE = re.compile(
    r"\b("
    r"implement|refactor|migrate|edit\s+file|write\s+code|"
    r"create\s+pr|open\s+pr|pull\s+request|"
    r"run\s+tests?|fix\s+the\s+code|debug\s+in\s+repo|"
    r"apply\s+patch|commit\s+this|"
    r"restart\s+service|reload\s+service"
    r")\b",
    re.I,
)
_PATH_OR_FILE_RE = re.compile(
    r"[/~][\w.\-~/]+|`[^`]+`|\b\w+\.(py|ts|tsx|js|jsx|md|sh|json|yaml|yml)\b",
    re.I,
)


@dataclass(frozen=True)
class DirectAnswerPolicy:
    allow_casual_social_fallback: bool
    lookup_eligible: bool
    preferred_lookup_domain: TurnDomain | str


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


def is_casual_social_only_turn(text: str) -> bool:
    clean = str(text or "").strip()
    if not clean:
        return False
    return bool(_CASUAL_SOCIAL_ONLY_RE.match(clean))


def is_tooling_identity_question(text: str) -> bool:
    return bool(_TOOLING_IDENTITY_Q_RE.match(str(text or "").strip()))


def is_openclaw_collaboration_state_question(text: str) -> bool:
    clean = str(text or "").strip()
    if not clean:
        return False
    if _OPENCLAW_COLLAB_ASK_RE.search(clean):
        return True
    if "openclaw" in clean.lower() and (
        "blocked" in clean.lower() or "happened" in clean.lower() or "continue" in clean.lower()
    ):
        return True
    return False


def classify_openclaw_source_role(*, source: str, candidate_text: str) -> OpenClawSourceRole:
    text = str(candidate_text or "").strip()
    if not text:
        return "non_openclaw"
    if is_internal_runtime_text(text):
        return "internal_runtime"
    if is_stale_openclaw_narrative(text):
        return "stale_narrative"
    src = str(source or "").strip()
    if src in {"blocked_state_reply"}:
        return "collaboration_blocker"
    if src in {"cursor_continuity_recall", "cursor_heavy_lift_context"}:
        return "collaboration_summary"
    if src in {"goal_status", "goal_continuity"}:
        low = text.lower()
        blocker_markers = (
            "blocked:",
            "main blocker",
            "internal collaboration limitation",
            "cross-model handoff",
            "cross lane",
            "cross-lane",
        )
        if any(m in low for m in blocker_markers):
            return "collaboration_blocker"
    return "non_openclaw"


def openclaw_role_relevance_for_turn(
    *,
    source: str,
    candidate_text: str,
    user_text: str,
    turn_plan: "TurnPlan",
) -> OpenClawRoleDecision:
    role = classify_openclaw_source_role(source=source, candidate_text=candidate_text)
    if role == "non_openclaw":
        return "allow"
    if role in {"internal_runtime", "stale_narrative"}:
        return "exclude"

    user = str(user_text or "").strip()
    domain = str(turn_plan.domain or "")
    focus = str(turn_plan.continuity_focus or "")
    explicit_collab = is_openclaw_collaboration_state_question(user)
    approval_only = is_approval_state_question(user) and "block" not in user.lower() and not explicit_collab
    if is_casual_social_only_turn(user) or is_tooling_identity_question(user):
        return "exclude"
    if domain in {"external_information", "technical_guidance", "personal_agenda", "attention_today"}:
        return "allow" if explicit_collab else "exclude"
    if domain == "opinion_reflection":
        return "exclude"
    if role == "collaboration_blocker":
        if explicit_collab or focus == "blocked_state":
            return "allow"
        if approval_only:
            return "demote"
        if domain == "project_status":
            return "demote"
        return "exclude"
    if role == "collaboration_summary":
        if explicit_collab or focus in {"recent_outcome_history", "cursor_followup_heavy_lift"}:
            return "allow"
        if domain == "project_status":
            return "demote"
        return "exclude"
    return "allow"


def is_execution_heavy_or_repo_action(text: str) -> bool:
    clean = str(text or "").strip()
    if not clean:
        return False
    if _PATH_OR_FILE_RE.search(clean):
        return True
    return bool(_EXECUTION_HEAVY_OR_REPO_RE.search(clean))


def is_substantive_non_social_question(text: str) -> bool:
    clean = str(text or "").strip()
    if not clean or is_casual_social_only_turn(clean):
        return False
    if is_recent_outcome_history_question(clean):
        return True
    if is_approval_state_question(clean):
        return True
    if _TECHNICAL_GUIDANCE_RE.search(clean):
        return True
    if _STATUS_EXTERNAL_NEWS_RE.search(clean):
        return True
    if _TOOLING_IDENTITY_Q_RE.match(clean):
        return True
    if _OPINION_RE.search(clean):
        return True
    words = clean.split()
    if len(words) >= 7 and _DIRECT_SUBSTANTIVE_HINT_RE.search(clean):
        return True
    if "?" in clean and _DIRECT_SUBSTANTIVE_HINT_RE.search(clean):
        return True
    return False


def build_direct_answer_policy(
    text: str,
    *,
    scenario_id: str,
    turn_plan: TurnPlan,
) -> DirectAnswerPolicy:
    clean = str(text or "").strip()
    dom = str(turn_plan.domain or "")
    sid = str(scenario_id or "")
    social = is_casual_social_only_turn(clean)
    if social:
        return DirectAnswerPolicy(
            allow_casual_social_fallback=True,
            lookup_eligible=False,
            preferred_lookup_domain="",
        )
    if bool(turn_plan.force_delegate) or is_execution_heavy_or_repo_action(clean):
        return DirectAnswerPolicy(
            allow_casual_social_fallback=False,
            lookup_eligible=False,
            preferred_lookup_domain="",
        )
    if dom in {"external_information", "technical_guidance"}:
        return DirectAnswerPolicy(
            allow_casual_social_fallback=False,
            lookup_eligible=True,
            preferred_lookup_domain=dom,
        )
    if dom in {"project_status", "approval_state", "attention_today", "personal_agenda"}:
        return DirectAnswerPolicy(
            allow_casual_social_fallback=False,
            lookup_eligible=False,
            preferred_lookup_domain="",
        )
    substantive = is_substantive_non_social_question(clean)
    if substantive and sid == "researchSummary":
        preferred = "technical_guidance" if _TECHNICAL_GUIDANCE_RE.search(clean) else "external_information"
        return DirectAnswerPolicy(
            allow_casual_social_fallback=False,
            lookup_eligible=True,
            preferred_lookup_domain=preferred,
        )
    if substantive and (dom in {"casual_conversation", "opinion_reflection"} or sid == "mixedResourceGoal"):
        preferred = "technical_guidance" if _TECHNICAL_GUIDANCE_RE.search(clean) else "external_information"
        return DirectAnswerPolicy(
            allow_casual_social_fallback=False,
            lookup_eligible=True,
            preferred_lookup_domain=preferred,
        )
    return DirectAnswerPolicy(
        allow_casual_social_fallback=False if substantive else True,
        lookup_eligible=False,
        preferred_lookup_domain="",
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
    if domain in {"external_information", "technical_guidance"}:
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


@dataclass(frozen=True)
class AnswerFamilyProfile:
    family: AnswerFamilyName
    allowed_sources: tuple[str, ...]
    min_score: int = 70


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
        if _TECHNICAL_GUIDANCE_RE.search(clean):
            domain = "technical_guidance"
            context_boundary = "technical_lookup_guidance"
        else:
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
        elif _TECHNICAL_GUIDANCE_RE.search(clean):
            domain = "technical_guidance"
            context_boundary = "technical_lookup_guidance"
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

    if domain in {"external_information", "technical_guidance"}:
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


def resolve_answer_family_profile(text: str, turn_plan: TurnPlan) -> AnswerFamilyProfile:
    """
    Compact semantic family profile used by selector/eval as a stable invariant.
    """
    clean = str(text or "").strip()
    domain = str(turn_plan.domain or "")
    focus = str(turn_plan.continuity_focus or "")
    if domain == "approval_state" or is_approval_state_question(clean):
        return AnswerFamilyProfile(
            family="approval_state",
            allowed_sources=("goal_status",),
            min_score=68,
        )
    if focus == "blocked_state":
        return AnswerFamilyProfile(
            family="blocked_state",
            allowed_sources=("blocked_state_reply", "goal_status"),
            min_score=68,
        )
    if focus == "cursor_followup_heavy_lift":
        return AnswerFamilyProfile(
            family="cursor_continuation",
            allowed_sources=("cursor_heavy_lift_context",),
            min_score=70,
        )
    if focus == "recent_outcome_history":
        if is_explicit_cursor_recall_question(clean):
            return AnswerFamilyProfile(
                family="cursor_recall",
                allowed_sources=("cursor_continuity_recall",),
                min_score=72,
            )
        return AnswerFamilyProfile(
            family="cursor_recall",
            allowed_sources=("cursor_continuity_recall", "goal_status"),
            min_score=70,
        )
    if domain in {"external_information", "technical_guidance"}:
        return AnswerFamilyProfile(
            family="grounded_research",
            allowed_sources=("grounded_research_lookup",),
            min_score=66,
        )
    return AnswerFamilyProfile(
        family="general_status",
        allowed_sources=("goal_status", "goal_continuity"),
        min_score=70,
    )

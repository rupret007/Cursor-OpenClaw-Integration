"""Rule-based intent classifier for protected assistant and Cursor control requests."""

from __future__ import annotations

import re
from typing import Iterable

from .model import ClassifiedIntent, IntentEnvelope

_SCHEDULE_RE = re.compile(
    r"\b("
    r"what(?:'s|s|\s+is)\s+on\s+my\s+schedule\s+today|"
    r"what\s+do\s+i\s+have\s+on\s+my\s+schedule\s+today|"
    r"what\s+do\s+i\s+have\s+today|"
    r"what\s+are\s+my\s+plans\s+today|"
    r"what(?:'s|s|\s+is)\s+planned\s+(?:for\s+)?today|"
    r"what(?:'s|s|\s+is)\s+on\s+(?:the\s+)?agenda(?:\s+today)?|"
    r"my\s+agenda(?:\s+today)?|"
    r"plan\s+for\s+today"
    r")\b",
    re.I,
)
_WEATHER_RE = re.compile(
    r"\b(weather|forecast|temperature|rain(?:ing)?|snow(?:ing)?|wind(?:y)?)\b",
    re.I,
)
_TIME_RE = re.compile(
    r"\b(what\s+time\s+is\s+it|time\s+right\s+now|current\s+time)\b",
    re.I,
)
_AVAILABILITY_RE = re.compile(
    r"\b(am\s+i\s+free|availability|available\s+(?:today|now)|free\s+(?:today|now))\b",
    re.I,
)
_CONTROL_TARGET_RE = r"(?:jobs?|agents?|runs?)"
_CANCEL_ALL_JOBS_RE = re.compile(
    rf"\b(cancel|stop)\b.*\ball\s+{_CONTROL_TARGET_RE}\b",
    re.I,
)
_LIST_ACTIVE_JOBS_RE = re.compile(
    rf"\b((?:what|which)\s+{_CONTROL_TARGET_RE}\s+are\s+(?:still\s+)?running|"
    rf"(?:list|show|summari[sz]e)\s+(?:the\s+)?(?:active|running)\s+{_CONTROL_TARGET_RE}|"
    rf"what\s+jobs?\s+are\s+still\s+running|"
    rf"{_CONTROL_TARGET_RE}\s+still\s+running)\b",
    re.I,
)
_LIST_ALL_JOBS_RE = re.compile(
    rf"\b(list|show|summari[sz]e|status|inspect)\b.*\b(all\s+)?{_CONTROL_TARGET_RE}\b",
    re.I,
)
_STATUS_JOB_RE = re.compile(
    rf"\b(status|inspect)\b.*\b{_CONTROL_TARGET_RE}\b",
    re.I,
)
_CONTINUE_RE = re.compile(
    r"\b(continue|keep\s+working|finish\s+that|same\s+task|resume\s+that|resume\s+the\s+task)\b",
    re.I,
)
_CODE_ACTION_RE = re.compile(
    r"\b("
    r"fix|patch|implement|refactor|create\s+branch|open\s+(?:a\s+)?pr|create\s+pr|"
    r"update\s+docs|modify|edit|inspect\s+(?:the\s+)?repo|review\s+(?:the\s+)?repo|"
    r"run\s+tests?|debug|change\s+instructions|edit\s+behavior\s+prompts?"
    r")\b",
    re.I,
)
_CODE_OBJECT_RE = re.compile(
    r"\b(repo|repository|branch|pr\b|pull\s+request|file|files|docs|prompt|service|tests?|bug|feature)\b|"
    r"[/~][\w.\-~/]+|`[^`]+`|\b\w+\.(py|ts|tsx|js|jsx|md|sh|json|yaml|yml)\b",
    re.I,
)
_EXPLICIT_LANE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openclaw", re.compile(r"(?<!\w)@openclaw\b", re.I)),
    ("cursor", re.compile(r"(?<!\w)@cursor\b", re.I)),
    ("andrea", re.compile(r"(?<!\w)@andrea\b", re.I)),
    ("openclaw", re.compile(r"\b(?:ask|tell)\s+@?openclaw\b", re.I)),
    ("cursor", re.compile(r"\b(?:ask|tell)\s+@?cursor\b", re.I)),
    ("openclaw", re.compile(r"^\s*@?openclaw\b", re.I)),
    ("cursor", re.compile(r"^\s*@?cursor\b", re.I)),
)


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _detect_explicit_lane(text: str) -> str:
    raw = str(text or "").strip()
    for lane, pattern in _EXPLICIT_LANE_PATTERNS:
        if pattern.search(raw):
            return lane
    return ""


def _intent_from_match(
    *,
    type_: str,
    action: str,
    target: str = "",
    object_: str = "",
    protected_category: str = "",
    source_text: str = "",
    required_user_visible_outcome: str = "",
    risk_level: str = "low",
    time_sensitivity: str = "normal",
    coalescing_eligible: bool = True,
) -> ClassifiedIntent:
    return ClassifiedIntent(
        type=type_,
        action=action,
        target=target,
        object=object_,
        protected_category=protected_category,
        source_text=source_text,
        required_user_visible_outcome=required_user_visible_outcome,
        risk_level=risk_level,
        time_sensitivity=time_sensitivity,
        coalescing_eligible=coalescing_eligible,
    )


def _sorted_unique_intents(items: Iterable[tuple[int, ClassifiedIntent]]) -> tuple[ClassifiedIntent, ...]:
    out: list[ClassifiedIntent] = []
    seen: set[tuple[str, str, str, str]] = set()
    for _pos, intent in sorted(items, key=lambda item: item[0]):
        key = (intent.type, intent.action, intent.target, intent.protected_category)
        if key in seen:
            continue
        seen.add(key)
        out.append(intent)
    return tuple(out)


def classify_intent_envelope(text: str) -> IntentEnvelope:
    raw = _normalize_spaces(text)
    explicit_lane = _detect_explicit_lane(raw)
    matches: list[tuple[int, ClassifiedIntent]] = []

    schedule = _SCHEDULE_RE.search(raw)
    if schedule:
        matches.append(
            (
                schedule.start(),
                _intent_from_match(
                    type_="personal_assistant",
                    action="get_schedule_today",
                    target="today",
                    object_="schedule",
                    protected_category="calendar",
                    source_text=schedule.group(0),
                    required_user_visible_outcome="today schedule answer",
                    time_sensitivity="high",
                    coalescing_eligible=False,
                ),
            )
        )

    weather = _WEATHER_RE.search(raw)
    if weather:
        matches.append(
            (
                weather.start(),
                _intent_from_match(
                    type_="personal_assistant",
                    action="get_weather",
                    target="current",
                    object_="weather",
                    protected_category="weather",
                    source_text=weather.group(0),
                    required_user_visible_outcome="weather answer",
                    time_sensitivity="high",
                    coalescing_eligible=False,
                ),
            )
        )

    time_match = _TIME_RE.search(raw)
    if time_match:
        matches.append(
            (
                time_match.start(),
                _intent_from_match(
                    type_="personal_assistant",
                    action="get_time",
                    target="current",
                    object_="time",
                    protected_category="time",
                    source_text=time_match.group(0),
                    required_user_visible_outcome="current time answer",
                    time_sensitivity="high",
                    coalescing_eligible=False,
                ),
            )
        )

    availability = _AVAILABILITY_RE.search(raw)
    if availability:
        matches.append(
            (
                availability.start(),
                _intent_from_match(
                    type_="personal_assistant",
                    action="get_availability",
                    target="today",
                    object_="availability",
                    protected_category="availability",
                    source_text=availability.group(0),
                    required_user_visible_outcome="availability answer",
                    time_sensitivity="high",
                    coalescing_eligible=False,
                ),
            )
        )

    cancel_all = _CANCEL_ALL_JOBS_RE.search(raw)
    if cancel_all:
        matches.append(
            (
                cancel_all.start(),
                _intent_from_match(
                    type_="control_plane",
                    action="cancel_jobs",
                    target="all_jobs",
                    object_="jobs",
                    source_text=cancel_all.group(0),
                    required_user_visible_outcome="job cancellation summary",
                    risk_level="medium",
                    time_sensitivity="high",
                    coalescing_eligible=False,
                ),
            )
        )

    list_active = _LIST_ACTIVE_JOBS_RE.search(raw)
    if list_active:
        matches.append(
            (
                list_active.start(),
                _intent_from_match(
                    type_="control_plane",
                    action="list_active_jobs",
                    target="active_jobs",
                    object_="jobs",
                    source_text=list_active.group(0),
                    required_user_visible_outcome="active jobs summary",
                    risk_level="low",
                    time_sensitivity="high",
                    coalescing_eligible=False,
                ),
            )
        )
    elif _LIST_ALL_JOBS_RE.search(raw) or _STATUS_JOB_RE.search(raw):
        list_all = _LIST_ALL_JOBS_RE.search(raw) or _STATUS_JOB_RE.search(raw)
        assert list_all is not None
        matches.append(
            (
                list_all.start(),
                _intent_from_match(
                    type_="control_plane",
                    action="list_jobs",
                    target="all_jobs",
                    object_="jobs",
                    source_text=list_all.group(0),
                    required_user_visible_outcome="jobs summary",
                    risk_level="low",
                    time_sensitivity="high",
                    coalescing_eligible=False,
                ),
            )
        )

    continuation = _CONTINUE_RE.search(raw)
    if continuation:
        matches.append(
            (
                continuation.start(),
                _intent_from_match(
                    type_="continuation",
                    action="continue_task",
                    target="active_task",
                    source_text=continuation.group(0),
                    required_user_visible_outcome="continuation acknowledged",
                    time_sensitivity="normal",
                    coalescing_eligible=True,
                ),
            )
        )

    if _CODE_ACTION_RE.search(raw) and _CODE_OBJECT_RE.search(raw):
        code_action = _CODE_ACTION_RE.search(raw)
        assert code_action is not None
        matches.append(
            (
                code_action.start(),
                _intent_from_match(
                    type_="code_plane",
                    action="repo_execute",
                    target="repository",
                    object_="repo",
                    source_text=code_action.group(0),
                    required_user_visible_outcome="code execution started",
                    risk_level="medium",
                    time_sensitivity="normal",
                    coalescing_eligible=bool(continuation),
                ),
            )
        )
    elif explicit_lane == "cursor" and not matches:
        matches.append(
            (
                0,
                _intent_from_match(
                    type_="code_plane",
                    action="repo_execute",
                    target="repository",
                    object_="repo",
                    source_text=raw,
                    required_user_visible_outcome="code execution started",
                    risk_level="medium",
                    time_sensitivity="normal",
                    coalescing_eligible=bool(continuation),
                ),
            )
        )

    intents = _sorted_unique_intents(matches)
    control_plane_flag = any(intent.type == "control_plane" for intent in intents)
    code_plane_flag = any(intent.type == "code_plane" for intent in intents)
    protected_categories = [intent.protected_category for intent in intents if intent.protected_category]
    protected_category = protected_categories[0] if len(protected_categories) == 1 else ""
    outcomes = tuple(
        outcome
        for outcome in (intent.required_user_visible_outcome for intent in intents)
        if outcome
    )
    coalescing_eligible = bool(intents)
    if control_plane_flag or protected_categories:
        coalescing_eligible = False
    elif any(not intent.coalescing_eligible for intent in intents):
        coalescing_eligible = False
    elif any(intent.type == "continuation" for intent in intents):
        coalescing_eligible = True

    if len(intents) > 1:
        intent_family = "mixed_bundle"
    elif control_plane_flag:
        intent_family = "control_plane"
    elif code_plane_flag:
        intent_family = "repo_execute"
    elif protected_categories:
        intent_family = "personal_assistant"
    elif intents and intents[0].type == "continuation":
        intent_family = "continuation"
    else:
        intent_family = "unknown"

    primary = intents[0] if intents else None
    risk_level = "low"
    if any(intent.risk_level == "medium" for intent in intents):
        risk_level = "medium"
    if any(intent.time_sensitivity == "high" for intent in intents):
        time_sensitivity = "high"
    else:
        time_sensitivity = "normal"

    return IntentEnvelope(
        raw_text=raw,
        explicit_lane=explicit_lane,
        intents=intents,
        intent_family=intent_family,
        action=primary.action if primary else "",
        object=primary.object if primary else "",
        protected_category=protected_category,
        control_plane_flag=control_plane_flag,
        code_plane_flag=code_plane_flag,
        coalescing_eligible=coalescing_eligible,
        required_user_visible_outcomes=outcomes,
        risk_level=risk_level,
        time_sensitivity=time_sensitivity,
    )

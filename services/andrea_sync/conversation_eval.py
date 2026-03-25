"""Conversational quality evaluation: suites, capture, deterministic detectors, LLM roles, fix briefs.

Used by experience assurance when ``suite=conversation_core``. Runtime semantic adjudication
helpers are gated behind ``ANDREA_RUNTIME_SEMANTIC_ADJUDICATOR``.
"""
from __future__ import annotations

import json
import os
import random
import time
import re
import urllib.error
import urllib.request
from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence
from unittest import mock

from .andrea_router import is_generic_direct_reply
from .assistant_answer_composer import (
    _cursor_recall_composition_is_metadata_led,
    _cursor_recall_output_should_force_clean_fallback,
    draft_implies_false_completion,
    draft_should_force_continuity_repair,
    is_continuation_fallback_family_text,
    is_cursor_thread_recall_question,
    is_strict_cursor_domain_recall_question,
)
from .bus import handle_command
from .experience_assurance import (
    HarnessInfraError,
    _render_telegram_final_message,
    experience_progress_enabled,
)
from .experience_types import ExperienceCheckResult, ExperienceObservation, ExperienceScenario
from .model_router import model_for_role
from .projector import project_task_dict
from .scenario_runtime import resolve_scenario
from .schema import CommandType, EventType, TaskStatus
from .semantic_continuity import resolve_semantic_continuity_patch, same_chat_max_delegation_score
from .store import (
    append_event,
    create_goal,
    create_goal_approval,
    get_task_channel,
    get_task_principal_id,
    insert_user_outcome_receipt,
    link_task_to_goal,
)
from .turn_intelligence import build_turn_plan, resolve_answer_family_profile
from .telegram_format import format_direct_message
from .user_surface import is_internal_runtime_text, sanitize_user_surface_text

# --- Failure taxonomy (families map to issue_code prefixes) ---
FAILURE_FAMILIES = (
    "generic_fallback_leak",
    "question_echo",
    "metadata_surface_leak",
    "false_completion",
    "wrong_domain_contamination",
    "cursor_recall_failure",
    "cursor_continuation_failure",
    "followup_carryover_failure",
    "wrong_context_boundary",
    "overly_mechanical_wording",
    "delegation_miss",
    "external_info_contamination",
    "thin_summary",
)

MECHANICAL_PHRASES = (
    "as an ai",
    "i cannot",
    "i don't have access",
    "based on the information provided",
    "let me know if you need anything else",
)

CURSOR_GRACE_SNIPPETS = (
    "not finding a strong stored summary",
    "don't have enough recorded history",
    "check the latest tracked state",
)


def _looks_fallback_shaped_reply(text: str) -> bool:
    low = str(text or "").strip().lower()
    if not low:
        return True
    patterns = (
        "not finding a recent clean cursor result",
        "not finding a recent cursor workstream",
        "i do not see active tracked work right now",
        "i'm not seeing any approval requests waiting on you right now",
        "status / follow-up reply",
    )
    return any(p in low for p in patterns)

_TOOL_TEXT_LANE_PRIOR_RE = re.compile(
    r"\b(?:bluebubbles|text\s+messages?|imessages?|recent\s+messages?)\b",
    re.I,
)
_TEXT_SUMMARIZE_FOLLOWUP_RE = re.compile(
    r"\b(?:summarize|summarise)\s+(?:my\s+)?(?:texts?|messages?)\b",
    re.I,
)
_TEXT_LANE_REPLY_MARKERS_RE = re.compile(
    r"\b(?:text|message|messages|inbox|imessage|bluebubbles|sms)\b",
    re.I,
)
_TEXT_LANE_ASSISTANT_REASONS = frozenset(
    {
        "recent_text_messages_ready",
        "recent_text_messages_failed",
        "recent_text_messages_failed_contaminated",
        "recent_text_messages_unavailable",
        "messaging_capability_read_answer",
        "messaging_capability_answer",
    }
)
_OUTBOUND_ONLY_CAPABILITY_BOILERPLATE_RE = re.compile(
    r"draft\s+the\s+message|confirmation\s+before\s+sending",
    re.I,
)


def _clip(value: Any, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _task_last_reply(detail: Dict[str, Any]) -> str:
    meta = detail.get("task", {}).get("meta", {})
    assistant = meta.get("assistant") if isinstance(meta.get("assistant"), dict) else {}
    return str(assistant.get("last_reply") or "").strip()


def _task_route(detail: Dict[str, Any]) -> str:
    meta = detail.get("task", {}).get("meta", {})
    assistant = meta.get("assistant") if isinstance(meta.get("assistant"), dict) else {}
    return str(assistant.get("route") or "").strip()


def _task_assistant_reason(detail: Dict[str, Any]) -> str:
    meta = detail.get("task", {}).get("meta", {})
    assistant = meta.get("assistant") if isinstance(meta.get("assistant"), dict) else {}
    return str(assistant.get("reason") or "").strip()


def _latest_assistant_reply_payload(detail: Dict[str, Any]) -> Dict[str, Any]:
    events = detail.get("events")
    if not isinstance(events, list):
        return {}
    for row in reversed(events):
        if not isinstance(row, dict):
            continue
        if str(row.get("event_type") or "") != EventType.ASSISTANT_REPLIED.value:
            continue
        payload = row.get("payload")
        if isinstance(payload, dict):
            return payload
    return {}


def _rendered_reply_text(
    *,
    harness: Any,
    detail: Dict[str, Any],
    raw_reply: str,
    assistant_route: str,
) -> str:
    task_status = str(detail.get("task", {}).get("status") or "")
    if assistant_route == "direct":
        return format_direct_message(raw_reply)
    if task_status in {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value}:
        try:
            return str(_render_telegram_final_message(harness, detail) or "")
        except Exception:
            return ""
    return ""


def _scenario_meta(detail: Dict[str, Any]) -> Dict[str, Any]:
    meta = detail.get("task", {}).get("meta", {})
    scen = meta.get("scenario") if isinstance(meta.get("scenario"), dict) else {}
    return scen if isinstance(scen, dict) else {}


def _execution_meta(detail: Dict[str, Any]) -> Dict[str, Any]:
    meta = detail.get("task", {}).get("meta", {})
    ex = meta.get("execution") if isinstance(meta.get("execution"), dict) else {}
    return ex if isinstance(ex, dict) else {}


def _meta_flags(detail: Dict[str, Any]) -> Dict[str, bool]:
    meta = detail.get("task", {}).get("meta", {})
    return {
        "has_cursor": bool(meta.get("cursor")),
        "has_openclaw": bool(meta.get("openclaw")),
        "delegated_to_cursor": bool(_execution_meta(detail).get("delegated_to_cursor")),
    }


def _projection_has_continuity_state(meta: Mapping[str, Any], summary: Any) -> bool:
    for key in (
        "goal",
        "plan",
        "approval",
        "daily_assistant_pack",
        "followthrough",
        "telegram",
        "proactive",
        "outcome",
        "execution",
        "assistant",
        "openclaw",
        "cursor",
    ):
        section = meta.get(key) if isinstance(meta, Mapping) else None
        if isinstance(section, dict) and section:
            return True
    return len(str(summary or "").strip()) > 8


def build_turn_capture(
    *,
    harness: Any,
    task_id: str,
    user_text: str,
    detail: Dict[str, Any],
) -> Dict[str, Any]:
    """Normalize reply + routing metadata for persistence in ExperienceCheckResult.metadata."""
    server = harness.server
    conn = harness.conn
    channel = get_task_channel(conn, task_id) or "telegram"
    proj = project_task_dict(conn, task_id, channel)
    meta = proj.get("meta") if isinstance(proj.get("meta"), dict) else {}
    scen = _scenario_meta(detail)
    scenario_id = str(scen.get("scenario_id") or "")
    resolution, _ = resolve_scenario(user_text, goal_id="", route_decision=None)
    projection_has_state = _projection_has_continuity_state(meta, proj.get("summary"))
    try:
        del_score = same_chat_max_delegation_score(conn, task_id)
    except (AttributeError, TypeError, ValueError):
        del_score = 0
    plan = build_turn_plan(
        user_text,
        scenario_id=scenario_id or resolution.scenario_id,
        projection_has_continuity_state=projection_has_state,
        same_chat_delegation_score=del_score,
    )
    try:
        cont_patch = resolve_semantic_continuity_patch(
            conn,
            task_id,
            user_text,
            scenario_id=scenario_id or resolution.scenario_id,
            base_focus=plan.continuity_focus,
            projection_has_continuity_state=projection_has_state,
        )
    except (AttributeError, TypeError, ValueError):
        from .semantic_continuity import SemanticContinuityPatch

        cont_patch = SemanticContinuityPatch()
    if cont_patch.continuity_focus_override is not None:
        plan = replace(plan, continuity_focus=cont_patch.continuity_focus_override)
    if cont_patch.force_prefer_state_reply and plan.domain in {"project_status", "approval_state"}:
        plan = replace(plan, prefer_state_reply=True)
    if cont_patch.stateful_allowed is False:
        plan = replace(
            plan,
            continuity_focus="none",
            prefer_state_reply=False,
            allow_goal_continuity_repair=False,
        )
    raw_reply = _task_last_reply(detail)
    sanitized = sanitize_user_surface_text(raw_reply, fallback="", limit=2000)
    family_profile = resolve_answer_family_profile(user_text, plan)
    expected_family = str(cont_patch.family_override or family_profile.family)
    expected_sources = list(cont_patch.allowed_sources_override or family_profile.allowed_sources)
    flags = _meta_flags(detail)
    assistant_payload = _latest_assistant_reply_payload(detail)
    semantic_selection = (
        assistant_payload.get("semantic_selection")
        if isinstance(assistant_payload.get("semantic_selection"), dict)
        else {}
    )
    semantic_contract = (
        semantic_selection.get("turn_contract")
        if isinstance(semantic_selection.get("turn_contract"), dict)
        else {}
    )
    assistant_route = _task_route(detail)
    rendered_reply = _rendered_reply_text(
        harness=harness,
        detail=detail,
        raw_reply=raw_reply,
        assistant_route=assistant_route,
    )
    rendered_sanitized = sanitize_user_surface_text(rendered_reply, fallback="", limit=4000)
    return {
        "user_turn": user_text,
        "raw_reply_text": raw_reply,
        "formatted_reply_text": sanitized,
        "rendered_reply_text": rendered_reply,
        "rendered_reply_sanitized": rendered_sanitized,
        "task_id": task_id,
        "task_status": str(detail.get("task", {}).get("status") or ""),
        "assistant_route": assistant_route,
        "assistant_reason": _task_assistant_reason(detail),
        "assistant_semantic_selection": semantic_selection,
        "semantic_turn_contract": semantic_contract,
        "scenario_id": scenario_id or resolution.scenario_id,
        "scenario_reason": str(scen.get("reason") or resolution.reason),
        "turn_plan_domain": plan.domain,
        "turn_plan_continuity_focus": plan.continuity_focus,
        "expected_answer_family": expected_family,
        "expected_answer_sources": expected_sources,
        "continuity_binding_reason": cont_patch.binding_reason,
        "continuity_stateful_allowed": cont_patch.stateful_allowed,
        "continuity_family_override": cont_patch.family_override,
        "continuity_allowed_sources_override": list(cont_patch.allowed_sources_override),
        "projection_has_continuity_state": projection_has_state,
        "delegated_to_cursor": flags["delegated_to_cursor"],
        "meta_cursor_present": flags["has_cursor"],
        "meta_openclaw_present": flags["has_openclaw"],
        "leak_echo_or_metadata_scaffold": draft_should_force_continuity_repair(raw_reply, user_text),
        "leak_internal_runtime": bool(raw_reply) and is_internal_runtime_text(raw_reply),
        "leak_sanitized_empty": bool(raw_reply) and not bool(sanitized),
    }


def run_deterministic_detectors(
    capture: Mapping[str, Any],
    *,
    prior_user_turn: str = "",
    expect_tool_carryover: bool = False,
    expect_cursor_substance: bool = False,
    expect_external_boundary: bool = False,
) -> List[Dict[str, Any]]:
    """Return list of findings: {family, issue_code, severity, detail}."""
    findings: List[Dict[str, Any]] = []
    rendered = str(capture.get("rendered_reply_sanitized") or "")
    text = rendered or str(capture.get("formatted_reply_text") or capture.get("raw_reply_text") or "")
    raw_text = str(capture.get("raw_reply_text") or "")
    user = str(capture.get("user_turn") or "")
    low = text.lower()
    expected_family = str(capture.get("expected_answer_family") or "")
    expected_sources = set(capture.get("expected_answer_sources") or [])
    semantic_selection = capture.get("assistant_semantic_selection")
    semantic_source = ""
    if isinstance(semantic_selection, dict):
        semantic_source = str(semantic_selection.get("source") or "")
    semantic_contract = capture.get("semantic_turn_contract")
    contract_family = ""
    contract_source = ""
    contract_allowed_sources: set[str] = set()
    contract_evidence_strength = 0
    if isinstance(semantic_contract, dict):
        contract_family = str(semantic_contract.get("family") or "")
        contract_source = str(semantic_contract.get("source") or "")
        raw_allowed = semantic_contract.get("allowed_sources")
        if isinstance(raw_allowed, list):
            contract_allowed_sources = {str(x) for x in raw_allowed if str(x)}
        try:
            contract_evidence_strength = int(semantic_contract.get("evidence_strength") or 0)
        except (TypeError, ValueError):
            contract_evidence_strength = 0

    if capture.get("leak_internal_runtime") or capture.get("leak_sanitized_empty"):
        findings.append(
            {
                "family": "metadata_surface_leak",
                "issue_code": "conversation_metadata_surface_leak",
                "severity": "high",
                "detail": "internal runtime or sanitization-stripped reply",
            }
        )
    if capture.get("leak_echo_or_metadata_scaffold"):
        if draft_should_force_continuity_repair(text, user):
            if user and user.strip().lower() in text.strip().lower():
                findings.append(
                    {
                        "family": "question_echo",
                        "issue_code": "conversation_question_echo",
                        "severity": "high",
                        "detail": "reply echoes user question or metadata scaffold",
                    }
                )
            else:
                findings.append(
                    {
                        "family": "metadata_surface_leak",
                        "issue_code": "conversation_metadata_scaffold",
                        "severity": "medium",
                        "detail": "metadata-heavy or echoey scaffold",
                    }
                )
    if text and is_generic_direct_reply(text):
        findings.append(
            {
                "family": "generic_fallback_leak",
                "issue_code": "conversation_generic_fallback_leak",
                "severity": "medium",
                "detail": "generic direct fallback phrasing",
            }
        )
    approval_inventory_only = bool(
        str(capture.get("turn_plan_domain") or "") == "approval_state"
        and (
            "approval requests waiting on you right now" in low
            or "approval requests waiting right now" in low
            or "no pending approvals right now" in low
        )
    )
    if (
        draft_implies_false_completion(raw_text)
        and any(k in low for k in ("blocked", "pending", "approval", "cursor", "task"))
        and not approval_inventory_only
    ):
        findings.append(
            {
                "family": "false_completion",
                "issue_code": "conversation_false_completion",
                "severity": "high",
                "detail": "false completion language with continuity cues",
            }
        )
    if any(p in low for p in MECHANICAL_PHRASES):
        findings.append(
            {
                "family": "overly_mechanical_wording",
                "issue_code": "conversation_mechanical_wording",
                "severity": "low",
                "detail": "templated or mechanical assistant phrasing",
            }
        )
    if expect_external_boundary and capture.get("turn_plan_domain") != "external_information":
        findings.append(
            {
                "family": "external_info_contamination",
                "issue_code": "conversation_external_domain_mismatch",
                "severity": "medium",
                "detail": "expected external_information domain",
            }
        )
    if (
        str(capture.get("assistant_reason") or "").startswith("semantic_state_")
        and str(capture.get("turn_plan_domain") or "")
        not in {"project_status", "approval_state"}
    ):
        findings.append(
            {
                "family": "wrong_domain_contamination",
                "issue_code": "conversation_stateful_domain_hijack",
                "severity": "high",
                "detail": "stateful semantic answer used outside project/approval domain",
            }
        )
    if (
        str(capture.get("assistant_reason") or "") == "goal_runtime_status"
        and str(capture.get("turn_plan_domain") or "") in {"opinion_reflection", "casual_conversation"}
    ):
        findings.append(
            {
                "family": "wrong_domain_contamination",
                "issue_code": "conversation_stateful_domain_hijack",
                "severity": "high",
                "detail": "goal-runtime status reply used on non-stateful opinion/casual turn",
            }
        )
    if expected_sources and semantic_source and semantic_source not in expected_sources:
        findings.append(
            {
                "family": "wrong_domain_contamination",
                "issue_code": "conversation_semantic_source_family_mismatch",
                "severity": "high",
                "detail": f"semantic source {semantic_source!r} not in expected family sources {tuple(expected_sources)!r}",
            }
        )
    if semantic_source and not isinstance(semantic_contract, dict):
        findings.append(
            {
                "family": "wrong_domain_contamination",
                "issue_code": "conversation_semantic_contract_missing",
                "severity": "high",
                "detail": "semantic selection missing persisted turn contract metadata",
            }
        )
    if contract_family and expected_family and contract_family != expected_family:
        findings.append(
            {
                "family": "wrong_domain_contamination",
                "issue_code": "conversation_semantic_contract_family_mismatch",
                "severity": "high",
                "detail": f"contract family {contract_family!r} does not match expected {expected_family!r}",
            }
        )
    if contract_source and semantic_source and contract_source != semantic_source:
        findings.append(
            {
                "family": "wrong_domain_contamination",
                "issue_code": "conversation_semantic_contract_source_mismatch",
                "severity": "high",
                "detail": f"contract source {contract_source!r} differs from semantic source {semantic_source!r}",
            }
        )
    if semantic_source and contract_allowed_sources and semantic_source not in contract_allowed_sources:
        findings.append(
            {
                "family": "wrong_domain_contamination",
                "issue_code": "conversation_semantic_contract_allowed_source_mismatch",
                "severity": "high",
                "detail": f"semantic source {semantic_source!r} not admitted by contract sources {tuple(contract_allowed_sources)!r}",
            }
        )
    if is_cursor_thread_recall_question(user) and is_continuation_fallback_family_text(text):
        findings.append(
            {
                "family": "cursor_recall_failure",
                "issue_code": "conversation_cursor_recall_continuation_family_leak",
                "severity": "high",
                "detail": "cursor recall ask answered with continuation / no-active-work fallback phrasing",
            }
        )
    if is_strict_cursor_domain_recall_question(user) and _cursor_recall_output_should_force_clean_fallback(text):
        findings.append(
            {
                "family": "cursor_recall_failure",
                "issue_code": "conversation_cursor_recall_approval_domain_contamination",
                "severity": "high",
                "detail": "cursor recall reply contains approval or status-followup scaffolding",
            }
        )
    if (
        expected_family == "cursor_recall"
        and semantic_source == "cursor_continuity_recall"
        and "not finding a recent clean cursor result" in low
        and (
            bool(capture.get("delegated_to_cursor"))
            or bool(capture.get("meta_cursor_present"))
            or bool(capture.get("meta_openclaw_present"))
        )
    ):
        findings.append(
            {
                "family": "cursor_recall_failure",
                "issue_code": "conversation_cursor_recall_fallback_under_evidence",
                "severity": "high",
                "detail": "cursor recall returned clean fallback despite cursor/openclaw evidence markers",
            }
        )
    if contract_evidence_strength >= 5 and _looks_fallback_shaped_reply(text):
        findings.append(
            {
                "family": "thin_summary",
                "issue_code": "conversation_fallback_shaped_under_contract_evidence",
                "severity": "high",
                "detail": "rendered answer stayed fallback-shaped despite strong contract evidence",
            }
        )
    if expect_cursor_substance and is_cursor_thread_recall_question(user):
        if any(s in low for s in CURSOR_GRACE_SNIPPETS):
            findings.append(
                {
                    "family": "cursor_recall_failure",
                    "issue_code": "conversation_cursor_recall_thin",
                    "severity": "medium",
                    "detail": "cursor recall ask met with grace fallback copy",
                }
            )
        elif _cursor_recall_composition_is_metadata_led(text):
            findings.append(
                {
                    "family": "cursor_recall_failure",
                    "issue_code": "conversation_cursor_recall_metadata_led",
                    "severity": "medium",
                    "detail": "cursor recall reply leads with execution/status scaffolding instead of recap",
                }
            )
        elif len(text.strip()) < 100 and is_generic_direct_reply(text):
            findings.append(
                {
                    "family": "cursor_recall_failure",
                    "issue_code": "conversation_cursor_recall_thin",
                    "severity": "medium",
                    "detail": "cursor recall ask met with generic short direct reply",
                }
            )
    if "cursor recap: cursor recap:" in low:
        findings.append(
            {
                "family": "thin_summary",
                "issue_code": "conversation_recap_recursion",
                "severity": "medium",
                "detail": "recursive recap label duplication",
            }
        )
    if expect_cursor_substance and is_cursor_thread_recall_question(user):
        head = text[:1200].lower()
        _openclaw_source_markers = (
            "latest useful result:",
            "phase synthesis:",
            "phase execution:",
            "phase critique:",
            "phase plan:",
            "collaboration note:",
            "recent receipt",
        )
        has_openclaw_source = any(tok in head for tok in _openclaw_source_markers)
        derived_lead = ("last assistant update on this task:" in head) or (
            "recorded summary:" in head and "cursor recap:" in head
        )
        if derived_lead and not has_openclaw_source:
            findings.append(
                {
                    "family": "cursor_recall_failure",
                    "issue_code": "conversation_cursor_recall_derived_surface_led",
                    "severity": "medium",
                    "detail": "cursor recall led with assistant/projection scaffolding without source-truth openclaw lines",
                }
            )
    if expect_tool_carryover and prior_user_turn:
        topic_tokens = [w for w in re.split(r"\W+", prior_user_turn.lower()) if len(w) > 4][:6]
        hits = sum(1 for w in topic_tokens if w and w in low)
        prior_l = prior_user_turn.lower()
        user_l = (user or "").lower()
        assistant_reason = str(capture.get("assistant_reason") or "").strip()
        messaging_lane_prior = bool(_TOOL_TEXT_LANE_PRIOR_RE.search(prior_l))
        summarize_follow = bool(_TEXT_SUMMARIZE_FOLLOWUP_RE.search(user_l))
        if messaging_lane_prior and summarize_follow:
            if (
                _OUTBOUND_ONLY_CAPABILITY_BOILERPLATE_RE.search(low)
                and "read" not in low
                and "retriev" not in low
            ):
                findings.append(
                    {
                        "family": "followup_carryover_failure",
                        "issue_code": "conversation_messaging_read_send_capability_mismatch",
                        "severity": "high",
                        "detail": "read/summarize follow-up met with outbound-only capability copy",
                    }
                )
            elif (
                assistant_reason not in _TEXT_LANE_ASSISTANT_REASONS
                and not _TEXT_LANE_REPLY_MARKERS_RE.search(low)
                and len(text) > 0
            ):
                findings.append(
                    {
                        "family": "followup_carryover_failure",
                        "issue_code": "conversation_followup_carryover_miss",
                        "severity": "medium",
                        "detail": "text-lane summarize follow-up missing messaging-domain reply cues",
                    }
                )
        elif hits < 1 and len(text) > 0:
            findings.append(
                {
                    "family": "followup_carryover_failure",
                    "issue_code": "conversation_followup_carryover_miss",
                    "severity": "medium",
                    "detail": "follow-up did not carry topic from prior turn",
                }
            )
    if "continue" in user.lower() and "cursor" in user.lower() and "cursor" not in low and len(text) < 80:
        findings.append(
            {
                "family": "cursor_continuation_failure",
                "issue_code": "conversation_cursor_continuation_thin",
                "severity": "medium",
                "detail": "cursor continuation ask with thin reply",
            }
        )
    return findings


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _openai_json_chat(
    *,
    system: str,
    user: str,
    model: str,
    timeout_seconds: int = 45,
) -> Dict[str, Any]:
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("openai_missing_key")
    payload: Dict[str, Any] = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 500,
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
        raise RuntimeError(f"openai_http_{err.code}:{raw[:240]}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"openai_transport:{err}") from err
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("openai_no_choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    content = str(message.get("content") or "").strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"openai_json_decode:{_clip(content, 200)}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("openai_json_not_object")
    return parsed


def run_llm_evaluator(
    capture: Mapping[str, Any],
    *,
    weak_or_failed: bool,
    force: bool = False,
) -> Dict[str, Any] | None:
    """Post-hoc quality JSON; verifier-class model slot. Returns None if skipped/disabled."""
    if (
        not force
        and not _env_truthy("ANDREA_CONVERSATION_EVAL_LLM", False)
        and not weak_or_failed
    ):
        return None
    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        return None
    model = model_for_role("verifier")
    system = (
        "You are a strict conversation QA judge for assistant Andrea. "
        "Return ONLY a JSON object with keys: "
        "satisfied_request (bool), right_domain (bool), useful (bool), too_thin (bool), "
        "too_mechanical (bool), contaminated_memory (bool), notes (string). "
        "Be conservative: if unsure, set booleans false except too_thin/too_mechanical."
    )
    user = json.dumps(
        {
            "user": capture.get("user_turn"),
            "assistant_reply": capture.get("rendered_reply_sanitized") or capture.get("raw_reply_text"),
            "route": capture.get("assistant_route"),
            "reason": capture.get("assistant_reason"),
            "scenario_id": capture.get("scenario_id"),
            "turn_domain": capture.get("turn_plan_domain"),
            "continuity_focus": capture.get("turn_plan_continuity_focus"),
            "semantic_turn_contract": capture.get("semantic_turn_contract"),
        },
        ensure_ascii=False,
    )
    try:
        return {"model": model, "verdict": _openai_json_chat(system=system, user=user, model=model)}
    except RuntimeError as exc:
        return {"error": str(exc), "model": model}


def run_semantic_adjudicator(
    *,
    user_text: str,
    scenario_id: str,
    turn_domain: str,
    continuity_focus: str,
    confidence: float,
    history_tail: Sequence[Mapping[str, str]] | None = None,
) -> Dict[str, Any] | None:
    """Narrow routing adjudication JSON; router-class slot."""
    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        return None
    model = model_for_role("router")
    hist = [
        {"role": str(h.get("role") or ""), "content": _clip(h.get("content"), 400)}
        for h in (history_tail or [])
        if str(h.get("content") or "").strip()
    ][-6:]
    system = (
        "You resolve ambiguous assistant routing for Andrea. Return ONLY JSON with keys: "
        "next_action (one of: direct_reply, clarify, delegate), "
        "domain_family (one of: casual_conversation, personal_agenda, attention_today, "
        "external_information, project_status, approval_state, technical_execution, other), "
        "continuity_family (one of: none, blocked_state, recent_outcome_history, "
        "cursor_followup_heavy_lift), "
        "reuse_recent_delegation (bool), "
        "clarify_question (string, empty unless next_action is clarify), "
        "confidence (number 0-1), rationale (string <= 200 chars)."
    )
    user = json.dumps(
        {
            "user_text": user_text,
            "resolved_scenario_id": scenario_id,
            "turn_domain_hint": turn_domain,
            "continuity_focus_hint": continuity_focus,
            "scenario_confidence": confidence,
            "recent_turns": hist,
        },
        ensure_ascii=False,
    )
    try:
        return {"model": model, "adjudication": _openai_json_chat(system=system, user=user, model=model)}
    except RuntimeError as exc:
        return {"error": str(exc), "model": model}


def runtime_adjudication_enabled() -> bool:
    return _env_truthy("ANDREA_RUNTIME_SEMANTIC_ADJUDICATOR", False)


def runtime_adjudication_gate(
    *,
    user_text: str,
    scenario_confidence: float,
    scenario_id: str,
    force_delegate: bool,
) -> bool:
    if not runtime_adjudication_enabled():
        return False
    if force_delegate:
        return False
    if scenario_confidence > 0.52:
        return False
    clean = str(user_text or "").strip()
    if len(clean) > 280:
        return False
    if scenario_id not in {"statusFollowupContinue", "goalContinuationAcrossSessions", "researchSummary"}:
        # default / fuzzy classifications only
        if scenario_confidence > 0.35:
            return False
    short = clean.lower()
    if not re.search(r"\b(that|this|it|those|these|there|one)\b", short) and "?" not in clean:
        return False
    return True


@dataclass(frozen=True)
class ConversationCaseSpec:
    case_id: str
    title: str
    behavior_family: str
    turns: tuple[str, ...]
    chat_id: int
    from_id: int
    first_update_id: int
    first_message_id: int
    patch_openclaw_news: bool = False
    patch_bluebubbles: bool = False
    expect_external_domain: bool = False
    expect_cursor_substance: bool = False
    expect_tool_carryover: bool = False
    setup_fn: Callable[[Any], None] | None = None
    turn_payload_overrides: tuple[Mapping[str, Any], ...] = ()
    required_reply_markers: tuple[str, ...] = ()
    forbidden_reply_markers: tuple[str, ...] = ()
    required_assistant_reasons: tuple[str, ...] = ()
    required_turn_domains: tuple[str, ...] = ()
    required_continuity_focuses: tuple[str, ...] = ()
    # terminal_reply: wait for completed/failed (meaningful capture). routing_smoke: allow queued/running.
    wait_policy: str = "terminal_reply"


def _wait_statuses_for_policy(wait_policy: str) -> tuple[str, ...]:
    pol = str(wait_policy or "terminal_reply").strip().lower()
    if pol == "routing_smoke":
        return (
            TaskStatus.COMPLETED.value,
            TaskStatus.FAILED.value,
            TaskStatus.QUEUED.value,
            TaskStatus.RUNNING.value,
        )
    return (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)


# Representative subset for fast harness health checks (real webhook + local correlation).
CONVERSATION_SMOKE_CASE_IDS: tuple[str, ...] = (
    "hi_andrea",
    "how_is_it_going",
    "news_today",
    "cursor_said",
    "continue_cursor",
    "bluebubbles_then_summarize",
)


def _openclaw_news_patch(server: Any) -> Any:
    return mock.patch.object(
        server,
        "_create_openclaw_job",
        return_value={
            "ok": True,
            "user_summary": (
                "Live news: AI policy and markets moved; regional headlines varied by outlet."
            ),
        },
    )


def _openclaw_generic_patch(server: Any, summary: str) -> Any:
    return mock.patch.object(
        server,
        "_create_openclaw_job",
        return_value={"ok": True, "user_summary": summary},
    )


def _bluebubbles_patch(server: Any) -> Any:
    return mock.patch.object(
        server,
        "_resolve_messaging_capability",
        return_value={
            "skill_key": "bluebubbles",
            "label": "text messaging",
            "truth": {"status": "verified_available"},
        },
    )


def _resolve_skill_patch(server: Any) -> Any:
    return mock.patch.object(
        server,
        "_resolve_runtime_skill",
        return_value={"skill_key": "brave-api-search", "truth": {"status": "verified_available"}},
    )


def _merge_turn_message_payload(
    base_message: Dict[str, Any],
    *,
    override: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    msg = dict(base_message)
    if not isinstance(override, Mapping):
        return msg
    for key, value in override.items():
        if key == "reply_to_message_id":
            if value in (None, ""):
                continue
            msg["reply_to_message"] = {"message_id": int(value)}
            continue
        if key in {"chat_id", "chat"}:
            if isinstance(value, Mapping):
                existing = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
                merged = dict(existing)
                merged.update(value)
                msg["chat"] = merged
            else:
                msg["chat"] = {"id": value}
            continue
        if key in {"from_id", "from"}:
            if isinstance(value, Mapping):
                existing = msg.get("from") if isinstance(msg.get("from"), dict) else {}
                merged = dict(existing)
                merged.update(value)
                msg["from"] = merged
            else:
                msg["from"] = {"id": value}
            continue
        msg[key] = value
    return msg


def _seed_multitask_recall_thread_state(harness: Any) -> None:
    server = harness.server
    assert server is not None

    def _cmd(body: Dict[str, Any]) -> Dict[str, Any]:
        return server.with_lock(lambda c: handle_command(c, body))

    # Two same-chat tasks with distinct thread and summary markers.
    payloads = (
        ("seed-thread-a", 77781, 6001, 501, "THREAD_ALPHA_UNIQUE_MARKER"),
        ("seed-thread-b", 77781, 6002, 502, "THREAD_BETA_UNIQUE_MARKER"),
    )
    for ext, chat_id, message_id, thread_id, marker in payloads:
        created = _cmd(
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": ext,
                "payload": {
                    "text": f"Seed {marker}",
                    "routing_text": f"Seed {marker}",
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "from_user": 99090,
                    "message_thread_id": thread_id,
                },
            }
        )
        task_id = str(created.get("task_id") or "")
        if not task_id:
            continue
        _cmd(
            {
                "command_type": CommandType.REPORT_CURSOR_EVENT.value,
                "channel": "cursor",
                "task_id": task_id,
                "payload": {
                    "event_type": EventType.JOB_COMPLETED.value,
                    "payload": {
                        "backend": "openclaw",
                        "runner": "openclaw",
                        "summary": f"Cursor recap: {marker} completed work.",
                        "user_summary": f"{marker} completed work.",
                    },
                },
            }
        )


def _seed_identity_hijack_state(harness: Any) -> None:
    server = harness.server
    assert server is not None

    created = server.with_lock(
        lambda c: handle_command(
            c,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "seed-identity-heavy",
                "payload": {
                    "text": "Seed heavy status context",
                    "routing_text": "Seed heavy status context",
                    "chat_id": 77783,
                    "message_id": 6101,
                    "from_user": 99091,
                },
            },
        )
    )
    task_id = str(created.get("task_id") or "")
    if not task_id:
        return
    server.with_lock(
        lambda c: handle_command(
            c,
            {
                "command_type": CommandType.REPORT_CURSOR_EVENT.value,
                "channel": "cursor",
                "task_id": task_id,
                "payload": {
                    "event_type": EventType.JOB_COMPLETED.value,
                    "payload": {
                        "backend": "openclaw",
                        "runner": "openclaw",
                        "summary": "Seeded continuity context for identity check.",
                        "user_summary": "Seeded continuity context for identity check.",
                    },
                },
            },
        )
    )


def _seed_source_truth_over_derived_recall(harness: Any) -> None:
    server = harness.server
    assert server is not None

    def _cmd(body: Dict[str, Any]) -> Dict[str, Any]:
        return server.with_lock(lambda c: handle_command(c, body))

    created = _cmd(
        {
            "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
            "channel": "telegram",
            "external_id": "seed-source-truth-derived",
            "payload": {
                "text": "Seed source truth vs derived",
                "routing_text": "Seed source truth vs derived",
                "chat_id": 77788,
                "message_id": 6301,
                "from_user": 99093,
            },
        }
    )
    task_id = str(created.get("task_id") or "")
    if not task_id:
        return
    _cmd(
        {
            "command_type": CommandType.REPORT_CURSOR_EVENT.value,
            "channel": "cursor",
            "task_id": task_id,
            "payload": {
                "event_type": EventType.JOB_COMPLETED.value,
                "payload": {
                    "backend": "openclaw",
                    "runner": "openclaw",
                    "summary": (
                        "Cursor recap: DERIVED_ASSISTANT_RECYCLED_SURFACE_XX "
                        "last assistant update noise."
                    ),
                    "user_summary": "SOURCE_TRUTH_UNIQUE_OPENCLAW_FACT_Z9 anchor for recall lead.",
                },
            },
        }
    )


def _seed_source_truth_rich_recall(harness: Any) -> None:
    server = harness.server
    assert server is not None

    def _cmd(body: Dict[str, Any]) -> Dict[str, Any]:
        return server.with_lock(lambda c: handle_command(c, body))

    created = _cmd(
        {
            "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
            "channel": "telegram",
            "external_id": "seed-source-truth-rich-recall",
            "payload": {
                "text": "Seed rich source truth recall",
                "routing_text": "Seed rich source truth recall",
                "chat_id": 77794,
                "message_id": 6721,
                "from_user": 99099,
            },
        }
    )
    task_id = str(created.get("task_id") or "")
    if not task_id:
        return
    _cmd(
        {
            "command_type": CommandType.REPORT_CURSOR_EVENT.value,
            "channel": "cursor",
            "task_id": task_id,
            "payload": {
                "event_type": EventType.JOB_COMPLETED.value,
                "payload": {
                    "backend": "openclaw",
                    "runner": "openclaw",
                    "summary": "Cursor recap: finalized API cleanup and regression checks.",
                    "user_summary": "RICH_RECALL_GROUNDED_FACT_42 API cleanup and regressions verified.",
                    "delegated_to_cursor": True,
                },
            },
        }
    )


def _seed_recall_rejects_continuation_assistant_surface(harness: Any) -> None:
    """OpenClaw source truth present while assistant.last_reply holds continuation fallback copy."""
    server = harness.server
    assert server is not None

    created = server.with_lock(
        lambda c: handle_command(
            c,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "seed-recall-cont-leak",
                "payload": {
                    "text": "Seed recall continuation boundary",
                    "routing_text": "Seed recall continuation boundary",
                    "chat_id": 77790,
                    "message_id": 6501,
                    "from_user": 99095,
                },
            },
        )
    )
    task_id = str(created.get("task_id") or "")
    if not task_id:
        return
    server.with_lock(
        lambda c: handle_command(
            c,
            {
                "command_type": CommandType.REPORT_CURSOR_EVENT.value,
                "channel": "cursor",
                "task_id": task_id,
                "payload": {
                    "event_type": EventType.JOB_COMPLETED.value,
                    "payload": {
                        "backend": "openclaw",
                        "runner": "openclaw",
                        "summary": "done",
                        "delegated_to_cursor": True,
                        "user_summary": (
                            "RECALL_SOURCE_TRUTH_BOUNDARY_77 Cursor actually shipped the auth hardening."
                        ),
                    },
                },
            },
        )
    )
    cont_fallback = (
        "I’m not finding a recent Cursor workstream with enough context to safely continue, "
        "so I’d need to start a new one from your latest instruction."
    )
    server.with_lock(
        lambda c: append_event(
            c,
            task_id,
            EventType.ASSISTANT_REPLIED,
            {"text": cont_fallback, "route": "direct", "reason": "seed_continuation_fallback"},
        )
    )


def _seed_recall_rejects_approval_status_sludge(harness: Any) -> None:
    """
    Task with approval-shaped assistant.last_reply plus status_followup receipt but no hard
    Cursor/delegated markers — explicit recall must not echo approval inventory.
    """
    server = harness.server
    assert server is not None

    created = server.with_lock(
        lambda c: handle_command(
            c,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "seed-recall-approval-sludge",
                "payload": {
                    "text": "Seed approval sludge recall",
                    "routing_text": "Seed approval sludge recall",
                    "chat_id": 77792,
                    "message_id": 6701,
                    "from_user": 99097,
                },
            },
        )
    )
    task_id = str(created.get("task_id") or "")
    if not task_id:
        return
    approval_reply = (
        "I'm not seeing any approval requests waiting on you right now. "
        "If you expected one, tell me which goal and I'll re-check."
    )
    server.with_lock(
        lambda c: append_event(
            c,
            task_id,
            EventType.ASSISTANT_REPLIED,
            {
                "text": approval_reply,
                "route": "direct",
                "reason": "seed_goal_runtime_status",
            },
        )
    )
    server.with_lock(
        lambda c: insert_user_outcome_receipt(
            c,
            receipt_id="seed-receipt-approval-sludge-1",
            task_id=task_id,
            receipt_kind="status_followup",
            summary="Status / follow-up reply (goal_runtime_status).",
            proof_refs={"reason": "goal_runtime_status"},
        )
    )


def _seed_recall_rejects_approval_status_sludge_thread(harness: Any) -> None:
    """Same sludge pattern as _seed_recall_rejects_approval_status_sludge for thread-shaped recall."""
    server = harness.server
    assert server is not None

    created = server.with_lock(
        lambda c: handle_command(
            c,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "seed-recall-approval-sludge-thread",
                "payload": {
                    "text": "Seed approval sludge thread recall",
                    "routing_text": "Seed approval sludge thread recall",
                    "chat_id": 77793,
                    "message_id": 6710,
                    "from_user": 99098,
                },
            },
        )
    )
    task_id = str(created.get("task_id") or "")
    if not task_id:
        return
    approval_reply = (
        "I'm not seeing any approval requests waiting on you right now. "
        "If you expected one, tell me which goal and I'll re-check."
    )
    server.with_lock(
        lambda c: append_event(
            c,
            task_id,
            EventType.ASSISTANT_REPLIED,
            {
                "text": approval_reply,
                "route": "direct",
                "reason": "seed_goal_runtime_status",
            },
        )
    )
    server.with_lock(
        lambda c: insert_user_outcome_receipt(
            c,
            receipt_id="seed-receipt-approval-sludge-thread-1",
            task_id=task_id,
            receipt_kind="status_followup",
            summary="Status / follow-up reply (goal_runtime_status).",
            proof_refs={"reason": "goal_runtime_status"},
        )
    )


def _seed_pending_approval_inventory(harness: Any) -> None:
    """Create an active goal + pending approval for the same Telegram principal."""
    server = harness.server
    assert server is not None
    created = server.with_lock(
        lambda c: handle_command(
            c,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "seed-approval-pending",
                "payload": {
                    "text": "Seed pending approval inventory",
                    "routing_text": "Seed pending approval inventory",
                    "chat_id": 88028,
                    "message_id": 28999,
                    "from_user": 99028,
                },
            },
        )
    )
    task_id = str(created.get("task_id") or "")
    if not task_id:
        return
    principal_id = server.with_lock(lambda c: get_task_principal_id(c, task_id))
    if not principal_id:
        return
    server.with_lock(
        lambda c: (
            lambda gid: (
                link_task_to_goal(c, task_id, gid),
                create_goal_approval(
                    c,
                    gid,
                    task_id,
                    rationale="Confirm deploy window before shipping to production.",
                ),
            )
        )(
            create_goal(
                c,
                principal_id=principal_id,
                summary="Seed approval goal",
                channel="telegram",
            )
        )
    )


def _seed_continue_prefers_fresher_workstream(harness: Any) -> None:
    """Older verbose delegated task vs newer source-rich workstream on the same chat."""
    server = harness.server
    assert server is not None

    def _cmd(body: Dict[str, Any]) -> Dict[str, Any]:
        return server.with_lock(lambda c: handle_command(c, body))

    old = _cmd(
        {
            "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
            "channel": "telegram",
            "external_id": "seed-fresh-ws-old",
            "payload": {
                "text": "Seed older workstream",
                "routing_text": "Seed older workstream",
                "chat_id": 77791,
                "message_id": 6601,
                "from_user": 99096,
            },
        }
    )
    old_id = str(old.get("task_id") or "")
    if old_id:
        _cmd(
            {
                "command_type": CommandType.REPORT_CURSOR_EVENT.value,
                "channel": "cursor",
                "task_id": old_id,
                "payload": {
                    "event_type": EventType.JOB_COMPLETED.value,
                    "payload": {
                        "backend": "openclaw",
                        "runner": "openclaw",
                        "summary": "done",
                        "delegated_to_cursor": True,
                        "user_summary": (
                            "STALE_VERBOSE_CONTINUE_MARKER_OLD delegated the task to a new "
                            "multi-agent handoff session for the sprint."
                        ),
                    },
                },
            }
        )
    _cmd(
        {
            "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
            "channel": "telegram",
            "external_id": "seed-fresh-ws-new",
            "payload": {
                "text": "hey",
                "routing_text": "hey",
                "chat_id": 77791,
                "message_id": 6602,
                "from_user": 99096,
            },
        }
    )
    new = _cmd(
        {
            "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
            "channel": "telegram",
            "external_id": "seed-fresh-ws-new2",
            "payload": {
                "text": "new shell",
                "routing_text": "new shell",
                "chat_id": 77791,
                "message_id": 6603,
                "from_user": 99096,
            },
        }
    )
    new_id = str(new.get("task_id") or "")
    if new_id:
        _cmd(
            {
                "command_type": CommandType.REPORT_CURSOR_EVENT.value,
                "channel": "cursor",
                "task_id": new_id,
                "payload": {
                    "event_type": EventType.JOB_COMPLETED.value,
                    "payload": {
                        "backend": "openclaw",
                        "runner": "openclaw",
                        "summary": "done",
                        "delegated_to_cursor": True,
                        "user_summary": (
                            "FRESHER_CONTINUE_WORKSTREAM_MARKER_V3 landed the payment retry fix."
                        ),
                    },
                },
            }
        )


def _seed_bare_continue_rich_neighbor(harness: Any) -> None:
    server = harness.server
    assert server is not None

    def _cmd(body: Dict[str, Any]) -> Dict[str, Any]:
        return server.with_lock(lambda c: handle_command(c, body))

    rich = _cmd(
        {
            "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
            "channel": "telegram",
            "external_id": "seed-rich-neighbor-a",
            "payload": {
                "text": "Seed rich neighbor A",
                "routing_text": "Seed rich neighbor A",
                "chat_id": 77789,
                "message_id": 6401,
                "from_user": 99094,
            },
        }
    )
    rid = str(rich.get("task_id") or "")
    if rid:
        _cmd(
            {
                "command_type": CommandType.REPORT_CURSOR_EVENT.value,
                "channel": "cursor",
                "task_id": rid,
                "payload": {
                    "event_type": EventType.JOB_COMPLETED.value,
                    "payload": {
                        "backend": "openclaw",
                        "runner": "openclaw",
                        "summary": "Prior delegated summary.",
                        "user_summary": "BARE_CONTINUE_RICH_NEIGHBOR_MARKER_Q5 solid recap anchor.",
                    },
                },
            }
        )
    _cmd(
        {
            "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
            "channel": "telegram",
            "external_id": "seed-thin-neighbor-b",
            "payload": {
                "text": "hey",
                "routing_text": "hey",
                "chat_id": 77789,
                "message_id": 6402,
                "from_user": 99094,
            },
        }
    )


def _seed_recap_recursion_state(harness: Any) -> None:
    server = harness.server
    assert server is not None
    created = server.with_lock(
        lambda c: handle_command(
            c,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "seed-recap-recursion",
                "payload": {
                    "text": "Seed recap recursion",
                    "routing_text": "Seed recap recursion",
                    "chat_id": 77784,
                    "message_id": 6201,
                    "from_user": 99092,
                },
            },
        )
    )
    task_id = str(created.get("task_id") or "")
    if not task_id:
        return
    server.with_lock(
        lambda c: handle_command(
            c,
            {
                "command_type": CommandType.REPORT_CURSOR_EVENT.value,
                "channel": "cursor",
                "task_id": task_id,
                "payload": {
                    "event_type": EventType.JOB_COMPLETED.value,
                    "payload": {
                        "backend": "openclaw",
                        "runner": "openclaw",
                        "summary": "Cursor recap: Cursor recap: resolved the issue cleanly.",
                        "user_summary": "Cursor recap: Cursor recap: resolved the issue cleanly.",
                    },
                },
            },
        )
    )


def run_conversation_case(
    harness: Any,
    scenario: ExperienceScenario,
    case: ConversationCaseSpec,
) -> ExperienceCheckResult:
    """Execute multi-turn case via real telegram webhook path; score with detectors (+ optional LLM)."""
    started = time.time()
    server = harness.server
    assert server is not None
    options: Dict[str, Any] = {}
    if isinstance(scenario.metadata, dict):
        options = dict(scenario.metadata.get("conversation_eval_options") or {})

    progress_on = bool(options.get("progress", experience_progress_enabled()))
    wait_statuses = _wait_statuses_for_policy(case.wait_policy)

    def _log_progress(phase: str, payload: Dict[str, Any]) -> None:
        if not progress_on:
            return
        base = {
            "scenario": scenario.scenario_id,
            "case": case.case_id,
            **payload,
        }
        print(f"[conversation_harness] {phase} {json.dumps(base, ensure_ascii=False)}", flush=True)

    patches: List[Any] = []
    if case.patch_openclaw_news:
        patches.append(_openclaw_news_patch(server))
    if case.patch_bluebubbles:
        patches.append(_bluebubbles_patch(server))
        patches.append(
            _openclaw_generic_patch(
                server,
                "Recent texts: Alex asked about dinner; Sam shared a link about travel.",
            )
        )
    if case.expect_external_domain and not case.patch_openclaw_news:
        patches.append(_resolve_skill_patch(server))

    captures: List[Dict[str, Any]] = []
    harness_timings: List[Dict[str, Any]] = []
    try:
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            if case.setup_fn is not None:
                case.setup_fn(harness)
            uid = case.first_update_id
            mid = case.first_message_id
            prior_user = ""
            for turn_idx, text in enumerate(case.turns):
                t_submit_start = time.time()
                _log_progress(
                    "turn_start",
                    {
                        "turn_index": turn_idx,
                        "message_id": mid + turn_idx,
                        "chat_id": case.chat_id,
                        "wait_policy": case.wait_policy,
                    },
                )
                try:
                    override = (
                        case.turn_payload_overrides[turn_idx]
                        if turn_idx < len(case.turn_payload_overrides)
                        else {}
                    )
                    message = _merge_turn_message_payload(
                        {
                            "text": text,
                            "message_id": mid + turn_idx,
                            "chat": {"id": case.chat_id},
                            "from": {"id": case.from_id},
                        },
                        override=override,
                    )
                    harness.submit_telegram_update({"update_id": uid + turn_idx, "message": message})
                except HarnessInfraError as exc:
                    raise HarnessInfraError(
                        exc.issue_code,
                        str(exc),
                        phase="submit",
                        metadata={
                            **exc.metadata,
                            "turn_index": turn_idx,
                            "scenario_id": scenario.scenario_id,
                            "case_id": case.case_id,
                        },
                    ) from exc
                submit_ms = round((time.time() - t_submit_start) * 1000.0, 2)
                _log_progress(
                    "submitted",
                    {"turn_index": turn_idx, "submit_ms": submit_ms},
                )

                try:
                    detail = harness.wait_for_telegram_task(
                        chat_id=case.chat_id,
                        message_id=mid + turn_idx,
                        update_id=uid + turn_idx,
                        statuses=wait_statuses,
                        on_progress=_log_progress,
                    )
                except HarnessInfraError as exc:
                    raise HarnessInfraError(
                        exc.issue_code,
                        str(exc),
                        phase=exc.phase,
                        metadata={
                            **exc.metadata,
                            "turn_index": turn_idx,
                            "scenario_id": scenario.scenario_id,
                            "case_id": case.case_id,
                            "submit_ms": submit_ms,
                        },
                    ) from exc

                ht = detail.get("_harness_timing") if isinstance(detail.get("_harness_timing"), dict) else {}
                harness_timings.append(
                    {
                        "turn_index": turn_idx,
                        "submit_ms": submit_ms,
                        **ht,
                    }
                )
                detail_for_cap = dict(detail)
                detail_for_cap.pop("_harness_timing", None)
                task_id = str(detail_for_cap.get("task", {}).get("task_id") or "")
                cap = build_turn_capture(
                    harness=harness, task_id=task_id, user_text=text, detail=detail_for_cap
                )
                cap["harness_timing"] = harness_timings[-1]
                captures.append(cap)
                findings = run_deterministic_detectors(
                    cap,
                    prior_user_turn=prior_user if turn_idx > 0 else "",
                    expect_tool_carryover=case.expect_tool_carryover and turn_idx > 0,
                    expect_cursor_substance=case.expect_cursor_substance,
                    expect_external_boundary=case.expect_external_domain,
                )
                cap["deterministic_findings"] = findings
                prior_user = text
                last_status = str(detail.get("task", {}).get("status") or "")
                if case.wait_policy == "terminal_reply" and last_status not in (
                    TaskStatus.COMPLETED.value,
                    TaskStatus.FAILED.value,
                ):
                    return ExperienceCheckResult.from_observations(
                        scenario,
                        [
                            ExperienceObservation(
                                description="harness capture waits for terminal task status",
                                expected="completed or failed for terminal_reply policy",
                                observed=f"status={last_status!r} (turn {turn_idx})",
                                passed=False,
                                issue_code="harness_capture_incomplete",
                                severity="high",
                            )
                        ],
                        output_excerpt=text,
                        metadata={
                            "conversation_case_id": case.case_id,
                            "harness_infra": True,
                            "harness_timings": harness_timings,
                            "captures": captures,
                            "last_task_status": last_status,
                        },
                        started_at=started,
                        completed_at=time.time(),
                    )
    except HarnessInfraError as exc:
        return ExperienceCheckResult.from_observations(
            scenario,
            [
                ExperienceObservation(
                    description="conversation harness infrastructure",
                    expected="webhook submit and local task correlation succeed",
                    observed=str(exc),
                    passed=False,
                    issue_code=exc.issue_code,
                    severity="high",
                )
            ],
            output_excerpt=str(exc),
            metadata={
                "conversation_case_id": case.case_id,
                "harness_infra": True,
                "harness_phase": exc.phase,
                "harness_infra_metadata": exc.metadata,
                "harness_timings": harness_timings,
                "captures": captures,
            },
            started_at=started,
            completed_at=time.time(),
        )

    last_cap = captures[-1] if captures else {}
    all_findings: List[Dict[str, Any]] = []
    for c in captures:
        all_findings.extend(list(c.get("deterministic_findings") or []))
    if captures:
        final_reply = str(
            captures[-1].get("rendered_reply_sanitized")
            or captures[-1].get("formatted_reply_text")
            or captures[-1].get("raw_reply_text")
            or ""
        )
        final_reply_l = final_reply.lower()
        final_reason = str(captures[-1].get("assistant_reason") or "")
        final_domain = str(captures[-1].get("turn_plan_domain") or "")
        final_focus = str(captures[-1].get("turn_plan_continuity_focus") or "")
        for marker in case.required_reply_markers:
            m = str(marker or "").strip()
            if not m:
                continue
            if m.lower() not in final_reply_l:
                all_findings.append(
                    {
                        "family": "wrong_context_boundary",
                        "issue_code": "conversation_missing_required_marker",
                        "severity": "high",
                        "detail": f"required marker missing: {m}",
                    }
                )
        for marker in case.forbidden_reply_markers:
            m = str(marker or "").strip()
            if not m:
                continue
            if m.lower() in final_reply_l:
                all_findings.append(
                    {
                        "family": "wrong_context_boundary",
                        "issue_code": "conversation_forbidden_context_marker",
                        "severity": "high",
                        "detail": f"forbidden marker present: {m}",
                    }
                )
        if case.required_assistant_reasons and final_reason not in set(case.required_assistant_reasons):
            all_findings.append(
                {
                    "family": "wrong_domain_contamination",
                    "issue_code": "conversation_assistant_reason_mismatch",
                    "severity": "high",
                    "detail": f"assistant_reason={final_reason!r} not in {tuple(case.required_assistant_reasons)!r}",
                }
            )
        if case.required_turn_domains and final_domain not in set(case.required_turn_domains):
            all_findings.append(
                {
                    "family": "wrong_domain_contamination",
                    "issue_code": "conversation_turn_domain_mismatch",
                    "severity": "high",
                    "detail": f"turn_plan_domain={final_domain!r} not in {tuple(case.required_turn_domains)!r}",
                }
            )
        if case.required_continuity_focuses and final_focus not in set(case.required_continuity_focuses):
            all_findings.append(
                {
                    "family": "wrong_domain_contamination",
                    "issue_code": "conversation_continuity_focus_mismatch",
                    "severity": "high",
                    "detail": f"turn_plan_continuity_focus={final_focus!r} not in {tuple(case.required_continuity_focuses)!r}",
                }
            )

    high = [f for f in all_findings if f.get("severity") == "high"]
    med = [f for f in all_findings if f.get("severity") == "medium"]
    quality_state = "fail" if high else ("weak_pass" if med else "full_pass")

    observations: List[ExperienceObservation] = [
        ExperienceObservation(
            description="deterministic conversation detectors",
            expected="no high/medium-severity conversation failures",
            observed=_clip(json.dumps(all_findings, ensure_ascii=False), 1200),
            passed=not bool(high or med),
            issue_code="conversation_quality_degraded" if (high or med) else "",
            severity="high" if high else "low",
        )
    ]
    if med:
        observations.append(
            ExperienceObservation(
                description="deterministic conversation warnings",
                expected="no medium-severity findings",
                observed=_clip(json.dumps(med, ensure_ascii=False), 800),
                passed=True,
                issue_code="",
                severity="medium",
            )
        )

    weak_or_failed = bool(high or med)
    llm_eval = None
    sample_n = int(options.get("sample_pass_evals") or 0)
    sample_hit = sample_n > 0 and random.randint(1, max(1, sample_n)) == 1
    if options.get("llm_eval") and (weak_or_failed or sample_hit):
        llm_eval = run_llm_evaluator(
            last_cap, weak_or_failed=weak_or_failed, force=bool(options.get("llm_eval"))
        )
        if isinstance(llm_eval, dict) and isinstance(llm_eval.get("verdict"), dict):
            v = llm_eval["verdict"]
            ok = bool(v.get("satisfied_request")) and bool(v.get("right_domain")) and bool(v.get("useful"))
            observations.append(
                ExperienceObservation(
                    description="llm_evaluator aggregate",
                    expected="useful on-domain reply",
                    observed=_clip(json.dumps(v, ensure_ascii=False), 500),
                    passed=ok,
                    issue_code="conversation_llm_eval_failed" if not ok else "",
                    severity="medium",
                )
            )

    adjudication = None
    if options.get("adjudicate_ambiguous") and weak_or_failed:
        last_user = str(last_cap.get("user_turn") or "")
        adjudication = run_semantic_adjudicator(
            user_text=last_user,
            scenario_id=str(last_cap.get("scenario_id") or ""),
            turn_domain=str(last_cap.get("turn_plan_domain") or ""),
            continuity_focus=str(last_cap.get("turn_plan_continuity_focus") or ""),
            confidence=0.45,
        )

    excerpt = "\n\n".join(
        _clip(c.get("rendered_reply_sanitized") or c.get("raw_reply_text"), 400) for c in captures
    )

    meta: Dict[str, Any] = {
        "conversation_case_id": case.case_id,
        "behavior_family": case.behavior_family,
        "captures": captures,
        "failure_families": sorted({str(f.get("family")) for f in all_findings}),
        "deterministic_findings_high_count": len(high),
        "deterministic_findings_medium_count": len(med),
        "quality_state": quality_state,
        "llm_eval": llm_eval,
        "semantic_adjudication": adjudication,
        "harness_timings": harness_timings,
        "wait_policy": case.wait_policy,
    }
    result = ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=excerpt,
        metadata=meta,
        started_at=started,
        completed_at=time.time(),
    )
    if med and not high:
        result = ExperienceCheckResult(
            check_id=result.check_id,
            scenario_id=result.scenario_id,
            title=result.title,
            category=result.category,
            passed=result.passed,
            score=min(84, result.score),
            summary="Weak pass: medium-severity semantic issues detected.",
            output_excerpt=result.output_excerpt,
            observations=result.observations,
            tags=result.tags,
            suspected_files=result.suspected_files,
            required=result.required,
            weight=result.weight,
            metadata=result.metadata,
            started_at=result.started_at,
            completed_at=result.completed_at,
        )
    return result


def _make_runner(case: ConversationCaseSpec) -> Callable[[Any, ExperienceScenario], ExperienceCheckResult]:
    def _runner(harness: Any, scenario: ExperienceScenario) -> ExperienceCheckResult:
        return run_conversation_case(harness, scenario, case)

    return _runner


CONVERSATION_CORE_CASES: tuple[ConversationCaseSpec, ...] = (
    ConversationCaseSpec(
        case_id="hi_andrea",
        title="Hi Andrea",
        behavior_family="casual_conversation",
        turns=("Hi Andrea",),
        chat_id=88001,
        from_id=99001,
        first_update_id=18001,
        first_message_id=28001,
    ),
    ConversationCaseSpec(
        case_id="how_is_it_going",
        title="How's it going?",
        behavior_family="casual_conversation",
        turns=("How's it going?",),
        chat_id=88002,
        from_id=99002,
        first_update_id=18002,
        first_message_id=28002,
    ),
    ConversationCaseSpec(
        case_id="agenda_today",
        title="What's on the agenda today?",
        behavior_family="personal_agenda",
        turns=("What's on the agenda today?",),
        chat_id=88003,
        from_id=99003,
        first_update_id=18003,
        first_message_id=28003,
    ),
    ConversationCaseSpec(
        case_id="planned_today",
        title="What's planned today?",
        behavior_family="personal_agenda",
        turns=("What's planned today?",),
        chat_id=88004,
        from_id=99004,
        first_update_id=18004,
        first_message_id=28004,
    ),
    ConversationCaseSpec(
        case_id="attention_today",
        title="Attention today",
        behavior_family="attention_today",
        turns=("What do I need to pay attention to today?",),
        chat_id=88005,
        from_id=99005,
        first_update_id=18005,
        first_message_id=28005,
    ),
    ConversationCaseSpec(
        case_id="news_today",
        title="News today",
        behavior_family="external_information",
        turns=("What's the news today?",),
        chat_id=88006,
        from_id=99006,
        first_update_id=18006,
        first_message_id=28006,
        patch_openclaw_news=True,
        expect_external_domain=True,
    ),
    ConversationCaseSpec(
        case_id="headlines_today",
        title="Headlines today",
        behavior_family="external_information",
        turns=("What are the headlines today?",),
        chat_id=88007,
        from_id=99007,
        first_update_id=18007,
        first_message_id=28007,
        patch_openclaw_news=True,
        expect_external_domain=True,
    ),
    ConversationCaseSpec(
        case_id="working_on_now",
        title="What are we working on right now?",
        behavior_family="project_status",
        turns=("What are we working on right now?",),
        chat_id=88008,
        from_id=99008,
        first_update_id=18008,
        first_message_id=28008,
    ),
    ConversationCaseSpec(
        case_id="working_on_with_andrea",
        title="What are we working on with Andrea?",
        behavior_family="project_status",
        turns=("What are we working on with Andrea?",),
        chat_id=88009,
        from_id=99009,
        first_update_id=18009,
        first_message_id=28009,
    ),
    ConversationCaseSpec(
        case_id="approval_queue",
        title="Approval state",
        behavior_family="approval_state",
        turns=("What still needs my approval?",),
        chat_id=88010,
        from_id=99010,
        first_update_id=18010,
        first_message_id=28010,
        required_turn_domains=("approval_state",),
    ),
    ConversationCaseSpec(
        case_id="blocked_now",
        title="Blocked now",
        behavior_family="blocked_state",
        turns=("What's blocked right now?",),
        chat_id=88011,
        from_id=99011,
        first_update_id=18011,
        first_message_id=28011,
    ),
    ConversationCaseSpec(
        case_id="what_happened_task",
        title="Recent outcome",
        behavior_family="recent_outcome_history",
        turns=("What happened with that task?",),
        chat_id=88012,
        from_id=99012,
        first_update_id=18012,
        first_message_id=28012,
    ),
    ConversationCaseSpec(
        case_id="cursor_said",
        title="Cursor said",
        behavior_family="cursor_recall",
        turns=("What did Cursor say?",),
        chat_id=88013,
        from_id=99013,
        first_update_id=18013,
        first_message_id=28013,
        expect_cursor_substance=True,
        required_turn_domains=("project_status",),
        required_continuity_focuses=("recent_outcome_history",),
    ),
    ConversationCaseSpec(
        case_id="cursor_did",
        title="Cursor did",
        behavior_family="cursor_recall",
        turns=("What did Cursor do?",),
        chat_id=88014,
        from_id=99014,
        first_update_id=18014,
        first_message_id=28014,
        expect_cursor_substance=True,
        required_turn_domains=("project_status",),
        required_continuity_focuses=("recent_outcome_history",),
    ),
    ConversationCaseSpec(
        case_id="cursor_thread",
        title="Cursor thread",
        behavior_family="cursor_recall",
        turns=("What happened in the Cursor thread?",),
        chat_id=88015,
        from_id=99015,
        first_update_id=18015,
        first_message_id=28015,
        expect_cursor_substance=True,
        required_turn_domains=("project_status",),
        required_continuity_focuses=("recent_outcome_history",),
    ),
    ConversationCaseSpec(
        case_id="cursor_thread_to",
        title="Cursor thread to",
        behavior_family="cursor_recall",
        turns=("What happened to the Cursor thread?",),
        chat_id=88022,
        from_id=99022,
        first_update_id=18022,
        first_message_id=28022,
        expect_cursor_substance=True,
        required_turn_domains=("project_status",),
        required_continuity_focuses=("recent_outcome_history",),
    ),
    ConversationCaseSpec(
        case_id="cursor_with",
        title="Cursor with",
        behavior_family="cursor_recall",
        turns=("What happened with Cursor?",),
        chat_id=88023,
        from_id=99023,
        first_update_id=18023,
        first_message_id=28023,
        expect_cursor_substance=True,
        required_turn_domains=("project_status",),
        required_continuity_focuses=("recent_outcome_history",),
    ),
    ConversationCaseSpec(
        case_id="approval_queue_bare",
        title="Approval state bare",
        behavior_family="approval_state",
        turns=("What still needs approval?",),
        chat_id=88024,
        from_id=99024,
        first_update_id=18024,
        first_message_id=28024,
        required_turn_domains=("approval_state",),
    ),
    ConversationCaseSpec(
        case_id="approval_waiting",
        title="Approval waiting",
        behavior_family="approval_state",
        turns=("What is waiting for approval?",),
        chat_id=88025,
        from_id=99025,
        first_update_id=18025,
        first_message_id=28025,
        required_turn_domains=("approval_state",),
    ),
    ConversationCaseSpec(
        case_id="approval_pending",
        title="Approval pending",
        behavior_family="approval_state",
        turns=("Do I have anything pending approval?",),
        chat_id=88026,
        from_id=99026,
        first_update_id=18026,
        first_message_id=28026,
        required_turn_domains=("approval_state",),
    ),
    ConversationCaseSpec(
        case_id="approval_plural_waiting",
        title="Approval plural waiting",
        behavior_family="approval_state",
        turns=("What approvals are waiting?",),
        chat_id=88027,
        from_id=99027,
        first_update_id=18027,
        first_message_id=28027,
        required_turn_domains=("approval_state",),
    ),
    ConversationCaseSpec(
        case_id="approval_pending_inventory",
        title="Approval pending inventory",
        behavior_family="approval_state",
        turns=("What still needs approval?",),
        chat_id=88028,
        from_id=99028,
        first_update_id=18028,
        first_message_id=28028,
        setup_fn=_seed_pending_approval_inventory,
        required_reply_markers=("pending approvals for tracked task",),
        forbidden_reply_markers=("not seeing any approval requests waiting on you right now",),
        required_turn_domains=("approval_state",),
    ),
    ConversationCaseSpec(
        case_id="continue_cursor",
        title="Continue Cursor task",
        behavior_family="cursor_continuation",
        turns=("Continue that Cursor task",),
        chat_id=88016,
        from_id=99016,
        first_update_id=18016,
        first_message_id=28016,
        wait_policy="routing_smoke",
    ),
    ConversationCaseSpec(
        case_id="troubleshoot_cursor",
        title="Troubleshoot @Cursor",
        behavior_family="technical_execution",
        turns=("Help me troubleshoot this issue with @Cursor",),
        chat_id=88017,
        from_id=99017,
        first_update_id=18017,
        first_message_id=28017,
        wait_policy="routing_smoke",
    ),
    ConversationCaseSpec(
        case_id="repo_wide_plan",
        title="Repo-wide plan",
        behavior_family="technical_execution",
        turns=("Build a plan for fixing this repo-wide issue",),
        chat_id=88018,
        from_id=99018,
        first_update_id=18018,
        first_message_id=28018,
        wait_policy="routing_smoke",
    ),
    ConversationCaseSpec(
        case_id="bluebubbles_then_summarize",
        title="Tool follow-up carryover",
        behavior_family="tool_followup_carryover",
        turns=(
            "Can you pull text messages from BlueBubbles?",
            "Can you summarize my texts too?",
        ),
        chat_id=88019,
        from_id=99019,
        first_update_id=18019,
        first_message_id=28019,
        patch_bluebubbles=True,
        expect_tool_carryover=True,
    ),
    ConversationCaseSpec(
        case_id="anaphoric_sequence",
        title="Anaphoric follow-up carryover",
        behavior_family="general_followup_carryover",
        turns=("What happened there?", "What about that one?", "Continue that"),
        chat_id=88020,
        from_id=99020,
        first_update_id=19020,
        first_message_id=29020,
        required_turn_domains=("project_status",),
        forbidden_reply_markers=("recent clean Cursor result",),
    ),
    ConversationCaseSpec(
        case_id="wrong_thread_cursor_recall",
        title="Wrong-thread Cursor recall boundary",
        behavior_family="wrong_context_boundary",
        turns=("What did Cursor say?",),
        chat_id=77781,
        from_id=99090,
        first_update_id=18781,
        first_message_id=6871,
        setup_fn=_seed_multitask_recall_thread_state,
        turn_payload_overrides=({"message_thread_id": 502},),
        required_reply_markers=("THREAD_BETA_UNIQUE_MARKER",),
        forbidden_reply_markers=("THREAD_ALPHA_UNIQUE_MARKER",),
        expect_cursor_substance=True,
    ),
    ConversationCaseSpec(
        case_id="continue_that_wrong_task",
        title="Continue-that selects correct workstream",
        behavior_family="wrong_context_boundary",
        turns=("Continue that",),
        chat_id=77781,
        from_id=99090,
        first_update_id=18782,
        first_message_id=6872,
        turn_payload_overrides=({"message_thread_id": 502},),
        required_reply_markers=("THREAD_BETA_UNIQUE_MARKER",),
        forbidden_reply_markers=("THREAD_ALPHA_UNIQUE_MARKER",),
        wait_policy="routing_smoke",
    ),
    ConversationCaseSpec(
        case_id="identity_question_not_hijacked",
        title="Identity question not continuity-hijacked",
        behavior_family="wrong_domain_contamination",
        turns=("Is this OpenClaw?",),
        chat_id=77783,
        from_id=99091,
        first_update_id=18783,
        first_message_id=6873,
        setup_fn=_seed_identity_hijack_state,
        required_assistant_reasons=("stack_or_tooling_question",),
        required_reply_markers=("openclaw",),
        forbidden_reply_markers=("goal `", "tracked task `", "where things stand:"),
    ),
    ConversationCaseSpec(
        case_id="cursor_recap_recursion",
        title="Cursor recap recursion hygiene",
        behavior_family="thin_summary",
        turns=("What did Cursor say?",),
        chat_id=77784,
        from_id=99092,
        first_update_id=18784,
        first_message_id=6874,
        setup_fn=_seed_recap_recursion_state,
        required_reply_markers=("cursor recap:",),
        forbidden_reply_markers=("cursor recap: cursor recap:",),
        expect_cursor_substance=True,
    ),
    ConversationCaseSpec(
        case_id="source_truth_beats_derived_recall",
        title="Source-truth recall beats derived assistant recap surface",
        behavior_family="cursor_recall",
        turns=("What did Cursor say?",),
        chat_id=77788,
        from_id=99093,
        first_update_id=18788,
        first_message_id=6875,
        setup_fn=_seed_source_truth_over_derived_recall,
        required_reply_markers=("SOURCE_TRUTH_UNIQUE_OPENCLAW_FACT_Z9",),
        forbidden_reply_markers=("DERIVED_ASSISTANT_RECYCLED_SURFACE_XX",),
        expect_cursor_substance=True,
    ),
    ConversationCaseSpec(
        case_id="bare_continue_rescues_recent_cursor",
        title="Bare continue finds recent source-rich neighbor",
        behavior_family="cursor_continuation",
        turns=("Continue that",),
        chat_id=77789,
        from_id=99094,
        first_update_id=18789,
        first_message_id=6876,
        setup_fn=_seed_bare_continue_rich_neighbor,
        required_reply_markers=("BARE_CONTINUE_RICH_NEIGHBOR_MARKER_Q5",),
        forbidden_reply_markers=("I do not see active tracked work right now",),
        wait_policy="routing_smoke",
    ),
    ConversationCaseSpec(
        case_id="recall_rejects_continuation_assistant_surface",
        title="Cursor recall ignores continuation fallback in assistant.last_reply",
        behavior_family="cursor_recall",
        turns=("What did Cursor say?",),
        chat_id=77790,
        from_id=99095,
        first_update_id=18790,
        first_message_id=6501,
        setup_fn=_seed_recall_rejects_continuation_assistant_surface,
        required_reply_markers=("RECALL_SOURCE_TRUTH_BOUNDARY_77",),
        forbidden_reply_markers=("safely continue", "start a new heavy-lift pass"),
        expect_cursor_substance=True,
        wait_policy="routing_smoke",
    ),
    ConversationCaseSpec(
        case_id="continue_prefers_fresher_workstream",
        title="Continuation prefers fresher source-rich workstream over stale neighbor",
        behavior_family="cursor_continuation",
        turns=("Continue that",),
        chat_id=77791,
        from_id=99096,
        first_update_id=18791,
        first_message_id=6603,
        setup_fn=_seed_continue_prefers_fresher_workstream,
        required_reply_markers=("FRESHER_CONTINUE_WORKSTREAM_MARKER_V3",),
        forbidden_reply_markers=("STALE_VERBOSE_CONTINUE_MARKER_OLD", "multi-agent handoff"),
        wait_policy="routing_smoke",
    ),
    ConversationCaseSpec(
        case_id="recall_rejects_approval_status_sludge",
        title="Cursor recall rejects approval inventory + status_followup receipt",
        behavior_family="cursor_recall",
        turns=("What did Cursor say?",),
        chat_id=77792,
        from_id=99097,
        first_update_id=18792,
        first_message_id=6701,
        setup_fn=_seed_recall_rejects_approval_status_sludge,
        required_reply_markers=("recent clean Cursor result",),
        forbidden_reply_markers=(
            "approval requests waiting on you",
            "Status / follow-up reply",
            "goal_runtime_status",
        ),
        expect_cursor_substance=False,
        required_turn_domains=("project_status",),
        required_continuity_focuses=("recent_outcome_history",),
        wait_policy="routing_smoke",
    ),
    ConversationCaseSpec(
        case_id="cursor_thread_rejects_approval_status_sludge",
        title="Cursor thread recall rejects approval/status sludge",
        behavior_family="cursor_recall",
        turns=("What happened in the Cursor thread?",),
        chat_id=77793,
        from_id=99098,
        first_update_id=18793,
        first_message_id=6710,
        setup_fn=_seed_recall_rejects_approval_status_sludge_thread,
        required_reply_markers=("recent clean Cursor result",),
        forbidden_reply_markers=(
            "approval requests waiting on you",
            "Status / follow-up reply",
            "goal_runtime_status",
        ),
        expect_cursor_substance=False,
        required_turn_domains=("project_status",),
        required_continuity_focuses=("recent_outcome_history",),
        wait_policy="routing_smoke",
    ),
    ConversationCaseSpec(
        case_id="continue_then_approval_no_merge_hijack",
        title="Continuation merge does not hijack approval turn",
        behavior_family="wrong_context_boundary",
        turns=("Continue that", "What still needs approval?"),
        chat_id=88029,
        from_id=99029,
        first_update_id=18029,
        first_message_id=28029,
        setup_fn=_seed_pending_approval_inventory,
        required_turn_domains=("approval_state",),
        forbidden_reply_markers=("I do not see active tracked work right now",),
    ),
    ConversationCaseSpec(
        case_id="cursor_recall_rich_truth_not_fallback_shaped",
        title="Rich cursor recall should avoid fallback-shaped rendering",
        behavior_family="cursor_recall",
        turns=("What did Cursor say?",),
        chat_id=77794,
        from_id=99099,
        first_update_id=18794,
        first_message_id=6721,
        setup_fn=_seed_source_truth_rich_recall,
        required_reply_markers=("RICH_RECALL_GROUNDED_FACT_42",),
        forbidden_reply_markers=("recent clean Cursor result",),
        expect_cursor_substance=True,
        required_turn_domains=("project_status",),
        required_continuity_focuses=("recent_outcome_history",),
        wait_policy="routing_smoke",
    ),
    ConversationCaseSpec(
        case_id="agenda_then_opinion",
        title="Agenda then opinion boundary",
        behavior_family="opinion_reflection",
        turns=("What's on the agenda today?", "What do you think about that?"),
        chat_id=88021,
        from_id=99021,
        first_update_id=19121,
        first_message_id=29121,
        required_turn_domains=("opinion_reflection",),
        forbidden_reply_markers=("recent clean Cursor result",),
    ),
)


def conversation_core_scenarios(conversation_eval_options: Mapping[str, Any] | None = None) -> List[ExperienceScenario]:
    opts = dict(conversation_eval_options or {})
    smoke = bool(opts.get("smoke"))
    smoke_ids = set(CONVERSATION_SMOKE_CASE_IDS) if smoke else None
    out: List[ExperienceScenario] = []
    for case in CONVERSATION_CORE_CASES:
        if smoke_ids is not None and case.case_id not in smoke_ids:
            continue
        out.append(
            ExperienceScenario(
                scenario_id=f"conversation_core::{case.case_id}",
                title=case.title,
                description=f"Conversation core eval · {case.behavior_family}",
                category="conversation",
                tags=["conversation_eval", "conversation_core", case.behavior_family],
                suspected_files=[
                    "services/andrea_sync/server.py",
                    "services/andrea_sync/andrea_router.py",
                    "services/andrea_sync/assistant_answer_composer.py",
                    "services/andrea_sync/turn_intelligence.py",
                ],
                runner=_make_runner(case),
                metadata={"conversation_eval_options": opts},
            )
        )
    return out


def cluster_failed_checks(checks: Iterable[ExperienceCheckResult]) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in checks:
        meta = row.metadata if isinstance(row.metadata, dict) else {}
        qstate = str(meta.get("quality_state") or ("fail" if not row.passed else "full_pass"))
        if row.passed and qstate == "full_pass":
            continue
        fams = meta.get("failure_families")
        if not isinstance(fams, list) or not fams:
            fams = ["unknown"]
        for fam in fams:
            key = str(fam)
            b = buckets.setdefault(
                key,
                {"failure_family": key, "count": 0, "scenario_ids": []},
            )
            b["count"] += 1
            if row.scenario_id not in b["scenario_ids"]:
                b["scenario_ids"].append(row.scenario_id)
    return sorted(buckets.values(), key=lambda x: -int(x.get("count") or 0))


def build_cursor_fix_brief(
    *,
    cluster: Mapping[str, Any],
    checks: Sequence[ExperienceCheckResult],
    planner_model: str | None = None,
) -> Dict[str, Any]:
    """Bounded fix brief for human/Cursor; does not apply code changes."""
    fam = str(cluster.get("failure_family") or "unknown")
    related = []
    for c in checks:
        cmeta = c.metadata if isinstance(c.metadata, dict) else {}
        cstate = str(cmeta.get("quality_state") or ("fail" if not c.passed else "full_pass"))
        if cstate != "full_pass" and fam in (cmeta.get("failure_families") or []):
            related.append(c)
    prompts: List[str] = []
    for c in related[:5]:
        caps = c.metadata.get("captures") if isinstance(c.metadata, dict) else None
        if isinstance(caps, list) and caps:
            last = caps[-1] if isinstance(caps[-1], dict) else {}
            u = last.get("user_turn")
            r = last.get("rendered_reply_sanitized") or last.get("raw_reply_text")
            if u:
                prompts.append(f"User: {u}\nReply: {_clip(r, 600)}")
    codes: List[str] = []
    for c in related:
        codes.extend(c.issue_codes)
    brief = {
        "title": f"Conversation quality fix · {fam}",
        "failure_family": fam,
        "deterministic_issue_codes": sorted({str(x) for x in codes}),
        "failing_prompts": prompts,
        "likely_files": [
            "services/andrea_sync/server.py",
            "services/andrea_sync/andrea_router.py",
            "services/andrea_sync/assistant_answer_composer.py",
            "services/andrea_sync/user_surface.py",
            "services/andrea_sync/turn_intelligence.py",
        ],
        "scope_instruction": "Modify 1-3 files; avoid runtime env toggles unless necessary.",
        "acceptance_criteria": [
            "conversation_core suite passes deterministic detectors for this family",
            "no new metadata or echo regressions on casual + status turns",
        ],
        "baseline_vs_candidate": {
            "note": "Re-run scripts/andrea_experience_cycle.py with --suite conversation_core "
            "and compare verification_report.checks metadata before/after change.",
            "compare_helper": "services/andrea_sync/repair_executor.py::compare_verification_reports",
        },
        "planner_model": planner_model or model_for_role("planner"),
    }
    return brief


def attach_conversation_eval_report(
    run_metadata: Dict[str, Any],
    checks: Sequence[ExperienceCheckResult],
    *,
    prepare_fix_brief: bool,
) -> None:
    clusters = cluster_failed_checks(checks)
    run_metadata["conversation_failure_clusters"] = clusters
    run_metadata["failure_family_counts"] = {
        str(c.get("failure_family")): int(c.get("count") or 0) for c in clusters
    }
    if prepare_fix_brief and clusters:
        briefs = []
        for c in clusters[:8]:
            if str(c.get("failure_family") or "") in {
                "generic_fallback_leak",
                "question_echo",
                "metadata_surface_leak",
                "thin_summary",
                "cursor_recall_failure",
                "cursor_continuation_failure",
                "followup_carryover_failure",
                "wrong_context_boundary",
                "overly_mechanical_wording",
            }:
                briefs.append(build_cursor_fix_brief(cluster=c, checks=checks))
        run_metadata["cursor_fix_briefs"] = briefs
    else:
        run_metadata["cursor_fix_briefs"] = []


__all__ = [
    "FAILURE_FAMILIES",
    "CONVERSATION_SMOKE_CASE_IDS",
    "attach_conversation_eval_report",
    "build_cursor_fix_brief",
    "build_turn_capture",
    "cluster_failed_checks",
    "conversation_core_scenarios",
    "runtime_adjudication_enabled",
    "runtime_adjudication_gate",
    "run_conversation_case",
    "run_deterministic_detectors",
    "run_llm_evaluator",
    "run_semantic_adjudicator",
    "CONVERSATION_CORE_CASES",
]

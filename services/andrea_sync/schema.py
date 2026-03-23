"""
Lockstep command/event schema and invariants (schema version 1).

All channels (Telegram, Alexa, CLI, Cursor reporting) normalize into this vocabulary.
Tasks are the unit of lockstep; events are append-only; projected state is derived.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1

# --- Channels (ingress attribution) ---


class Channel(str, Enum):
    TELEGRAM = "telegram"
    ALEXA = "alexa"
    CLI = "cli"
    CURSOR = "cursor"
    INTERNAL = "internal"


# --- Command types (imperative, accepted or rejected) ---


class CommandType(str, Enum):
    CREATE_TASK = "CreateTask"
    SUBMIT_USER_MESSAGE = "SubmitUserMessage"
    SUBMIT_USER_FEEDBACK = "SubmitUserFeedback"
    CREATE_CURSOR_JOB = "CreateCursorJob"
    CURSOR_FOLLOWUP = "CursorFollowup"
    CURSOR_STOP = "CursorStop"
    REPORT_CURSOR_EVENT = "ReportCursorEvent"  # lifecycle from cursor_openclaw / handoff
    ALEXA_UTTERANCE = "AlexaUtterance"
    PUBLISH_CAPABILITY_SNAPSHOT = "PublishCapabilitySnapshot"
    RECORD_EVALUATION_FINDING = "RecordEvaluationFinding"
    CREATE_OPTIMIZATION_PROPOSAL = "CreateOptimizationProposal"
    RUN_OPTIMIZATION_CYCLE = "RunOptimizationCycle"
    APPLY_OPTIMIZATION_PROPOSAL = "ApplyOptimizationProposal"
    SAVE_PRINCIPAL_MEMORY = "SavePrincipalMemory"
    SET_PRINCIPAL_PREFERENCE = "SetPrincipalPreference"
    LINK_PRINCIPAL_IDENTITY = "LinkPrincipalIdentity"
    CREATE_REMINDER = "CreateReminder"
    RUN_PROACTIVE_SWEEP = "RunProactiveSweep"
    KILL_SWITCH_ENGAGE = "KillSwitchEngage"
    KILL_SWITCH_RELEASE = "KillSwitchRelease"


# --- Event types (facts, append-only) ---


class EventType(str, Enum):
    COMMAND_RECEIVED = "CommandReceived"
    COMMAND_DEDUPED = "CommandDeduped"
    TASK_CREATED = "TaskCreated"
    USER_MESSAGE = "UserMessage"
    USER_FEEDBACK = "UserFeedback"
    ASSISTANT_REPLIED = "AssistantReplied"
    JOB_QUEUED = "JobQueued"
    JOB_STARTED = "JobStarted"
    JOB_PROGRESS = "JobProgress"
    JOB_COMPLETED = "JobCompleted"
    JOB_FAILED = "JobFailed"
    HUMAN_APPROVAL_REQUIRED = "HumanApprovalRequired"
    ORCHESTRATION_STEP = "OrchestrationStep"
    PRINCIPAL_LINKED = "PrincipalLinked"
    PRINCIPAL_MEMORY_SAVED = "PrincipalMemorySaved"
    PRINCIPAL_PREFERENCE_UPDATED = "PrincipalPreferenceUpdated"
    REMINDER_CREATED = "ReminderCreated"
    REMINDER_TRIGGERED = "ReminderTriggered"
    REMINDER_DELIVERED = "ReminderDelivered"
    REMINDER_FAILED = "ReminderFailed"
    EVALUATION_RECORDED = "EvaluationRecorded"
    OPTIMIZATION_PROPOSAL = "OptimizationProposal"
    OPTIMIZATION_RUN_STARTED = "OptimizationRunStarted"
    OPTIMIZATION_RUN_COMPLETED = "OptimizationRunCompleted"
    OPTIMIZATION_RUN_FAILED = "OptimizationRunFailed"
    REGRESSION_RECORDED = "RegressionRecorded"
    LOCAL_AUTO_HEAL_STARTED = "LocalAutoHealStarted"
    LOCAL_AUTO_HEAL_COMPLETED = "LocalAutoHealCompleted"
    LOCAL_AUTO_HEAL_FAILED = "LocalAutoHealFailed"
    EXTERNAL_REF = "ExternalRef"  # telegram update_id, alexa request id, etc.
    CAPABILITY_SNAPSHOT = "CapabilitySnapshot"
    KILL_SWITCH_ENGAGED = "KillSwitchEngaged"
    KILL_SWITCH_RELEASED = "KillSwitchReleased"


# --- Task status (projected) ---


class TaskStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_SLUG = re.compile(r"[^a-z0-9_-]+", re.I)
_INTERNAL_RUNTIME_RE = re.compile(
    r"\b("
    r"sessionkey|session key|sessionid|session id|session label|runtime id|"
    r"sessions_send|sessions_spawn|attachments\.enabled|tool chatter|tool call|"
    r"internal runtime|cursor session|label that identifies|session identifier"
    r")\b",
    re.I,
)


def normalize_idempotency_base(channel: str, external_id: str, command_type: str) -> str:
    raw = f"{channel}|{external_id}|{command_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def normalize_scoped_idempotency_key(
    scope_a: str, scope_b: str, command_type: str
) -> str:
    """Stable key for idempotency rows scoped to an existing task or report payload."""
    raw = f"{scope_a}|{scope_b}|{command_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def new_task_id() -> str:
    return f"tsk_{uuid.uuid4().hex[:16]}"


def validate_command_type(raw: str) -> CommandType:
    try:
        return CommandType(raw)
    except ValueError as e:
        raise ValueError(f"unknown command_type: {raw}") from e


def validate_event_type(raw: str) -> EventType:
    try:
        return EventType(raw)
    except ValueError as e:
        raise ValueError(f"unknown event_type: {raw}") from e


def _clip_meta_text(value: Any, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _ensure_meta_dict(root: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = root.get(key)
    if isinstance(value, dict):
        return value
    fresh: Dict[str, Any] = {}
    root[key] = fresh
    return fresh


def _append_outcome_flag(flags: List[str], flag: str) -> None:
    if flag and flag not in flags:
        flags.append(flag)


def _looks_internal_runtime_text(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if _INTERNAL_RUNTIME_RE.search(text):
        return True
    return bool(
        re.search(r"\b(tool|runtime|config|setting|session)\b", text, re.I)
        and re.search(r"\b(key|label|id|enabled|disabled|missing|required)\b", text, re.I)
    )


def _derive_requested_capability(payload: Dict[str, Any]) -> str:
    explicit = str(payload.get("requested_capability") or "").strip()
    if explicit:
        return explicit
    hint = str(payload.get("routing_hint") or "").strip().lower()
    collab = str(payload.get("collaboration_mode") or "").strip().lower()
    if hint == "cursor":
        return "cursor_execution"
    if hint == "collaborate" or collab == "collaborative":
        return "collaboration"
    return "assistant"


def _normalize_machine_trace(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    for raw in value[:8]:
        if not isinstance(raw, dict):
            summary = _clip_meta_text(raw, 240)
            if summary:
                out.append({"phase": "", "lane": "", "summary": summary})
            continue
        phase = str(raw.get("phase") or "").strip().lower()
        lane = str(raw.get("lane") or raw.get("role") or raw.get("worker") or "").strip()
        provider = str(raw.get("provider") or "").strip()
        model = str(raw.get("model") or "").strip()
        summary = _clip_meta_text(
            raw.get("summary") or raw.get("text") or raw.get("message") or "",
            240,
        )
        entry = {
            "phase": phase,
            "lane": lane,
            "provider": provider,
            "model": model,
            "summary": summary,
        }
        signature = "|".join(
            [entry["phase"], entry["lane"], entry["provider"], entry["model"], entry["summary"]]
        )
        if any(
            signature
            == "|".join(
                [
                    str(existing.get("phase") or ""),
                    str(existing.get("lane") or ""),
                    str(existing.get("provider") or ""),
                    str(existing.get("model") or ""),
                    str(existing.get("summary") or ""),
                ]
            )
            for existing in out
        ):
            continue
        if any(entry.values()):
            out.append(entry)
    return out


def _normalize_phase_outputs(value: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for phase in ("plan", "critique", "execution", "synthesis"):
        raw = value.get(phase)
        if isinstance(raw, dict):
            lane = str(raw.get("lane") or "").strip()
            status = str(raw.get("status") or "").strip().lower() or "completed"
            summary = _clip_meta_text(
                raw.get("summary") or raw.get("message") or raw.get("text") or "",
                320,
            )
        else:
            lane = ""
            status = "completed"
            summary = _clip_meta_text(raw, 320)
        if summary or lane or raw is not None:
            out[phase] = {
                "lane": lane,
                "status": status,
                "summary": summary,
            }
    return out


def _fold_orchestration_step_meta(proj: "TaskProjection", payload: Dict[str, Any]) -> None:
    orchestration_meta = _ensure_meta_dict(proj.meta, "orchestration")
    phase = str(payload.get("phase") or "").strip().lower()
    status = str(payload.get("status") or "").strip().lower() or "completed"
    lane = str(payload.get("lane") or payload.get("runner") or "").strip()
    provider = str(payload.get("provider") or "").strip()
    model = str(payload.get("model") or "").strip()
    summary = _clip_meta_text(
        payload.get("summary") or payload.get("message") or payload.get("text") or "",
        320,
    )
    if phase:
        orchestration_meta["last_phase"] = phase
    orchestration_meta["last_status"] = status
    if lane:
        orchestration_meta["last_lane"] = lane
    if provider:
        orchestration_meta["last_provider"] = provider
    if model:
        orchestration_meta["last_model"] = model
    if summary:
        orchestration_meta["last_summary"] = summary

    phase_counts = orchestration_meta.get("phase_counts")
    if not isinstance(phase_counts, dict):
        phase_counts = {}
        orchestration_meta["phase_counts"] = phase_counts
    if phase and status == "completed":
        phase_counts[phase] = int(phase_counts.get(phase) or 0) + 1

    status_counts = orchestration_meta.get("status_counts")
    if not isinstance(status_counts, dict):
        status_counts = {}
        orchestration_meta["status_counts"] = status_counts
    if phase:
        status_key = f"{phase}:{status}"
        status_counts[status_key] = int(status_counts.get(status_key) or 0) + 1

    phase_details = orchestration_meta.get("phases")
    if not isinstance(phase_details, dict):
        phase_details = {}
        orchestration_meta["phases"] = phase_details
    if phase:
        detail = phase_details.get(phase)
        if not isinstance(detail, dict):
            detail = {}
            phase_details[phase] = detail
        detail["status"] = status
        if lane:
            detail["lane"] = lane
        if provider:
            detail["provider"] = provider
        if model:
            detail["model"] = model
        if summary:
            detail["summary"] = summary

    if phase and status == "failed":
        orchestration_meta["failed_phase"] = phase
    elif phase and status == "completed" and orchestration_meta.get("failed_phase") == phase:
        orchestration_meta.pop("failed_phase", None)

    steps = orchestration_meta.get("steps")
    if not isinstance(steps, list):
        steps = []
        orchestration_meta["steps"] = steps
    entry = {
        "phase": phase,
        "status": status,
        "lane": lane,
        "provider": provider,
        "model": model,
        "summary": summary,
    }
    if any(entry.values()):
        steps.append(entry)
        if len(steps) > 12:
            del steps[:-12]


def _fold_openclaw_contract_meta(proj: "TaskProjection", payload: Dict[str, Any]) -> None:
    backend = str(payload.get("backend") or "").strip().lower()
    runner = str(payload.get("runner") or "").strip().lower()
    has_openclaw_fields = any(
        payload.get(key)
        for key in (
            "openclaw_run_id",
            "openclaw_session_id",
            "provider",
            "model",
            "raw_text",
            "internal_trace",
            "user_summary",
            "blocked_reason",
            "collaboration_trace",
            "machine_collaboration_trace",
            "phase_outputs",
        )
    )
    if not has_openclaw_fields and backend != "openclaw" and runner != "openclaw":
        return
    openclaw_meta = proj.meta.setdefault("openclaw", {})
    if payload.get("openclaw_run_id"):
        openclaw_meta["run_id"] = str(payload["openclaw_run_id"])
    if payload.get("openclaw_session_id"):
        openclaw_meta["session_id"] = str(payload["openclaw_session_id"])
    if payload.get("provider"):
        openclaw_meta["provider"] = str(payload["provider"])
    if payload.get("model"):
        openclaw_meta["model"] = str(payload["model"])
    if payload.get("raw_text"):
        openclaw_meta["raw_text"] = str(payload["raw_text"])[:4000]
    if payload.get("internal_trace"):
        openclaw_meta["internal_trace"] = str(payload["internal_trace"])[:4000]
    summary = payload.get("user_summary") or payload.get("summary")
    if summary:
        openclaw_meta["user_summary"] = _clip_meta_text(summary, 2000)
    if payload.get("blocked_reason"):
        openclaw_meta["blocked_reason"] = _clip_meta_text(payload.get("blocked_reason"), 500)
    trace = payload.get("collaboration_trace")
    if isinstance(trace, list):
        normalized: List[str] = []
        for raw in trace[:6]:
            item = _clip_meta_text(raw, 240)
            if item and item not in normalized:
                normalized.append(item)
        openclaw_meta["collaboration_trace"] = normalized
    machine_trace = _normalize_machine_trace(payload.get("machine_collaboration_trace"))
    if machine_trace:
        openclaw_meta["machine_collaboration_trace"] = machine_trace
    phase_outputs = _normalize_phase_outputs(payload.get("phase_outputs"))
    if phase_outputs:
        openclaw_meta["phase_outputs"] = phase_outputs


def _derive_result_kind(
    proj: "TaskProjection",
    assistant_meta: Dict[str, Any],
    execution_meta: Dict[str, Any],
    cursor_meta: Dict[str, Any],
) -> str:
    delegated_to_cursor = bool(execution_meta.get("delegated_to_cursor"))
    backend = str(execution_meta.get("backend") or "").strip().lower()
    runner = str(execution_meta.get("runner") or "").strip().lower()
    cursor_kind = str(cursor_meta.get("kind") or "").strip().lower()
    if assistant_meta.get("route") == "direct":
        if proj.status == TaskStatus.COMPLETED:
            return "direct_completed"
        if proj.status == TaskStatus.FAILED:
            return "direct_failed"
        return "direct_in_progress"
    if proj.status == TaskStatus.COMPLETED:
        if delegated_to_cursor:
            return "openclaw_cursor_completed"
        if backend == "openclaw" or runner == "openclaw" or cursor_kind == "openclaw":
            return "openclaw_completed"
        if backend == "cursor" or runner == "cursor" or cursor_kind == "cursor":
            return "cursor_completed"
        return "completed"
    if proj.status == TaskStatus.FAILED:
        if delegated_to_cursor:
            return "openclaw_cursor_failed"
        if backend == "openclaw" or runner == "openclaw" or cursor_kind == "openclaw":
            return "openclaw_failed"
        if backend == "cursor" or runner == "cursor" or cursor_kind == "cursor":
            return "cursor_failed"
        return "failed"
    if proj.status == TaskStatus.RUNNING:
        return "running"
    if proj.status == TaskStatus.QUEUED:
        return "queued"
    if proj.status == TaskStatus.AWAITING_APPROVAL:
        return "awaiting_approval"
    return proj.status.value


def _refresh_outcome_meta(proj: "TaskProjection") -> None:
    execution_meta = proj.meta.get("execution") if isinstance(proj.meta.get("execution"), dict) else {}
    cursor_meta = proj.meta.get("cursor") if isinstance(proj.meta.get("cursor"), dict) else {}
    openclaw_meta = proj.meta.get("openclaw") if isinstance(proj.meta.get("openclaw"), dict) else {}
    assistant_meta = proj.meta.get("assistant") if isinstance(proj.meta.get("assistant"), dict) else {}
    telegram_meta = proj.meta.get("telegram") if isinstance(proj.meta.get("telegram"), dict) else {}
    orchestration_meta = (
        proj.meta.get("orchestration") if isinstance(proj.meta.get("orchestration"), dict) else {}
    )
    identity_meta = proj.meta.get("identity") if isinstance(proj.meta.get("identity"), dict) else {}
    proactive_meta = proj.meta.get("proactive") if isinstance(proj.meta.get("proactive"), dict) else {}
    analytics_meta = _ensure_meta_dict(proj.meta, "analytics")
    feedback_meta = _ensure_meta_dict(proj.meta, "feedback")
    evaluation_meta = _ensure_meta_dict(proj.meta, "evaluation")
    optimization_meta = _ensure_meta_dict(proj.meta, "optimization")
    outcome_meta = _ensure_meta_dict(proj.meta, "outcome")

    route_mode = str(assistant_meta.get("route") or "").strip()
    if not route_mode and (
        execution_meta.get("lane")
        or execution_meta.get("runner")
        or execution_meta.get("backend")
        or cursor_meta.get("kind")
    ):
        route_mode = "delegate"
    route_reason = str(
        execution_meta.get("route_reason") or assistant_meta.get("reason") or ""
    ).strip()
    routing_hint = str(
        execution_meta.get("routing_hint") or telegram_meta.get("routing_hint") or "auto"
    ).strip() or "auto"
    collaboration_mode = str(
        execution_meta.get("collaboration_mode")
        or telegram_meta.get("collaboration_mode")
        or "auto"
    ).strip() or "auto"
    requested_capability = str(
        execution_meta.get("requested_capability")
        or telegram_meta.get("requested_capability")
        or ""
    ).strip()
    visibility_mode = str(
        execution_meta.get("visibility_mode") or telegram_meta.get("visibility_mode") or "summary"
    ).strip() or "summary"
    backend = str(execution_meta.get("backend") or "").strip()
    lane = str(execution_meta.get("lane") or "").strip()
    runner = str(execution_meta.get("runner") or "").strip()
    delegated_to_cursor = bool(execution_meta.get("delegated_to_cursor"))
    cursor_invoked = delegated_to_cursor or bool(
        cursor_meta.get("agent_url")
        or cursor_meta.get("pr_url")
        or cursor_meta.get("cursor_agent_id")
        or runner == "cursor"
        or backend == "cursor"
        or str(cursor_meta.get("kind") or "").strip().lower() == "cursor"
    )
    user_turn_count = int(analytics_meta.get("user_turn_count") or 0)
    continuation_count = int(telegram_meta.get("continuation_count") or 0)
    feedback_count = int(feedback_meta.get("count") or 0)
    score_total = float(feedback_meta.get("score_total") or 0.0)
    feedback_average = round(score_total / feedback_count, 3) if feedback_count > 0 else None
    last_feedback_label = str(feedback_meta.get("last_label") or "").strip()
    collaboration_trace = (
        openclaw_meta.get("collaboration_trace")
        if isinstance(openclaw_meta.get("collaboration_trace"), list)
        else []
    )
    machine_trace = (
        openclaw_meta.get("machine_collaboration_trace")
        if isinstance(openclaw_meta.get("machine_collaboration_trace"), list)
        else []
    )
    blocked_reason = str(
        openclaw_meta.get("blocked_reason") or execution_meta.get("user_safe_error") or ""
    ).strip()
    internal_trace = str(
        openclaw_meta.get("internal_trace")
        or openclaw_meta.get("raw_text")
        or execution_meta.get("internal_error")
        or ""
    ).strip()
    user_visible_text = " ".join(
        part
        for part in (
            str(assistant_meta.get("last_reply") or "").strip(),
            str(proj.summary or "").strip(),
            str(openclaw_meta.get("user_summary") or "").strip(),
        )
        if part
    )
    negative_feedback = last_feedback_label in {
        "negative",
        "downvote",
        "thumbs_down",
        "bad",
    } or (feedback_average is not None and feedback_average < 0)
    phase_counts = (
        orchestration_meta.get("phase_counts")
        if isinstance(orchestration_meta.get("phase_counts"), dict)
        else {}
    )
    plan_count = int(phase_counts.get("plan") or 0)
    critique_count = int(phase_counts.get("critique") or 0)
    execution_count = int(phase_counts.get("execution") or 0)
    synthesis_count = int(phase_counts.get("synthesis") or 0)
    orchestration_steps = (
        orchestration_meta.get("steps") if isinstance(orchestration_meta.get("steps"), list) else []
    )
    failed_phase = str(orchestration_meta.get("failed_phase") or "").strip().lower()
    principal_id = str(identity_meta.get("principal_id") or "").strip()
    memory_count = int(identity_meta.get("memory_count") or 0)
    pending_reminder_count = int(proactive_meta.get("pending_reminder_count") or 0)
    last_reminder_status = str(proactive_meta.get("last_reminder_status") or "").strip().lower()

    ux_flags: List[str] = []
    if route_reason == "stack_or_tooling_question" and route_mode == "delegate":
        _append_outcome_flag(ux_flags, "overdelegated_meta_question")
    if visibility_mode == "full" and collaboration_mode not in {"cursor_primary", "collaborative"}:
        _append_outcome_flag(ux_flags, "low_value_full_visibility")
    if continuation_count >= 2:
        _append_outcome_flag(ux_flags, "continuation_heavy")
    if proj.status == TaskStatus.FAILED and route_mode == "delegate":
        _append_outcome_flag(ux_flags, "execution_failed")
    if negative_feedback:
        _append_outcome_flag(ux_flags, "negative_feedback")
    if blocked_reason:
        _append_outcome_flag(ux_flags, "blocked_capability")
    if _looks_internal_runtime_text(user_visible_text):
        _append_outcome_flag(ux_flags, "runtime_jargon_leaked")
    if internal_trace and _looks_internal_runtime_text(internal_trace):
        _append_outcome_flag(ux_flags, "internal_runtime_trace")
    if failed_phase == "plan":
        _append_outcome_flag(ux_flags, "planner_failure")
    if failed_phase == "critique":
        _append_outcome_flag(ux_flags, "critic_failure")
    if failed_phase == "execution":
        _append_outcome_flag(ux_flags, "executor_failure")
    if collaboration_mode in {"cursor_primary", "collaborative"} and plan_count > 0 and critique_count == 0:
        _append_outcome_flag(ux_flags, "critic_missing")
    if last_reminder_status == "failed":
        _append_outcome_flag(ux_flags, "proactive_delivery_failed")

    outcome_meta["version"] = 1
    outcome_meta["terminal_status"] = proj.status.value
    outcome_meta["result_kind"] = _derive_result_kind(
        proj, assistant_meta, execution_meta, cursor_meta
    )
    outcome_meta["route_mode"] = route_mode or "unknown"
    outcome_meta["route_reason"] = route_reason
    outcome_meta["routing_hint"] = routing_hint
    outcome_meta["collaboration_mode"] = collaboration_mode
    outcome_meta["requested_capability"] = requested_capability or "unspecified"
    outcome_meta["visibility_mode"] = visibility_mode
    outcome_meta["execution_lane"] = lane
    outcome_meta["runner"] = runner
    outcome_meta["backend"] = backend
    outcome_meta["cursor_invoked"] = cursor_invoked
    outcome_meta["delegated_to_cursor"] = delegated_to_cursor
    outcome_meta["has_pr"] = bool(cursor_meta.get("pr_url"))
    outcome_meta["user_turn_count"] = user_turn_count
    outcome_meta["continuation_count"] = continuation_count
    outcome_meta["feedback_count"] = feedback_count
    outcome_meta["collaboration_trace_count"] = len(collaboration_trace)
    outcome_meta["verified_collaboration_trace_count"] = len(machine_trace)
    outcome_meta["orchestration_step_count"] = len(orchestration_steps)
    outcome_meta["planner_steps"] = plan_count
    outcome_meta["critic_steps"] = critique_count
    outcome_meta["executor_steps"] = execution_count
    outcome_meta["synthesis_steps"] = synthesis_count
    outcome_meta["principal_bound"] = bool(principal_id)
    outcome_meta["principal_memory_count"] = memory_count
    outcome_meta["pending_reminder_count"] = pending_reminder_count
    if feedback_average is not None:
        outcome_meta["feedback_average"] = feedback_average
    else:
        outcome_meta.pop("feedback_average", None)
    if last_feedback_label:
        outcome_meta["last_feedback_label"] = last_feedback_label
    else:
        outcome_meta.pop("last_feedback_label", None)
    if evaluation_meta.get("last_category"):
        outcome_meta["latest_eval_category"] = str(evaluation_meta.get("last_category"))
    else:
        outcome_meta.pop("latest_eval_category", None)
    if blocked_reason:
        outcome_meta["blocked_reason"] = _clip_meta_text(blocked_reason, 280)
    else:
        outcome_meta.pop("blocked_reason", None)
    if failed_phase:
        outcome_meta["failed_orchestration_phase"] = failed_phase
    else:
        outcome_meta.pop("failed_orchestration_phase", None)
    outcome_meta["ux_flags"] = ux_flags
    outcome_meta["optimization_candidate"] = bool(ux_flags)
    if optimization_meta.get("last_run_status"):
        outcome_meta["optimizer_last_run_status"] = str(optimization_meta.get("last_run_status"))
    elif proj.task_id.startswith("tsk_system"):
        outcome_meta.pop("optimizer_last_run_status", None)


@dataclass
class CommandEnvelope:
    command_type: CommandType
    channel: Channel
    payload: Dict[str, Any]
    task_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    external_id: Optional[str] = None

    def resolved_idempotency_key(self) -> str:
        if self.idempotency_key and str(self.idempotency_key).strip():
            return str(self.idempotency_key).strip()
        ext = self.external_id or ""
        return normalize_idempotency_base(
            self.channel.value, ext, self.command_type.value
        )


@dataclass
class TaskProjection:
    task_id: str
    status: TaskStatus
    channel: str
    summary: str = ""
    cursor_agent_id: Optional[str] = None
    last_error: Optional[str] = None
    seq_applied: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "channel": self.channel,
            "summary": self.summary,
            "cursor_agent_id": self.cursor_agent_id,
            "last_error": self.last_error,
            "seq_applied": self.seq_applied,
            "meta": self.meta,
        }


def legal_task_transition(
    current: TaskStatus, event: EventType
) -> Tuple[bool, Optional[TaskStatus]]:
    """Return (ok, new_status_if_changed). None new_status means unchanged."""
    if event == EventType.TASK_CREATED:
        return current == TaskStatus.CREATED or current == TaskStatus.QUEUED, None
    if event == EventType.JOB_QUEUED:
        if current in (TaskStatus.CREATED,):
            return True, TaskStatus.QUEUED
        if current in (TaskStatus.QUEUED, TaskStatus.RUNNING):
            return True, None
        return False, None
    if event == EventType.JOB_STARTED:
        if current in (TaskStatus.CREATED, TaskStatus.QUEUED):
            return True, TaskStatus.RUNNING
        if current == TaskStatus.RUNNING:
            return True, None
        return False, None
    if event == EventType.JOB_COMPLETED:
        if current in (TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.COMPLETED):
            return False, None
        return True, TaskStatus.COMPLETED
    if event == EventType.ASSISTANT_REPLIED:
        if current in (TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.COMPLETED):
            return False, None
        return True, TaskStatus.COMPLETED
    if event == EventType.JOB_FAILED:
        if current in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            return False, None
        return True, TaskStatus.FAILED
    if event == EventType.HUMAN_APPROVAL_REQUIRED:
        return current not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED), (
            TaskStatus.AWAITING_APPROVAL
        )
    if event in (
        EventType.USER_MESSAGE,
        EventType.USER_FEEDBACK,
        EventType.COMMAND_RECEIVED,
        EventType.EXTERNAL_REF,
        EventType.ORCHESTRATION_STEP,
        EventType.PRINCIPAL_LINKED,
        EventType.PRINCIPAL_MEMORY_SAVED,
        EventType.PRINCIPAL_PREFERENCE_UPDATED,
        EventType.REMINDER_CREATED,
        EventType.REMINDER_TRIGGERED,
        EventType.REMINDER_DELIVERED,
        EventType.REMINDER_FAILED,
        EventType.EVALUATION_RECORDED,
        EventType.OPTIMIZATION_PROPOSAL,
        EventType.OPTIMIZATION_RUN_STARTED,
        EventType.OPTIMIZATION_RUN_COMPLETED,
        EventType.OPTIMIZATION_RUN_FAILED,
        EventType.REGRESSION_RECORDED,
        EventType.LOCAL_AUTO_HEAL_STARTED,
        EventType.LOCAL_AUTO_HEAL_COMPLETED,
        EventType.LOCAL_AUTO_HEAL_FAILED,
    ):
        return True, None
    if event == EventType.JOB_PROGRESS:
        return current in (
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
            TaskStatus.AWAITING_APPROVAL,
        ), None
    if event == EventType.COMMAND_DEDUPED:
        return True, None
    if event in (
        EventType.CAPABILITY_SNAPSHOT,
        EventType.KILL_SWITCH_ENGAGED,
        EventType.KILL_SWITCH_RELEASED,
    ):
        return True, None
    return True, None


def fold_projection(
    proj: TaskProjection, event_type: EventType, payload: Dict[str, Any]
) -> None:
    """Mutate projection in place from a single event."""
    execution_meta = proj.meta.get("execution")
    cursor_meta = proj.meta.get("cursor")
    openclaw_meta = proj.meta.get("openclaw")
    ok, new_st = legal_task_transition(proj.status, event_type)
    if not ok:
        proj.meta.setdefault("warnings", []).append(
            f"ignored_illegal_transition:{event_type.value}:from:{proj.status.value}"
        )
        return
    if new_st is not None:
        proj.status = new_st
    if event_type == EventType.TASK_CREATED:
        proj.summary = str(payload.get("summary") or proj.summary or "")[:500]
    if event_type == EventType.USER_MESSAGE:
        routing_text = str(payload.get("routing_text") or "").strip()
        snippet = str(routing_text or payload.get("text") or "")[:200]
        if snippet:
            proj.summary = snippet
        analytics_meta = _ensure_meta_dict(proj.meta, "analytics")
        analytics_meta["user_turn_count"] = int(analytics_meta.get("user_turn_count") or 0) + 1
        if payload.get("channel") == Channel.TELEGRAM.value:
            telegram_meta = proj.meta.setdefault("telegram", {})
            chunk_for_acc = routing_text or str(payload.get("text") or "").strip()
            if chunk_for_acc:
                prior_acc = str(telegram_meta.get("accumulated_prompt") or "").strip()
                if prior_acc:
                    telegram_meta["accumulated_prompt"] = prior_acc + "\n\n" + chunk_for_acc
                else:
                    telegram_meta["accumulated_prompt"] = chunk_for_acc
            if payload.get("telegram_continuation"):
                telegram_meta["continuation_count"] = int(
                    telegram_meta.get("continuation_count") or 0
                ) + 1
            # Anchor for Telegram reply threading: keep status updates under the first user message.
            if payload.get("message_id") is not None and telegram_meta.get(
                "first_user_message_id"
            ) is None:
                telegram_meta["first_user_message_id"] = payload.get("message_id")
            for src_key, dst_key in (
                ("chat_id", "chat_id"),
                ("chat_type", "chat_type"),
                ("message_id", "message_id"),
                ("message_thread_id", "message_thread_id"),
                ("from_user", "from_user"),
                ("from_username", "from_username"),
            ):
                if payload.get(src_key) is not None:
                    telegram_meta[dst_key] = payload.get(src_key)
            if payload.get("routing_hint"):
                telegram_meta["routing_hint"] = str(payload.get("routing_hint"))
            mention_targets = payload.get("mention_targets")
            if isinstance(mention_targets, list):
                telegram_meta["mention_targets"] = [str(v) for v in mention_targets[:5]]
            model_mentions = payload.get("model_mentions")
            if isinstance(model_mentions, list):
                telegram_meta["model_mentions"] = [str(v) for v in model_mentions[:5]]
            if payload.get("preferred_model_family"):
                telegram_meta["preferred_model_family"] = str(payload.get("preferred_model_family"))
            if payload.get("preferred_model_label"):
                telegram_meta["preferred_model_label"] = str(payload.get("preferred_model_label"))
            if payload.get("collaboration_mode"):
                telegram_meta["collaboration_mode"] = str(payload.get("collaboration_mode"))
            if payload.get("visibility_mode"):
                telegram_meta["visibility_mode"] = str(payload.get("visibility_mode"))
            telegram_meta["requested_capability"] = _derive_requested_capability(payload)
            if routing_text:
                telegram_meta["routing_text"] = routing_text[:500]
            if snippet:
                telegram_meta["last_text"] = snippet
        if payload.get("channel") == Channel.ALEXA.value:
            alexa_meta = proj.meta.setdefault("alexa", {})
            for src_key, dst_key in (
                ("session_id", "session_id"),
                ("request_id", "request_id"),
                ("intent_name", "intent_name"),
                ("locale", "locale"),
                ("user_id", "user_id"),
                ("device_id", "device_id"),
            ):
                if payload.get(src_key):
                    alexa_meta[dst_key] = str(payload.get(src_key))
            if routing_text:
                alexa_meta["routing_text"] = routing_text[:500]
            if snippet:
                alexa_meta["last_text"] = snippet
        if payload.get("principal_id"):
            identity_meta = _ensure_meta_dict(proj.meta, "identity")
            identity_meta["principal_id"] = str(payload.get("principal_id"))
            if payload.get("channel"):
                channels = identity_meta.get("channels")
                if not isinstance(channels, list):
                    channels = []
                    identity_meta["channels"] = channels
                channel_value = str(payload.get("channel"))
                if channel_value and channel_value not in channels:
                    channels.append(channel_value)
    if event_type == EventType.ASSISTANT_REPLIED:
        assistant_meta = proj.meta.setdefault("assistant", {})
        assistant_meta["route"] = str(payload.get("route") or "direct")
        if payload.get("reason"):
            assistant_meta["reason"] = str(payload.get("reason"))
        reply_text = str(payload.get("text") or "").strip()
        if reply_text:
            assistant_meta["last_reply"] = reply_text[:2000]
            proj.summary = reply_text[:500]
    if event_type == EventType.JOB_QUEUED:
        execution_meta = proj.meta.setdefault("execution", {})
        if payload.get("execution_lane"):
            execution_meta["lane"] = str(payload["execution_lane"])
        if payload.get("runner"):
            execution_meta["runner"] = str(payload["runner"])
        if payload.get("route_reason"):
            execution_meta["route_reason"] = str(payload["route_reason"])
        if payload.get("source"):
            execution_meta["source"] = str(payload["source"])
        if payload.get("routing_hint"):
            execution_meta["routing_hint"] = str(payload["routing_hint"])
        if payload.get("collaboration_mode"):
            execution_meta["collaboration_mode"] = str(payload["collaboration_mode"])
        if payload.get("visibility_mode"):
            execution_meta["visibility_mode"] = str(payload["visibility_mode"])
        execution_meta["requested_capability"] = _derive_requested_capability(payload)
        mention_targets = payload.get("mention_targets")
        if isinstance(mention_targets, list):
            execution_meta["mention_targets"] = [str(v) for v in mention_targets[:5]]
        model_mentions = payload.get("model_mentions")
        if isinstance(model_mentions, list):
            execution_meta["model_mentions"] = [str(v) for v in model_mentions[:5]]
        if payload.get("preferred_model_family"):
            execution_meta["preferred_model_family"] = str(payload["preferred_model_family"])
        if payload.get("preferred_model_label"):
            execution_meta["preferred_model_label"] = str(payload["preferred_model_label"])
        if payload.get("cursor_agent_id"):
            proj.cursor_agent_id = str(payload["cursor_agent_id"])
        cursor_meta = proj.meta.setdefault("cursor", {})
        cursor_meta["kind"] = str(payload.get("kind") or "cursor")
        if payload.get("prompt_excerpt"):
            cursor_meta["prompt_excerpt"] = str(payload["prompt_excerpt"])[:300]
        if payload.get("kind") == "openclaw":
            openclaw_meta = proj.meta.setdefault("openclaw", {})
            openclaw_meta["kind"] = "openclaw"
    if event_type == EventType.JOB_STARTED:
        execution_meta = proj.meta.setdefault("execution", {})
        if payload.get("backend"):
            execution_meta["backend"] = str(payload["backend"])
        if payload.get("execution_lane"):
            execution_meta["lane"] = str(payload["execution_lane"])
        if payload.get("runner"):
            execution_meta["runner"] = str(payload["runner"])
        if payload.get("delegated_to_cursor") is not None:
            execution_meta["delegated_to_cursor"] = bool(payload.get("delegated_to_cursor"))
        if payload.get("runner") == "cursor" or payload.get("backend") == "cursor":
            cursor_meta = proj.meta.setdefault("cursor", {})
            cursor_meta["kind"] = "cursor"
        elif payload.get("runner") == "openclaw" or payload.get("backend") == "openclaw":
            cursor_meta = proj.meta.setdefault("cursor", {})
            cursor_meta.setdefault("kind", "openclaw")
        if payload.get("cursor_agent_id"):
            proj.cursor_agent_id = str(payload["cursor_agent_id"])
            cursor_meta = proj.meta.setdefault("cursor", {})
            cursor_meta["cursor_agent_id"] = str(payload["cursor_agent_id"])
        cursor_meta = proj.meta.setdefault("cursor", {})
        if payload.get("agent_url"):
            cursor_meta["agent_url"] = str(payload["agent_url"])
        if payload.get("pr_url"):
            cursor_meta["pr_url"] = str(payload["pr_url"])
        if payload.get("backend"):
            cursor_meta["backend"] = str(payload["backend"])
        if payload.get("openclaw_run_id"):
            openclaw_meta = proj.meta.setdefault("openclaw", {})
            openclaw_meta["run_id"] = str(payload["openclaw_run_id"])
        if payload.get("openclaw_session_id"):
            openclaw_meta = proj.meta.setdefault("openclaw", {})
            openclaw_meta["session_id"] = str(payload["openclaw_session_id"])
        if payload.get("provider"):
            openclaw_meta = proj.meta.setdefault("openclaw", {})
            openclaw_meta["provider"] = str(payload["provider"])
        if payload.get("model"):
            openclaw_meta = proj.meta.setdefault("openclaw", {})
            openclaw_meta["model"] = str(payload["model"])
        if payload.get("visibility_mode"):
            execution_meta["visibility_mode"] = str(payload["visibility_mode"])
        if payload.get("preferred_model_family"):
            execution_meta["preferred_model_family"] = str(payload["preferred_model_family"])
        if payload.get("preferred_model_label"):
            execution_meta["preferred_model_label"] = str(payload["preferred_model_label"])
        if payload.get("user_safe_error"):
            execution_meta["user_safe_error"] = _clip_meta_text(payload.get("user_safe_error"), 500)
        if payload.get("internal_error"):
            execution_meta["internal_error"] = _clip_meta_text(payload.get("internal_error"), 1200)
        _fold_openclaw_contract_meta(proj, payload)
    if event_type == EventType.JOB_PROGRESS:
        execution_meta = proj.meta.setdefault("execution", {})
        if payload.get("backend"):
            execution_meta["backend"] = str(payload["backend"])
        if payload.get("execution_lane"):
            execution_meta["lane"] = str(payload["execution_lane"])
        if payload.get("runner"):
            execution_meta["runner"] = str(payload["runner"])
        if payload.get("delegated_to_cursor") is not None:
            execution_meta["delegated_to_cursor"] = bool(payload.get("delegated_to_cursor"))
        if payload.get("runner") == "cursor" or payload.get("backend") == "cursor":
            cursor_meta = proj.meta.setdefault("cursor", {})
            cursor_meta["kind"] = "cursor"
        elif payload.get("runner") == "openclaw" or payload.get("backend") == "openclaw":
            cursor_meta = proj.meta.setdefault("cursor", {})
            cursor_meta.setdefault("kind", "openclaw")
        if payload.get("cursor_agent_id"):
            proj.cursor_agent_id = str(payload["cursor_agent_id"])
            cursor_meta = proj.meta.setdefault("cursor", {})
            cursor_meta["cursor_agent_id"] = str(payload["cursor_agent_id"])
        cursor_meta = proj.meta.setdefault("cursor", {})
        if payload.get("agent_url"):
            cursor_meta["agent_url"] = str(payload["agent_url"])
        if payload.get("pr_url"):
            cursor_meta["pr_url"] = str(payload["pr_url"])
        if payload.get("provider"):
            openclaw_meta = proj.meta.setdefault("openclaw", {})
            openclaw_meta["provider"] = str(payload["provider"])
        if payload.get("model"):
            openclaw_meta = proj.meta.setdefault("openclaw", {})
            openclaw_meta["model"] = str(payload["model"])
        if payload.get("visibility_mode"):
            execution_meta["visibility_mode"] = str(payload["visibility_mode"])
        if payload.get("preferred_model_family"):
            execution_meta["preferred_model_family"] = str(payload["preferred_model_family"])
        if payload.get("preferred_model_label"):
            execution_meta["preferred_model_label"] = str(payload["preferred_model_label"])
        if payload.get("user_safe_error"):
            execution_meta["user_safe_error"] = _clip_meta_text(payload.get("user_safe_error"), 500)
        if payload.get("internal_error"):
            execution_meta["internal_error"] = _clip_meta_text(payload.get("internal_error"), 1200)
        _fold_openclaw_contract_meta(proj, payload)
    if event_type == EventType.JOB_FAILED:
        proj.last_error = str(payload.get("error") or payload.get("message") or "failed")[:2000]
        execution_meta = proj.meta.setdefault("execution", {})
        if payload.get("backend"):
            execution_meta["backend"] = str(payload["backend"])
        if payload.get("execution_lane"):
            execution_meta["lane"] = str(payload["execution_lane"])
        if payload.get("runner"):
            execution_meta["runner"] = str(payload["runner"])
        if payload.get("delegated_to_cursor") is not None:
            execution_meta["delegated_to_cursor"] = bool(payload.get("delegated_to_cursor"))
        if payload.get("runner") == "cursor" or payload.get("backend") == "cursor":
            cursor_meta = proj.meta.setdefault("cursor", {})
            cursor_meta["kind"] = "cursor"
        elif payload.get("runner") == "openclaw" or payload.get("backend") == "openclaw":
            cursor_meta = proj.meta.setdefault("cursor", {})
            cursor_meta.setdefault("kind", "openclaw")
        if payload.get("cursor_agent_id"):
            proj.cursor_agent_id = str(payload["cursor_agent_id"])
            cursor_meta = proj.meta.setdefault("cursor", {})
            cursor_meta["cursor_agent_id"] = str(payload["cursor_agent_id"])
        cursor_meta = proj.meta.setdefault("cursor", {})
        if payload.get("agent_url"):
            cursor_meta["agent_url"] = str(payload["agent_url"])
        if payload.get("pr_url"):
            cursor_meta["pr_url"] = str(payload["pr_url"])
        if payload.get("openclaw_run_id"):
            openclaw_meta = proj.meta.setdefault("openclaw", {})
            openclaw_meta["run_id"] = str(payload["openclaw_run_id"])
        if payload.get("openclaw_session_id"):
            openclaw_meta = proj.meta.setdefault("openclaw", {})
            openclaw_meta["session_id"] = str(payload["openclaw_session_id"])
        if payload.get("provider"):
            openclaw_meta = proj.meta.setdefault("openclaw", {})
            openclaw_meta["provider"] = str(payload["provider"])
        if payload.get("model"):
            openclaw_meta = proj.meta.setdefault("openclaw", {})
            openclaw_meta["model"] = str(payload["model"])
        if payload.get("visibility_mode"):
            execution_meta["visibility_mode"] = str(payload["visibility_mode"])
        if payload.get("preferred_model_family"):
            execution_meta["preferred_model_family"] = str(payload["preferred_model_family"])
        if payload.get("preferred_model_label"):
            execution_meta["preferred_model_label"] = str(payload["preferred_model_label"])
        if payload.get("user_safe_error"):
            execution_meta["user_safe_error"] = _clip_meta_text(payload.get("user_safe_error"), 500)
        if payload.get("internal_error"):
            execution_meta["internal_error"] = _clip_meta_text(payload.get("internal_error"), 1200)
        elif payload.get("message"):
            execution_meta["internal_error"] = _clip_meta_text(payload.get("message"), 1200)
        _fold_openclaw_contract_meta(proj, payload)
    if event_type == EventType.JOB_COMPLETED:
        proj.last_error = None
        if payload.get("summary"):
            proj.summary = str(payload["summary"])[:500]
        execution_meta = proj.meta.setdefault("execution", {})
        if payload.get("backend"):
            execution_meta["backend"] = str(payload["backend"])
        if payload.get("execution_lane"):
            execution_meta["lane"] = str(payload["execution_lane"])
        if payload.get("runner"):
            execution_meta["runner"] = str(payload["runner"])
        if payload.get("delegated_to_cursor") is not None:
            execution_meta["delegated_to_cursor"] = bool(payload.get("delegated_to_cursor"))
        if payload.get("runner") == "cursor" or payload.get("backend") == "cursor":
            cursor_meta = proj.meta.setdefault("cursor", {})
            cursor_meta["kind"] = "cursor"
        elif payload.get("runner") == "openclaw" or payload.get("backend") == "openclaw":
            cursor_meta = proj.meta.setdefault("cursor", {})
            cursor_meta.setdefault("kind", "openclaw")
        if payload.get("cursor_agent_id"):
            proj.cursor_agent_id = str(payload["cursor_agent_id"])
            cursor_meta = proj.meta.setdefault("cursor", {})
            cursor_meta["cursor_agent_id"] = str(payload["cursor_agent_id"])
        cursor_meta = proj.meta.setdefault("cursor", {})
        if payload.get("agent_url"):
            cursor_meta["agent_url"] = str(payload["agent_url"])
        if payload.get("pr_url"):
            cursor_meta["pr_url"] = str(payload["pr_url"])
        if payload.get("openclaw_run_id"):
            openclaw_meta = proj.meta.setdefault("openclaw", {})
            openclaw_meta["run_id"] = str(payload["openclaw_run_id"])
        if payload.get("openclaw_session_id"):
            openclaw_meta = proj.meta.setdefault("openclaw", {})
            openclaw_meta["session_id"] = str(payload["openclaw_session_id"])
        if payload.get("provider"):
            openclaw_meta = proj.meta.setdefault("openclaw", {})
            openclaw_meta["provider"] = str(payload["provider"])
        if payload.get("model"):
            openclaw_meta = proj.meta.setdefault("openclaw", {})
            openclaw_meta["model"] = str(payload["model"])
        if payload.get("visibility_mode"):
            execution_meta["visibility_mode"] = str(payload["visibility_mode"])
        if payload.get("preferred_model_family"):
            execution_meta["preferred_model_family"] = str(payload["preferred_model_family"])
        if payload.get("preferred_model_label"):
            execution_meta["preferred_model_label"] = str(payload["preferred_model_label"])
        if payload.get("user_safe_error"):
            execution_meta["user_safe_error"] = _clip_meta_text(payload.get("user_safe_error"), 500)
        if payload.get("internal_error"):
            execution_meta["internal_error"] = _clip_meta_text(payload.get("internal_error"), 1200)
        _fold_openclaw_contract_meta(proj, payload)
    if event_type == EventType.ORCHESTRATION_STEP:
        _fold_orchestration_step_meta(proj, payload)
    if event_type == EventType.PRINCIPAL_LINKED:
        identity_meta = _ensure_meta_dict(proj.meta, "identity")
        if payload.get("principal_id"):
            identity_meta["principal_id"] = str(payload.get("principal_id"))
        if payload.get("channel"):
            channels = identity_meta.get("channels")
            if not isinstance(channels, list):
                channels = []
                identity_meta["channels"] = channels
            channel_value = str(payload.get("channel"))
            if channel_value and channel_value not in channels:
                channels.append(channel_value)
    if event_type == EventType.PRINCIPAL_MEMORY_SAVED:
        identity_meta = _ensure_meta_dict(proj.meta, "identity")
        if payload.get("principal_id"):
            identity_meta["principal_id"] = str(payload.get("principal_id"))
        identity_meta["memory_count"] = int(identity_meta.get("memory_count") or 0) + 1
        if payload.get("kind"):
            identity_meta["last_memory_kind"] = str(payload.get("kind"))
        if payload.get("content"):
            identity_meta["last_memory"] = _clip_meta_text(payload.get("content"), 240)
    if event_type == EventType.PRINCIPAL_PREFERENCE_UPDATED:
        identity_meta = _ensure_meta_dict(proj.meta, "identity")
        if payload.get("principal_id"):
            identity_meta["principal_id"] = str(payload.get("principal_id"))
        prefs = identity_meta.get("preferences")
        if not isinstance(prefs, dict):
            prefs = {}
            identity_meta["preferences"] = prefs
        if payload.get("key"):
            prefs[str(payload.get("key"))] = payload.get("value")
    if event_type == EventType.REMINDER_CREATED:
        proactive_meta = _ensure_meta_dict(proj.meta, "proactive")
        proactive_meta["pending_reminder_count"] = int(
            proactive_meta.get("pending_reminder_count") or 0
        ) + 1
        if payload.get("reminder_id"):
            proactive_meta["last_reminder_id"] = str(payload.get("reminder_id"))
        if payload.get("message"):
            proactive_meta["last_reminder_message"] = _clip_meta_text(payload.get("message"), 240)
        if payload.get("due_at") is not None:
            try:
                proactive_meta["last_reminder_due_at"] = float(payload.get("due_at"))
            except (TypeError, ValueError):
                pass
        proactive_meta["last_reminder_status"] = "scheduled"
    if event_type == EventType.REMINDER_TRIGGERED:
        proactive_meta = _ensure_meta_dict(proj.meta, "proactive")
        proactive_meta["triggered_reminder_count"] = int(
            proactive_meta.get("triggered_reminder_count") or 0
        ) + 1
        proactive_meta["last_reminder_status"] = "triggered"
    if event_type == EventType.REMINDER_DELIVERED:
        proactive_meta = _ensure_meta_dict(proj.meta, "proactive")
        proactive_meta["delivered_reminder_count"] = int(
            proactive_meta.get("delivered_reminder_count") or 0
        ) + 1
        proactive_meta["pending_reminder_count"] = max(
            0, int(proactive_meta.get("pending_reminder_count") or 0) - 1
        )
        proactive_meta["last_reminder_status"] = "delivered"
    if event_type == EventType.REMINDER_FAILED:
        proactive_meta = _ensure_meta_dict(proj.meta, "proactive")
        proactive_meta["failed_reminder_count"] = int(
            proactive_meta.get("failed_reminder_count") or 0
        ) + 1
        proactive_meta["last_reminder_status"] = "failed"
    if event_type == EventType.USER_FEEDBACK:
        feedback_meta = _ensure_meta_dict(proj.meta, "feedback")
        feedback_meta["count"] = int(feedback_meta.get("count") or 0) + 1
        if payload.get("label"):
            feedback_meta["last_label"] = str(payload.get("label"))
        if payload.get("comment"):
            feedback_meta["last_comment"] = _clip_meta_text(payload.get("comment"), 800)
        if payload.get("source"):
            feedback_meta["last_source"] = str(payload.get("source"))
        if payload.get("score") is not None:
            try:
                score = float(payload.get("score"))
            except (TypeError, ValueError):
                score = 0.0
            feedback_meta["score_total"] = float(feedback_meta.get("score_total") or 0.0) + score
            feedback_meta["last_score"] = score
    if event_type == EventType.EVALUATION_RECORDED:
        evaluation_meta = _ensure_meta_dict(proj.meta, "evaluation")
        evaluation_meta["count"] = int(evaluation_meta.get("count") or 0) + 1
        if payload.get("category"):
            category = str(payload.get("category"))
            evaluation_meta["last_category"] = category
            categories = evaluation_meta.get("categories")
            if not isinstance(categories, dict):
                categories = {}
                evaluation_meta["categories"] = categories
            categories[category] = int(categories.get(category) or 0) + int(payload.get("count") or 1)
        if payload.get("severity"):
            evaluation_meta["last_severity"] = str(payload.get("severity"))
        if payload.get("summary"):
            evaluation_meta["last_summary"] = _clip_meta_text(payload.get("summary"), 800)
    if event_type == EventType.OPTIMIZATION_PROPOSAL:
        optimization_meta = _ensure_meta_dict(proj.meta, "optimization")
        optimization_meta["proposal_count"] = int(optimization_meta.get("proposal_count") or 0) + 1
        if payload.get("proposal_id"):
            optimization_meta["last_proposal_id"] = str(payload.get("proposal_id"))
        if payload.get("title"):
            optimization_meta["last_proposal_title"] = _clip_meta_text(payload.get("title"), 240)
        if payload.get("category"):
            optimization_meta["last_proposal_category"] = str(payload.get("category"))
        if payload.get("status"):
            optimization_meta["last_proposal_status"] = str(payload.get("status"))
    if event_type == EventType.OPTIMIZATION_RUN_STARTED:
        optimization_meta = _ensure_meta_dict(proj.meta, "optimization")
        optimization_meta["run_count"] = int(optimization_meta.get("run_count") or 0) + 1
        if payload.get("run_id"):
            optimization_meta["last_run_id"] = str(payload.get("run_id"))
        optimization_meta["last_run_status"] = "running"
        if payload.get("analysis_mode"):
            optimization_meta["last_analysis_mode"] = str(payload.get("analysis_mode"))
    if event_type == EventType.OPTIMIZATION_RUN_COMPLETED:
        optimization_meta = _ensure_meta_dict(proj.meta, "optimization")
        if payload.get("run_id"):
            optimization_meta["last_run_id"] = str(payload.get("run_id"))
        optimization_meta["last_run_status"] = "completed"
        if payload.get("gate_allowed") is not None:
            optimization_meta["last_gate_allowed"] = bool(payload.get("gate_allowed"))
        if payload.get("proposal_count") is not None:
            optimization_meta["last_run_proposal_count"] = int(payload.get("proposal_count") or 0)
        if payload.get("finding_count") is not None:
            optimization_meta["last_run_finding_count"] = int(payload.get("finding_count") or 0)
    if event_type == EventType.OPTIMIZATION_RUN_FAILED:
        optimization_meta = _ensure_meta_dict(proj.meta, "optimization")
        if payload.get("run_id"):
            optimization_meta["last_run_id"] = str(payload.get("run_id"))
        optimization_meta["last_run_status"] = "failed"
        if payload.get("error"):
            optimization_meta["last_run_error"] = _clip_meta_text(payload.get("error"), 800)
    if event_type == EventType.REGRESSION_RECORDED:
        optimization_meta = _ensure_meta_dict(proj.meta, "optimization")
        try:
            optimization_meta["last_regression_total"] = int(payload.get("total") or 0)
        except (TypeError, ValueError):
            optimization_meta["last_regression_total"] = 0
        optimization_meta["last_regression_passed"] = bool(payload.get("passed"))
        if payload.get("command"):
            optimization_meta["last_regression_command"] = _clip_meta_text(payload.get("command"), 240)
    if event_type == EventType.LOCAL_AUTO_HEAL_STARTED:
        optimization_meta = _ensure_meta_dict(proj.meta, "optimization")
        optimization_meta["last_auto_heal_status"] = "running"
        optimization_meta["auto_heal_run_count"] = int(
            optimization_meta.get("auto_heal_run_count") or 0
        ) + 1
        if payload.get("proposal_id"):
            optimization_meta["last_auto_heal_proposal_id"] = str(payload.get("proposal_id"))
    if event_type == EventType.LOCAL_AUTO_HEAL_COMPLETED:
        optimization_meta = _ensure_meta_dict(proj.meta, "optimization")
        optimization_meta["last_auto_heal_status"] = "completed"
        optimization_meta["auto_heal_success_count"] = int(
            optimization_meta.get("auto_heal_success_count") or 0
        ) + 1
        if payload.get("proposal_id"):
            optimization_meta["last_auto_heal_proposal_id"] = str(payload.get("proposal_id"))
        if payload.get("branch"):
            optimization_meta["last_auto_heal_branch"] = str(payload.get("branch"))
        if payload.get("agent_url"):
            optimization_meta["last_auto_heal_agent_url"] = str(payload.get("agent_url"))
        if payload.get("pr_url"):
            optimization_meta["last_auto_heal_pr_url"] = str(payload.get("pr_url"))
    if event_type == EventType.LOCAL_AUTO_HEAL_FAILED:
        optimization_meta = _ensure_meta_dict(proj.meta, "optimization")
        optimization_meta["last_auto_heal_status"] = "failed"
        optimization_meta["auto_heal_failure_count"] = int(
            optimization_meta.get("auto_heal_failure_count") or 0
        ) + 1
        if payload.get("proposal_id"):
            optimization_meta["last_auto_heal_proposal_id"] = str(payload.get("proposal_id"))
        if payload.get("error"):
            optimization_meta["last_auto_heal_error"] = _clip_meta_text(payload.get("error"), 800)
    if event_type == EventType.CAPABILITY_SNAPSHOT:
        proj.meta["last_capability_excerpt"] = str(payload.get("summary_json_excerpt", ""))[:500]
    if event_type in (EventType.KILL_SWITCH_ENGAGED, EventType.KILL_SWITCH_RELEASED):
        proj.meta["kill_switch_last"] = event_type.value
    _refresh_outcome_meta(proj)

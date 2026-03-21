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
    CREATE_CURSOR_JOB = "CreateCursorJob"
    CURSOR_FOLLOWUP = "CursorFollowup"
    CURSOR_STOP = "CursorStop"
    REPORT_CURSOR_EVENT = "ReportCursorEvent"  # lifecycle from cursor_openclaw / handoff
    ALEXA_UTTERANCE = "AlexaUtterance"
    PUBLISH_CAPABILITY_SNAPSHOT = "PublishCapabilitySnapshot"
    KILL_SWITCH_ENGAGE = "KillSwitchEngage"
    KILL_SWITCH_RELEASE = "KillSwitchRelease"


# --- Event types (facts, append-only) ---


class EventType(str, Enum):
    COMMAND_RECEIVED = "CommandReceived"
    COMMAND_DEDUPED = "CommandDeduped"
    TASK_CREATED = "TaskCreated"
    USER_MESSAGE = "UserMessage"
    JOB_QUEUED = "JobQueued"
    JOB_STARTED = "JobStarted"
    JOB_PROGRESS = "JobProgress"
    JOB_COMPLETED = "JobCompleted"
    JOB_FAILED = "JobFailed"
    HUMAN_APPROVAL_REQUIRED = "HumanApprovalRequired"
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


def normalize_idempotency_base(channel: str, external_id: str, command_type: str) -> str:
    raw = f"{channel}|{external_id}|{command_type}"
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
    if event == EventType.JOB_FAILED:
        if current in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            return False, None
        return True, TaskStatus.FAILED
    if event == EventType.HUMAN_APPROVAL_REQUIRED:
        return current not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED), (
            TaskStatus.AWAITING_APPROVAL
        )
    if event in (EventType.USER_MESSAGE, EventType.COMMAND_RECEIVED, EventType.EXTERNAL_REF):
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
        snippet = str(payload.get("text") or "")[:200]
        if snippet:
            proj.summary = snippet
    if event_type == EventType.JOB_QUEUED:
        if payload.get("cursor_agent_id"):
            proj.cursor_agent_id = str(payload["cursor_agent_id"])
    if event_type == EventType.JOB_STARTED:
        if payload.get("cursor_agent_id"):
            proj.cursor_agent_id = str(payload["cursor_agent_id"])
    if event_type == EventType.JOB_FAILED:
        proj.last_error = str(payload.get("error") or payload.get("message") or "failed")[:2000]
    if event_type == EventType.JOB_COMPLETED:
        proj.last_error = None
        if payload.get("summary"):
            proj.summary = str(payload["summary"])[:500]
    if event_type == EventType.CAPABILITY_SNAPSHOT:
        proj.meta["last_capability_excerpt"] = str(payload.get("summary_json_excerpt", ""))[:500]
    if event_type in (EventType.KILL_SWITCH_ENGAGED, EventType.KILL_SWITCH_RELEASED):
        proj.meta["kill_switch_last"] = event_type.value

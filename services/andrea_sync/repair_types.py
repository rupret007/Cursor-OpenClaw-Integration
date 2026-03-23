"""Shared data structures for Andrea's incident-driven repair pipeline."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

INCIDENT_STATES = (
    "detected",
    "triaged",
    "patching_primary",
    "verifying_primary",
    "patching_challenger",
    "verifying_challenger",
    "planning_escalation",
    "cursor_handoff_ready",
    "resolved",
    "rolled_back",
    "human_review_required",
)


def _clip(value: Any, limit: int = 800) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _dedupe_text_items(value: Any, *, limit: int = 12, item_limit: int = 240) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for raw in value:
        item = _clip(raw, item_limit)
        if item and item not in out:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _normalize_json_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_history_items(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    for raw in value[:32]:
        if not isinstance(raw, dict):
            continue
        entry = {
            "state": str(raw.get("state") or "").strip(),
            "ts": float(raw.get("ts") or time.time()),
            "reason": _clip(raw.get("reason") or "", 240),
            "model_used": _clip(raw.get("model_used") or "", 160),
            "attempt_id": str(raw.get("attempt_id") or "").strip(),
            "attempt_number": int(raw.get("attempt_number") or 0),
            "extra": _normalize_json_dict(raw.get("extra")),
        }
        if not entry["state"]:
            continue
        out.append(entry)
    return out


def new_incident_id() -> str:
    return f"inc_{uuid.uuid4().hex[:16]}"


def new_attempt_id() -> str:
    return f"att_{uuid.uuid4().hex[:16]}"


def new_plan_id() -> str:
    return f"rpl_{uuid.uuid4().hex[:16]}"


@dataclass
class VerificationCheck:
    check_id: str
    label: str
    command: str
    cwd: str
    required: bool = True
    enabled: bool = True
    timeout_seconds: int = 900
    tags: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["tags"] = _dedupe_text_items(self.tags, limit=8, item_limit=80)
        return payload


@dataclass
class Incident:
    incident_id: str
    created_at: float
    updated_at: float
    source: str
    service_name: str
    environment: str
    error_type: str
    summary: str
    stack_trace: str = ""
    failing_tests: List[str] = field(default_factory=list)
    suspected_files: List[str] = field(default_factory=list)
    recent_diff: List[str] = field(default_factory=list)
    triage_confidence: float = 0.0
    safe_to_attempt: bool = False
    attempt_count: int = 0
    current_state: str = "detected"
    history: List[Dict[str, Any]] = field(default_factory=list)
    probable_root_cause: str = ""
    recommended_repair_scope: str = ""
    source_task_id: str = ""
    fingerprint: str = ""
    verification: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return self.current_state

    @status.setter
    def status(self, value: str) -> None:
        self.current_state = str(value or "").strip() or self.current_state

    @property
    def confidence(self) -> float:
        return float(self.triage_confidence)

    @confidence.setter
    def confidence(self, value: float) -> None:
        try:
            self.triage_confidence = float(value)
        except (TypeError, ValueError):
            self.triage_confidence = 0.0

    @property
    def timestamp(self) -> float:
        return float(self.created_at)

    def record_state(
        self,
        state: str,
        *,
        reason: str = "",
        model_used: str = "",
        attempt_id: str = "",
        attempt_number: int = 0,
        extra: Dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> None:
        transition_ts = float(ts or time.time())
        new_state = str(state or "").strip() or self.current_state
        entry = {
            "state": new_state,
            "ts": transition_ts,
            "reason": _clip(reason, 240),
            "model_used": _clip(model_used, 160),
            "attempt_id": str(attempt_id or "").strip(),
            "attempt_number": int(attempt_number or 0),
            "extra": _normalize_json_dict(extra),
        }
        signature = "|".join(
            [
                entry["state"],
                str(entry["attempt_number"]),
                entry["attempt_id"],
                entry["reason"],
                entry["model_used"],
                str(entry["extra"]),
            ]
        )
        if not any(
            signature
            == "|".join(
                [
                    str(existing.get("state") or ""),
                    str(existing.get("attempt_number") or 0),
                    str(existing.get("attempt_id") or ""),
                    str(existing.get("reason") or ""),
                    str(existing.get("model_used") or ""),
                    str(existing.get("extra") or {}),
                ]
            )
            for existing in self.history[-4:]
        ):
            self.history.append(entry)
            if len(self.history) > 32:
                del self.history[:-32]
        self.current_state = new_state
        self.updated_at = transition_ts

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["summary"] = _clip(self.summary, 500)
        payload["stack_trace"] = _clip(self.stack_trace, 2400)
        payload["failing_tests"] = _dedupe_text_items(self.failing_tests, limit=12)
        payload["suspected_files"] = _dedupe_text_items(self.suspected_files, limit=16)
        payload["recent_diff"] = _dedupe_text_items(self.recent_diff, limit=16, item_limit=800)
        payload["history"] = _normalize_history_items(self.history)
        payload["verification"] = _normalize_json_dict(self.verification)
        payload["metadata"] = _normalize_json_dict(self.metadata)
        payload["timestamp"] = float(self.created_at)
        payload["status"] = self.current_state
        payload["confidence"] = float(self.triage_confidence)
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Incident":
        data = dict(payload)
        created_at = float(data.get("created_at") or data.get("timestamp") or time.time())
        updated_at = float(data.get("updated_at") or created_at)
        incident = cls(
            incident_id=str(data.get("incident_id") or new_incident_id()),
            created_at=created_at,
            updated_at=updated_at,
            source=str(data.get("source") or "unknown"),
            service_name=str(data.get("service_name") or "andrea_sync"),
            environment=str(data.get("environment") or "local"),
            error_type=str(data.get("error_type") or "unclear_or_unsafe"),
            summary=_clip(data.get("summary") or "", 500),
            stack_trace=_clip(data.get("stack_trace") or "", 2400),
            failing_tests=_dedupe_text_items(data.get("failing_tests"), limit=12),
            suspected_files=_dedupe_text_items(data.get("suspected_files"), limit=16),
            recent_diff=_dedupe_text_items(data.get("recent_diff"), limit=16, item_limit=800),
            triage_confidence=float(data.get("triage_confidence") or data.get("confidence") or 0.0),
            safe_to_attempt=bool(data.get("safe_to_attempt")),
            attempt_count=int(data.get("attempt_count") or 0),
            current_state=str(data.get("current_state") or data.get("status") or "detected"),
            history=_normalize_history_items(data.get("history")),
            probable_root_cause=_clip(data.get("probable_root_cause") or "", 1200),
            recommended_repair_scope=_clip(data.get("recommended_repair_scope") or "", 500),
            source_task_id=str(data.get("source_task_id") or ""),
            fingerprint=str(data.get("fingerprint") or ""),
            verification=_normalize_json_dict(data.get("verification")),
            metadata=_normalize_json_dict(data.get("metadata")),
        )
        if not incident.history and incident.current_state:
            incident.record_state(
                incident.current_state,
                reason=f"{incident.source or 'unknown'} incident loaded",
                ts=updated_at,
            )
        return incident


@dataclass
class PatchProposal:
    model_used: str
    reasoning_summary: str
    files_touched: List[str] = field(default_factory=list)
    diff: str = ""
    tests_expected: List[str] = field(default_factory=list)
    confidence: float = 0.0
    safe_to_apply: bool = False
    test_change_reason: str = ""
    raw_response: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["reasoning_summary"] = _clip(self.reasoning_summary, 1200)
        payload["files_touched"] = _dedupe_text_items(self.files_touched, limit=16)
        payload["tests_expected"] = _dedupe_text_items(self.tests_expected, limit=12)
        payload["diff"] = _clip(self.diff, 24000)
        payload["test_change_reason"] = _clip(self.test_change_reason, 400)
        payload["raw_response"] = _normalize_json_dict(self.raw_response)
        return payload


@dataclass
class PatchAttempt:
    attempt_id: str
    incident_id: str
    attempt_number: int
    stage: str
    model_used: str
    status: str = "pending"
    prompt_version: str = ""
    files_touched: List[str] = field(default_factory=list)
    diff: str = ""
    reasoning_summary: str = ""
    verification_results: Dict[str, Any] = field(default_factory=dict)
    success: bool = False
    rollback_performed: bool = False
    branch: str = ""
    worktree_path: str = ""
    report_path: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def started_at(self) -> float:
        return float(self.created_at)

    @property
    def completed_at(self) -> float:
        return float(self.updated_at) if self.status in {"completed", "failed", "blocked"} else 0.0

    @property
    def verification_result(self) -> Dict[str, Any]:
        return self.verification_results

    @verification_result.setter
    def verification_result(self, value: Dict[str, Any]) -> None:
        self.verification_results = _normalize_json_dict(value)

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["files_touched"] = _dedupe_text_items(self.files_touched, limit=16)
        payload["diff"] = _clip(self.diff, 24000)
        payload["reasoning_summary"] = _clip(self.reasoning_summary, 1200)
        payload["verification_results"] = _normalize_json_dict(self.verification_results)
        payload["verification_result"] = _normalize_json_dict(self.verification_results)
        payload["error"] = _clip(self.error, 1600)
        payload["metadata"] = _normalize_json_dict(self.metadata)
        payload["started_at"] = float(self.started_at)
        payload["completed_at"] = float(self.completed_at)
        return payload


@dataclass
class RepairPlan:
    plan_id: str
    incident_id: str
    model_used: str
    prompt_version: str
    root_cause: str
    steps: List[str]
    files_to_modify: List[str]
    risks: List[str]
    verification_plan: List[str]
    stop_conditions: List[str]
    cursor_handoff_prompt: str = ""
    cursor_handoff_payload: Dict[str, Any] = field(default_factory=dict)
    status: str = "planned"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def planner_model(self) -> str:
        return self.model_used

    @property
    def repair_steps(self) -> List[str]:
        return list(self.steps)

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["root_cause"] = _clip(self.root_cause, 1600)
        payload["steps"] = _dedupe_text_items(self.steps, limit=16, item_limit=400)
        payload["repair_steps"] = list(payload["steps"])
        payload["files_to_modify"] = _dedupe_text_items(self.files_to_modify, limit=20)
        payload["risks"] = _dedupe_text_items(self.risks, limit=12, item_limit=320)
        payload["verification_plan"] = _dedupe_text_items(
            self.verification_plan, limit=12, item_limit=320
        )
        payload["stop_conditions"] = _dedupe_text_items(
            self.stop_conditions, limit=12, item_limit=320
        )
        payload["cursor_handoff_prompt"] = _clip(self.cursor_handoff_prompt, 12000)
        payload["cursor_handoff_payload"] = _normalize_json_dict(self.cursor_handoff_payload)
        payload["planner_model"] = self.model_used
        payload["prompt_version"] = _clip(self.prompt_version, 120)
        payload["metadata"] = _normalize_json_dict(self.metadata)
        return payload


@dataclass
class RepairBudget:
    max_token_budget: int = 24000
    max_model_invocations: int = 4
    max_elapsed_seconds: float = 1800.0
    max_patch_attempts: int = 2
    max_changed_lines: int = 240
    token_budget_used: int = 0
    model_invocations_used: int = 0
    patch_attempts_used: int = 0
    changed_lines_used: int = 0
    started_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

"""Shared data structures for Andrea's incident-driven repair pipeline."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


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
    timestamp: float
    source: str
    error_type: str
    summary: str
    stack_trace: str = ""
    failing_tests: List[str] = field(default_factory=list)
    suspected_files: List[str] = field(default_factory=list)
    recent_diff: List[str] = field(default_factory=list)
    confidence: float = 0.0
    safe_to_attempt: bool = False
    attempt_count: int = 0
    status: str = "open"
    probable_root_cause: str = ""
    recommended_repair_scope: str = ""
    source_task_id: str = ""
    fingerprint: str = ""
    verification: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["summary"] = _clip(self.summary, 500)
        payload["stack_trace"] = _clip(self.stack_trace, 2400)
        payload["failing_tests"] = _dedupe_text_items(self.failing_tests, limit=12)
        payload["suspected_files"] = _dedupe_text_items(self.suspected_files, limit=16)
        payload["recent_diff"] = _dedupe_text_items(self.recent_diff, limit=16)
        payload["verification"] = _normalize_json_dict(self.verification)
        payload["metadata"] = _normalize_json_dict(self.metadata)
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Incident":
        data = dict(payload)
        return cls(
            incident_id=str(data.get("incident_id") or new_incident_id()),
            timestamp=float(data.get("timestamp") or time.time()),
            source=str(data.get("source") or "unknown"),
            error_type=str(data.get("error_type") or "unclear_or_unsafe"),
            summary=_clip(data.get("summary") or "", 500),
            stack_trace=_clip(data.get("stack_trace") or "", 2400),
            failing_tests=_dedupe_text_items(data.get("failing_tests"), limit=12),
            suspected_files=_dedupe_text_items(data.get("suspected_files"), limit=16),
            recent_diff=_dedupe_text_items(data.get("recent_diff"), limit=16),
            confidence=float(data.get("confidence") or 0.0),
            safe_to_attempt=bool(data.get("safe_to_attempt")),
            attempt_count=int(data.get("attempt_count") or 0),
            status=str(data.get("status") or "open"),
            probable_root_cause=_clip(data.get("probable_root_cause") or "", 1200),
            recommended_repair_scope=_clip(data.get("recommended_repair_scope") or "", 500),
            source_task_id=str(data.get("source_task_id") or ""),
            fingerprint=str(data.get("fingerprint") or ""),
            verification=_normalize_json_dict(data.get("verification")),
            metadata=_normalize_json_dict(data.get("metadata")),
        )


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

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["files_touched"] = _dedupe_text_items(self.files_touched, limit=16)
        payload["diff"] = _clip(self.diff, 24000)
        payload["reasoning_summary"] = _clip(self.reasoning_summary, 1200)
        payload["verification_results"] = _normalize_json_dict(self.verification_results)
        payload["error"] = _clip(self.error, 1600)
        payload["metadata"] = _normalize_json_dict(self.metadata)
        return payload


@dataclass
class RepairPlan:
    plan_id: str
    incident_id: str
    model_used: str
    root_cause: str
    steps: List[str]
    files_to_modify: List[str]
    risks: List[str]
    verification_plan: List[str]
    stop_conditions: List[str]
    cursor_handoff_prompt: str = ""
    status: str = "planned"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["root_cause"] = _clip(self.root_cause, 1600)
        payload["steps"] = _dedupe_text_items(self.steps, limit=16, item_limit=400)
        payload["files_to_modify"] = _dedupe_text_items(self.files_to_modify, limit=20)
        payload["risks"] = _dedupe_text_items(self.risks, limit=12, item_limit=320)
        payload["verification_plan"] = _dedupe_text_items(
            self.verification_plan, limit=12, item_limit=320
        )
        payload["stop_conditions"] = _dedupe_text_items(
            self.stop_conditions, limit=12, item_limit=320
        )
        payload["cursor_handoff_prompt"] = _clip(self.cursor_handoff_prompt, 12000)
        payload["metadata"] = _normalize_json_dict(self.metadata)
        return payload


@dataclass
class RepairBudget:
    max_token_budget: int = 24000
    max_model_invocations: int = 4
    max_elapsed_seconds: float = 1800.0
    max_patch_attempts: int = 2
    token_budget_used: int = 0
    model_invocations_used: int = 0
    patch_attempts_used: int = 0
    started_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

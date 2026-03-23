"""Contracts for approval-aware plan / verify / recover orchestration."""
from __future__ import annotations

from enum import Enum


class PlanStatus(str, Enum):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    QUEUED = "queued"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    BLOCKED = "blocked"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class StepStatus(str, Enum):
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    QUEUED = "queued"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ABANDONED = "abandoned"


class PlanKind(str, Enum):
    DELEGATED_REPO_TASK = "delegated_repo_task"
    DIRECT_STRUCTURED_ACTION = "direct_structured_action"


class StepKind(str, Enum):
    ANALYZE = "analyze"
    EXECUTE_DELEGATED = "execute_delegated"
    VERIFY_REPO = "verify_repo"
    SUMMARIZE = "summarize"

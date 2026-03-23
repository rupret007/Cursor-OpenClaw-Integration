"""Guardrails and budget tracking for incident-driven repairs."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List

from .repair_types import Incident, RepairBudget

SAFE_REPAIR_ROOTS = (
    "services/andrea_sync/",
    "tests/",
    "scripts/",
    "skills/",
)

AUTO_ATTEMPT_FILE_LIMITS = {
    1: 3,
    2: 5,
}

SENSITIVE_PATH_FRAGMENTS = (
    ".env",
    "credential",
    "secret",
    "token",
    "auth",
    "billing",
    "migration",
    "migrations",
)

DANGEROUS_DIFF_PATTERNS = (
    re.compile(r"\bDROP\s+TABLE\b", re.I),
    re.compile(r"\bTRUNCATE\s+TABLE\b", re.I),
    re.compile(r"\brm\s+-rf\b", re.I),
    re.compile(r"\bDELETE\s+FROM\b", re.I),
    re.compile(r"\bALTER\s+TABLE\b", re.I),
)

UNSAFE_CLASSIFICATIONS = {
    "unclear_or_unsafe",
    "unsafe_human_review",
}


def _clip(value: Any, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def estimate_token_usage(text: Any) -> int:
    blob = str(text or "")
    if not blob.strip():
        return 0
    return max(1, len(blob) // 4)


def normalize_repo_paths(value: Any, *, limit: int = 24) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for raw in value:
        path = str(raw or "").strip().replace("\\", "/")
        if not path:
            continue
        if path.startswith("./"):
            path = path[2:]
        if path not in out:
            out.append(path)
        if len(out) >= limit:
            break
    return out


def is_sensitive_path(path: str) -> bool:
    lowered = str(path or "").strip().lower()
    return any(fragment in lowered for fragment in SENSITIVE_PATH_FRAGMENTS)


def default_repair_budget() -> RepairBudget:
    return RepairBudget(
        max_token_budget=int(os.environ.get("ANDREA_REPAIR_MAX_TOKEN_BUDGET", "24000")),
        max_model_invocations=int(os.environ.get("ANDREA_REPAIR_MAX_MODEL_INVOCATIONS", "4")),
        max_elapsed_seconds=float(os.environ.get("ANDREA_REPAIR_MAX_ELAPSED_SECONDS", "1800")),
        max_patch_attempts=int(os.environ.get("ANDREA_REPAIR_MAX_PATCH_ATTEMPTS", "2")),
    )


def budget_state(budget: RepairBudget) -> Dict[str, Any]:
    elapsed = max(0.0, time.time() - float(budget.started_at))
    exhausted: List[str] = []
    if budget.token_budget_used >= budget.max_token_budget:
        exhausted.append("token_budget_exhausted")
    if budget.model_invocations_used >= budget.max_model_invocations:
        exhausted.append("model_invocation_budget_exhausted")
    if budget.patch_attempts_used >= budget.max_patch_attempts:
        exhausted.append("patch_attempt_budget_exhausted")
    if elapsed >= budget.max_elapsed_seconds:
        exhausted.append("elapsed_budget_exhausted")
    return {
        "elapsed_seconds": elapsed,
        "remaining_token_budget": max(0, budget.max_token_budget - budget.token_budget_used),
        "remaining_model_invocations": max(
            0, budget.max_model_invocations - budget.model_invocations_used
        ),
        "remaining_patch_attempts": max(0, budget.max_patch_attempts - budget.patch_attempts_used),
        "exhausted": exhausted,
    }


def record_model_invocation(budget: RepairBudget, prompt: Any) -> Dict[str, Any]:
    budget.model_invocations_used += 1
    budget.token_budget_used += estimate_token_usage(prompt)
    return budget_state(budget)


def record_patch_attempt(budget: RepairBudget) -> Dict[str, Any]:
    budget.patch_attempts_used += 1
    return budget_state(budget)


def incident_auto_attempt_guard(incident: Incident) -> Dict[str, Any]:
    reasons: List[str] = []
    classification = str(incident.error_type or "").strip().lower()
    if classification in UNSAFE_CLASSIFICATIONS:
        reasons.append(f"classification_blocked:{classification}")
    if incident.confidence < float(os.environ.get("ANDREA_REPAIR_MIN_CONFIDENCE", "0.45")):
        reasons.append("confidence_below_threshold")
    for path in normalize_repo_paths(incident.suspected_files):
        if is_sensitive_path(path):
            reasons.append(f"sensitive_incident_path:{path}")
    allowed = bool(incident.safe_to_attempt) and not reasons
    return {
        "allowed": allowed,
        "reasons": reasons,
        "classification": classification or "unknown",
    }


def patch_guardrails(proposal: Dict[str, Any], *, attempt_number: int) -> Dict[str, Any]:
    reasons: List[str] = []
    files_touched = normalize_repo_paths(proposal.get("files_touched"))
    diff = str(proposal.get("diff") or "").strip()
    max_files = AUTO_ATTEMPT_FILE_LIMITS.get(attempt_number, AUTO_ATTEMPT_FILE_LIMITS[2])

    if not files_touched:
        reasons.append("no_files_touched")
    if len(files_touched) > max_files:
        reasons.append(f"too_many_files_for_attempt:{len(files_touched)}>{max_files}")
    for path in files_touched:
        if not any(path.startswith(root) for root in SAFE_REPAIR_ROOTS):
            reasons.append(f"disallowed_target:{path}")
        if is_sensitive_path(path):
            reasons.append(f"sensitive_target:{path}")
    if not diff:
        reasons.append("missing_unified_diff")
    for pattern in DANGEROUS_DIFF_PATTERNS:
        if diff and pattern.search(diff):
            reasons.append(f"dangerous_diff:{pattern.pattern}")
            break

    tests_only = bool(files_touched) and all(path.startswith("tests/") for path in files_touched)
    test_change_reason = str(proposal.get("test_change_reason") or "").strip()
    if tests_only and not re.search(
        r"\b(stale|incorrect|obsolete|regression|behavior|fixture|test-only)\b",
        test_change_reason,
        re.I,
    ):
        reasons.append("tests_only_without_explicit_reason")

    return {
        "allowed": not reasons,
        "reasons": reasons,
        "files_touched": files_touched,
        "max_files": max_files,
        "tests_only": tests_only,
        "summary": _clip(proposal.get("reasoning_summary") or "", 400),
    }


def summarize_safe_roots(repo_path: Path) -> str:
    roots = ", ".join(SAFE_REPAIR_ROOTS)
    return f"Auto-repair roots for {repo_path}: {roots}"

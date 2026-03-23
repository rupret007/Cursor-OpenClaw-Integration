"""Incident-driven multi-model self-healing pipeline for Andrea."""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from .observability import metric_log, structured_log
from .repair_adapters import run_role_json
from .repair_detectors import detect_incident, heuristic_triage, recent_diff_summary
from .repair_executor import (
    apply_unified_diff,
    build_default_verification_checks,
    cleanup_worktree,
    commit_worktree_if_clean,
    compare_verification_reports,
    create_sandbox_worktree,
    main_worktree_clean,
    run_cursor_repair_handoff,
    run_verification_suite,
    write_repair_artifacts,
)
from .repair_policy import (
    budget_state,
    default_repair_budget,
    incident_auto_attempt_guard,
    normalize_repo_paths,
    patch_guardrails,
    repair_enabled,
    record_model_invocation,
    record_patch_attempt,
    record_patch_scope,
)
from .repair_prompts import (
    build_challenger_patch_prompt,
    build_deep_debug_prompt,
    build_primary_patch_prompt,
    build_triage_prompt,
    repair_prompt_version,
)
from .repair_types import (
    Incident,
    PatchAttempt,
    PatchProposal,
    RepairPlan,
    VerificationCheck,
    new_attempt_id,
    new_incident_id,
    new_plan_id,
)
from .schema import EventType
from .store import (
    SYSTEM_TASK_ID,
    append_event,
    ensure_system_task,
    get_incident,
    get_latest_repair_plan,
    list_repair_attempts,
    save_incident,
    save_repair_attempt,
    save_repair_plan,
    task_exists,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTEXT_FILES = (
    ".env.example",
    "README.md",
    "services/andrea_sync/server.py",
    "services/andrea_sync/policy.py",
    "scripts/andrea_openclaw_enforce.sh",
)


def _clip(value: Any, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _audit_task_id(conn: Any, source_task_id: str) -> str:
    tid = str(source_task_id or "").strip()
    if tid and task_exists(conn, tid):
        return tid
    ensure_system_task(conn)
    return SYSTEM_TASK_ID


def _select_context_files(repo_path: Path, incident: Incident) -> List[Dict[str, Any]]:
    paths = normalize_repo_paths(incident.suspected_files)
    if incident.error_type in {"dependency_issue", "config_issue"}:
        for default_path in DEFAULT_CONTEXT_FILES:
            if default_path not in paths:
                paths.append(default_path)
    context_files: List[Dict[str, Any]] = []
    max_bytes = int(os.environ.get("ANDREA_REPAIR_CONTEXT_BYTES", "6000"))
    for rel in paths[:8]:
        abs_path = (repo_path / rel).resolve()
        try:
            abs_path.relative_to(repo_path.resolve())
        except ValueError:
            continue
        if not abs_path.is_file():
            continue
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        context_files.append({"path": rel, "content": _clip(text, max_bytes)})
    return context_files


def _model_used(result: Dict[str, Any]) -> str:
    provider = str(result.get("provider") or result.get("requested_family") or "").strip()
    model = str(result.get("model") or result.get("requested_label") or "").strip()
    return ":".join(part for part in (provider, model) if part) or "unknown"


def _routing_meta(result: Dict[str, Any]) -> Dict[str, Any]:
    routing = dict(result.get("routing") or {}) if isinstance(result.get("routing"), dict) else {}
    return {
        "agent_id": str(result.get("agent_id") or "").strip(),
        "requested_family": str(result.get("requested_family") or "").strip(),
        "requested_label": str(result.get("requested_label") or "").strip(),
        "provider": str(result.get("provider") or "").strip(),
        "model": str(result.get("model") or "").strip(),
        "routing": routing,
    }


def _attempt_from_dict(payload: Dict[str, Any]) -> PatchAttempt:
    return PatchAttempt(
        attempt_id=str(payload.get("attempt_id") or new_attempt_id()),
        incident_id=str(payload.get("incident_id") or ""),
        attempt_number=int(payload.get("attempt_number") or payload.get("attempt_no") or 0),
        stage=str(payload.get("stage") or ""),
        model_used=str(payload.get("model_used") or ""),
        status=str(payload.get("status") or "pending"),
        prompt_version=str(payload.get("prompt_version") or ""),
        files_touched=normalize_repo_paths(payload.get("files_touched")),
        diff=str(payload.get("diff") or ""),
        reasoning_summary=_clip(payload.get("reasoning_summary") or "", 1200),
        verification_results=dict(
            payload.get("verification_results") or payload.get("verification_result") or {}
        )
        if isinstance(payload.get("verification_results") or payload.get("verification_result"), dict)
        else {},
        success=bool(payload.get("success")),
        rollback_performed=bool(payload.get("rollback_performed")),
        branch=str(payload.get("branch") or ""),
        worktree_path=str(payload.get("worktree_path") or ""),
        report_path=str(payload.get("report_path") or ""),
        error=_clip(payload.get("error") or "", 1600),
        created_at=float(payload.get("created_at") or payload.get("started_at") or time.time()),
        updated_at=float(payload.get("updated_at") or payload.get("completed_at") or time.time()),
        metadata=dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {},
    )


def _proposal_from_model(result: Dict[str, Any]) -> PatchProposal:
    payload = dict(result.get("payload") or {})
    return PatchProposal(
        model_used=_model_used(result),
        reasoning_summary=_clip(
            payload.get("reasoning_summary") or payload.get("critique_of_previous_attempt") or "",
            1200,
        ),
        files_touched=normalize_repo_paths(payload.get("files_touched")),
        diff=str(payload.get("diff") or ""),
        tests_expected=[
            str(v) for v in (payload.get("tests_expected") or []) if str(v).strip()
        ][:12]
        if isinstance(payload.get("tests_expected"), list)
        else [],
        confidence=float(payload.get("confidence") or 0.0),
        safe_to_apply=bool(payload.get("safe_to_apply")),
        test_change_reason=str(payload.get("test_change_reason") or ""),
        raw_response=payload,
    )


def _plan_from_model(
    result: Dict[str, Any],
    *,
    incident_id: str,
    prompt_version: str,
) -> RepairPlan:
    payload = dict(result.get("payload") or {})
    return RepairPlan(
        plan_id=new_plan_id(),
        incident_id=incident_id,
        model_used=_model_used(result),
        prompt_version=prompt_version,
        root_cause=_clip(payload.get("root_cause") or "", 1600),
        steps=[str(v) for v in (payload.get("steps") or []) if str(v).strip()][:16],
        files_to_modify=normalize_repo_paths(payload.get("files_to_modify")),
        risks=[str(v) for v in (payload.get("risks") or []) if str(v).strip()][:12],
        verification_plan=[
            str(v) for v in (payload.get("verification_plan") or []) if str(v).strip()
        ][:12],
        stop_conditions=[
            str(v) for v in (payload.get("stop_conditions") or []) if str(v).strip()
        ][:12],
        cursor_handoff_prompt=_clip(payload.get("handoff_summary") or "", 2000),
        status="planned",
        metadata={**payload, "routing_meta": _routing_meta(result)},
    )


def _commit_message(incident: Incident) -> str:
    kind = str(incident.error_type or "incident").replace("_", "-")
    return f"fix(repair): resolve {kind} incident {incident.incident_id[:8]}"


def _save_state(
    conn: Any,
    incident: Incident,
    state: str,
    *,
    reason: str,
    model_used: str = "",
    attempt_id: str = "",
    attempt_number: int = 0,
    extra: Dict[str, Any] | None = None,
) -> None:
    incident.record_state(
        state,
        reason=reason,
        model_used=model_used,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        extra=extra,
    )
    save_incident(conn, incident.as_dict())


def _run_patch_attempt(
    conn: Any,
    *,
    repo_path: Path,
    audit_task_id: str,
    incident: Incident,
    attempt_number: int,
    stage: str,
    prompt: str,
    prompt_version: str,
    budget: Any,
    verification_checks: List[VerificationCheck],
    baseline_verification: Dict[str, Any],
) -> PatchAttempt:
    patch_state = "patching_primary" if stage == "primary_patch" else "patching_challenger"
    verify_state = "verifying_primary" if stage == "primary_patch" else "verifying_challenger"
    attempt = PatchAttempt(
        attempt_id=new_attempt_id(),
        incident_id=incident.incident_id,
        attempt_number=attempt_number,
        stage=stage,
        model_used="",
        status="running",
        prompt_version=prompt_version,
    )
    _save_state(
        conn,
        incident,
        patch_state,
        reason=f"{stage} attempt started",
        attempt_id=attempt.attempt_id,
        attempt_number=attempt_number,
        extra={"prompt_version": prompt_version},
    )
    save_repair_attempt(conn, attempt.as_dict())
    append_event(
        conn,
        audit_task_id,
        EventType.REPAIR_ATTEMPT_STARTED,
        {
            "incident_id": incident.incident_id,
            "attempt_id": attempt.attempt_id,
            "attempt_number": attempt_number,
            "stage": stage,
            "summary": incident.summary,
            "current_state": incident.current_state,
            "prompt_version": prompt_version,
        },
    )
    record_model_invocation(budget, prompt)
    role_name = "primary_patch" if stage == "primary_patch" else "challenger_patch"
    model_result = run_role_json(
        role=role_name,
        prompt=prompt,
        incident_id=incident.incident_id,
        repo_path=repo_path,
    )
    attempt.model_used = _model_used(model_result)
    if not model_result.get("ok"):
        attempt.status = "failed"
        attempt.error = str(model_result.get("error") or "model lane failed")
        attempt.updated_at = time.time()
        save_repair_attempt(conn, attempt.as_dict())
        append_event(
            conn,
            audit_task_id,
            EventType.REPAIR_ATTEMPT_FAILED,
            {
                "incident_id": incident.incident_id,
                "attempt_id": attempt.attempt_id,
                "attempt_number": attempt_number,
                "stage": stage,
                "model_used": attempt.model_used,
                "error": attempt.error,
                "current_state": incident.current_state,
                "prompt_version": prompt_version,
            },
        )
        return attempt

    proposal = _proposal_from_model(model_result)
    attempt.model_used = proposal.model_used
    attempt.files_touched = list(proposal.files_touched)
    attempt.diff = proposal.diff
    attempt.reasoning_summary = proposal.reasoning_summary
    attempt.metadata["routing_meta"] = _routing_meta(model_result)
    record_patch_scope(budget, proposal.diff)
    guard = patch_guardrails(proposal.as_dict(), attempt_number=attempt_number)
    attempt.metadata["guardrails"] = guard
    if not proposal.safe_to_apply or not guard.get("allowed"):
        attempt.status = "blocked"
        attempt.error = "; ".join(guard.get("reasons") or []) or "model_marked_patch_unsafe"
        attempt.updated_at = time.time()
        save_repair_attempt(conn, attempt.as_dict())
        append_event(
            conn,
            audit_task_id,
            EventType.REPAIR_ATTEMPT_FAILED,
            {
                "incident_id": incident.incident_id,
                "attempt_id": attempt.attempt_id,
                "attempt_number": attempt_number,
                "stage": stage,
                "model_used": attempt.model_used,
                "error": attempt.error,
                "current_state": incident.current_state,
                "prompt_version": prompt_version,
            },
        )
        return attempt

    sandbox = create_sandbox_worktree(
        repo_path,
        incident_id=incident.incident_id,
        stage=f"a{attempt_number}-{stage}",
    )
    if not sandbox.get("ok"):
        attempt.status = "failed"
        attempt.error = str(sandbox.get("error") or "sandbox creation failed")
        attempt.updated_at = time.time()
        save_repair_attempt(conn, attempt.as_dict())
        append_event(
            conn,
            audit_task_id,
            EventType.REPAIR_ATTEMPT_FAILED,
            {
                "incident_id": incident.incident_id,
                "attempt_id": attempt.attempt_id,
                "attempt_number": attempt_number,
                "stage": stage,
                "model_used": attempt.model_used,
                "error": attempt.error,
                "current_state": incident.current_state,
                "prompt_version": prompt_version,
            },
        )
        return attempt

    attempt.branch = str(sandbox.get("branch") or "")
    attempt.worktree_path = str(sandbox.get("worktree_path") or "")
    apply_result = apply_unified_diff(
        worktree_path=Path(attempt.worktree_path),
        diff_text=proposal.diff,
    )
    if not apply_result.get("ok"):
        cleanup_worktree(
            repo_path=repo_path,
            worktree_path=attempt.worktree_path,
            branch=attempt.branch,
            keep_branch=False,
        )
        attempt.rollback_performed = True
        attempt.status = "failed"
        attempt.error = str(apply_result.get("error") or "patch apply failed")
        attempt.updated_at = time.time()
        _save_state(
            conn,
            incident,
            "rolled_back",
            reason=f"{stage} patch apply failed",
            model_used=attempt.model_used,
            attempt_id=attempt.attempt_id,
            attempt_number=attempt_number,
            extra={"branch": attempt.branch},
        )
        save_repair_attempt(conn, attempt.as_dict())
        append_event(
            conn,
            audit_task_id,
            EventType.REPAIR_ROLLBACK_COMPLETED,
            {
                "incident_id": incident.incident_id,
                "attempt_id": attempt.attempt_id,
                "attempt_number": attempt_number,
                "stage": stage,
                "branch": attempt.branch,
                "current_state": incident.current_state,
            },
        )
        append_event(
            conn,
            audit_task_id,
            EventType.REPAIR_ATTEMPT_FAILED,
            {
                "incident_id": incident.incident_id,
                "attempt_id": attempt.attempt_id,
                "attempt_number": attempt_number,
                "stage": stage,
                "model_used": attempt.model_used,
                "error": attempt.error,
                "current_state": incident.current_state,
                "prompt_version": prompt_version,
            },
        )
        return attempt

    _save_state(
        conn,
        incident,
        verify_state,
        reason=f"{stage} verification started",
        model_used=attempt.model_used,
        attempt_id=attempt.attempt_id,
        attempt_number=attempt_number,
    )
    verification = run_verification_suite(
        checks=verification_checks,
        cwd_override=Path(attempt.worktree_path),
        repo_path=repo_path,
    )
    verification_delta = compare_verification_reports(
        baseline_report=baseline_verification,
        candidate_report=verification,
    )
    attempt.verification_results = {
        **verification,
        "baseline_comparison": verification_delta,
    }
    attempt.metadata["verification_delta"] = verification_delta
    if verification_delta.get("worse_than_baseline"):
        verification = {**verification, "passed": False, "summary": verification_delta.get("summary")}
        attempt.verification_results = {
            **verification,
            "baseline_comparison": verification_delta,
        }

    if verification.get("passed"):
        commit = commit_worktree_if_clean(
            worktree_path=Path(attempt.worktree_path),
            message=_commit_message(incident),
        )
        if not commit.get("ok") or bool(commit.get("skipped")):
            cleanup_worktree(
                repo_path=repo_path,
                worktree_path=attempt.worktree_path,
                branch=attempt.branch,
                keep_branch=False,
            )
            attempt.rollback_performed = True
            attempt.status = "failed"
            attempt.error = str(
                commit.get("error")
                or ("no_changes_after_patch" if commit.get("skipped") else "commit failed")
            )
            attempt.updated_at = time.time()
            _save_state(
                conn,
                incident,
                "rolled_back",
                reason=f"{stage} commit failed",
                model_used=attempt.model_used,
                attempt_id=attempt.attempt_id,
                attempt_number=attempt_number,
                extra={"branch": attempt.branch},
            )
            save_repair_attempt(conn, attempt.as_dict())
            append_event(
                conn,
                audit_task_id,
                EventType.REPAIR_ROLLBACK_COMPLETED,
                {
                    "incident_id": incident.incident_id,
                    "attempt_id": attempt.attempt_id,
                    "attempt_number": attempt_number,
                    "stage": stage,
                    "branch": attempt.branch,
                    "current_state": incident.current_state,
                },
            )
            append_event(
                conn,
                audit_task_id,
                EventType.REPAIR_ATTEMPT_FAILED,
                {
                    "incident_id": incident.incident_id,
                    "attempt_id": attempt.attempt_id,
                    "attempt_number": attempt_number,
                    "stage": stage,
                    "model_used": attempt.model_used,
                    "error": attempt.error,
                    "current_state": incident.current_state,
                "prompt_version": prompt_version,
                },
            )
            return attempt

        cleanup_worktree(
            repo_path=repo_path,
            worktree_path=attempt.worktree_path,
            branch=attempt.branch,
            keep_branch=True,
        )
        attempt.success = True
        attempt.status = "completed"
        attempt.updated_at = time.time()
        attempt.metadata["commit_sha"] = str(commit.get("commit_sha") or "")
        save_repair_attempt(conn, attempt.as_dict())
        append_event(
            conn,
            audit_task_id,
            EventType.REPAIR_ATTEMPT_COMPLETED,
            {
                "incident_id": incident.incident_id,
                "attempt_id": attempt.attempt_id,
                "attempt_number": attempt_number,
                "stage": stage,
                "model_used": attempt.model_used,
                "branch": attempt.branch,
                "summary": attempt.reasoning_summary,
                "current_state": incident.current_state,
                "prompt_version": prompt_version,
            },
        )
        return attempt

    cleanup_worktree(
        repo_path=repo_path,
        worktree_path=attempt.worktree_path,
        branch=attempt.branch,
        keep_branch=False,
    )
    attempt.rollback_performed = True
    attempt.status = "failed"
    attempt.error = str(verification.get("summary") or "verification failed")
    attempt.updated_at = time.time()
    _save_state(
        conn,
        incident,
        "rolled_back",
        reason=f"{stage} verification failed",
        model_used=attempt.model_used,
        attempt_id=attempt.attempt_id,
        attempt_number=attempt_number,
        extra={"branch": attempt.branch, "verification_delta": verification_delta},
    )
    save_repair_attempt(conn, attempt.as_dict())
    append_event(
        conn,
        audit_task_id,
        EventType.REPAIR_ROLLBACK_COMPLETED,
        {
            "incident_id": incident.incident_id,
            "attempt_id": attempt.attempt_id,
            "attempt_number": attempt_number,
            "stage": stage,
            "branch": attempt.branch,
            "current_state": incident.current_state,
        },
    )
    append_event(
        conn,
        audit_task_id,
        EventType.REPAIR_ATTEMPT_FAILED,
        {
            "incident_id": incident.incident_id,
            "attempt_id": attempt.attempt_id,
            "attempt_number": attempt_number,
            "stage": stage,
            "model_used": attempt.model_used,
            "error": attempt.error,
            "current_state": incident.current_state,
            "prompt_version": prompt_version,
        },
    )
    return attempt


def run_incident_repair_cycle(
    conn: Any,
    *,
    repo_path: Path,
    actor: str = "internal",
    incident_payload: Dict[str, Any] | None = None,
    verification_report: Dict[str, Any] | None = None,
    runtime_error: Dict[str, Any] | None = None,
    health_failure: Dict[str, Any] | None = None,
    log_alert: Dict[str, Any] | None = None,
    source_task_id: str = "",
    incident_id: str = "",
    cursor_execute: bool = False,
    write_report: bool = True,
) -> Dict[str, Any]:
    ensure_system_task(conn)
    repo = Path(repo_path or REPO_ROOT).expanduser()
    audit_task_id = _audit_task_id(conn, source_task_id)
    verification_checks = build_default_verification_checks(repo)
    verification = dict(verification_report or {})
    if not verification:
        verification = run_verification_suite(
            checks=verification_checks,
            cwd_override=repo,
            repo_path=repo,
        )

    incident: Incident | None = None
    if incident_id:
        stored_incident = get_incident(conn, incident_id)
        if stored_incident:
            incident = Incident.from_dict(stored_incident)
    if incident is None:
        incident = detect_incident(
            repo_path=repo,
            incident_payload=incident_payload,
            verification_report=verification,
            runtime_error=runtime_error,
            health_failure=health_failure,
            log_alert=log_alert,
            source_task_id=source_task_id,
        )
    if incident is None:
        return {
            "ok": True,
            "resolved": False,
            "skipped": True,
            "skip_reason": "no_incident_detected",
            "verification_report": verification,
        }

    if not incident.incident_id:
        incident.incident_id = new_incident_id()
    if not incident.fingerprint:
        incident.fingerprint = hashlib.sha256(
            "|".join(
                [
                    incident.summary,
                    incident.error_type,
                    _clip(incident.stack_trace, 400),
                ]
            ).encode("utf-8")
        ).hexdigest()[:20]
    incident.source_task_id = incident.source_task_id or source_task_id
    incident.verification = verification or incident.verification
    _save_state(
        conn,
        incident,
        "detected",
        reason=f"{incident.source} incident opened",
        extra={"actor": actor, "fingerprint": incident.fingerprint},
    )
    append_event(
        conn,
        audit_task_id,
        EventType.INCIDENT_RECORDED,
        {
            "incident_id": incident.incident_id,
            "source_task_id": incident.source_task_id,
            "source": incident.source,
            "error_type": incident.error_type,
            "summary": incident.summary,
            "fingerprint": incident.fingerprint,
            "actor": actor,
            "current_state": incident.current_state,
            "service_name": incident.service_name,
            "environment": incident.environment,
        },
    )
    structured_log(
        "incident_repair_opened",
        incident_id=incident.incident_id,
        source=incident.source,
        error_type=incident.error_type,
    )

    if not repair_enabled():
        incident.metadata["repair_disabled"] = True
        _save_state(
            conn,
            incident,
            "human_review_required",
            reason="repair execution disabled by policy",
            extra={"policy_env": "ANDREA_REPAIR_ENABLED"},
        )
        append_event(
            conn,
            audit_task_id,
            EventType.INCIDENT_ESCALATED,
            {
                "incident_id": incident.incident_id,
                "summary": incident.summary,
                "error": "repair_disabled",
                "current_state": incident.current_state,
            },
        )
        return {
            "ok": True,
            "resolved": False,
            "skipped": True,
            "skip_reason": "repair_disabled",
            "status": incident.current_state,
            "incident": incident.as_dict(),
            "attempts": [],
            "plan": {},
            "verification_report": verification,
            "artifact_paths": {},
            "budget": default_repair_budget().as_dict(),
        }

    budget = default_repair_budget()
    triage_prompt_version = repair_prompt_version("TRIAGE")
    triage_prompt = build_triage_prompt(
        incident=incident,
        verification_report=verification,
        recent_diff_summary=recent_diff_summary(repo),
        budget_state=budget_state(budget),
    )
    record_model_invocation(budget, triage_prompt)
    triage_result = run_role_json(
        role="triage",
        prompt=triage_prompt,
        incident_id=incident.incident_id,
        repo_path=repo,
    )
    triage_payload = dict(triage_result.get("payload") or {})
    heuristic = heuristic_triage(incident.stack_trace or incident.summary)
    incident.error_type = str(
        triage_payload.get("classification") or incident.error_type or heuristic["classification"]
    )
    incident.probable_root_cause = _clip(
        triage_payload.get("probable_root_cause")
        or incident.probable_root_cause
        or heuristic["probable_root_cause"],
        1200,
    )
    incident.recommended_repair_scope = _clip(
        triage_payload.get("recommended_repair_scope")
        or incident.recommended_repair_scope
        or "1-3 files in allowed auto-repair roots.",
        500,
    )
    if isinstance(triage_payload.get("affected_files"), list):
        incident.suspected_files = normalize_repo_paths(triage_payload.get("affected_files"))
    if isinstance(triage_payload.get("failing_tests"), list):
        incident.failing_tests = [str(v) for v in triage_payload.get("failing_tests")[:10]]
    if isinstance(incident.metadata, dict):
        incident.metadata["triage_prompt_version"] = triage_prompt_version
        incident.metadata["triage_routing_meta"] = _routing_meta(triage_result)
        incident.metadata["needs_human_review"] = bool(triage_payload.get("needs_human_review"))
    incident.confidence = float(
        triage_payload.get("confidence") or incident.confidence or heuristic["confidence"]
    )
    incident.safe_to_attempt = bool(
        triage_payload.get("safe_to_auto_attempt")
        if triage_result.get("ok")
        else incident.safe_to_attempt or heuristic["safe_to_auto_attempt"]
    )
    _save_state(
        conn,
        incident,
        "triaged",
        reason="triage completed",
        model_used=_model_used(triage_result),
        extra={
            "classification": incident.error_type,
            "safe_to_auto_attempt": incident.safe_to_attempt,
            "confidence": incident.confidence,
            "prompt_version": triage_prompt_version,
        },
    )
    append_event(
        conn,
        audit_task_id,
        EventType.INCIDENT_TRIAGED,
        {
            "incident_id": incident.incident_id,
            "classification": incident.error_type,
            "confidence": incident.confidence,
            "safe_to_auto_attempt": incident.safe_to_attempt,
            "summary": incident.summary,
            "model_used": _model_used(triage_result),
            "current_state": incident.current_state,
            "prompt_version": triage_prompt_version,
        },
    )

    context_files = _select_context_files(repo, incident)
    guard = incident_auto_attempt_guard(incident)
    clean_state = main_worktree_clean(repo)
    if not clean_state.get("clean"):
        guard = {
            **guard,
            "allowed": False,
            "reasons": list(guard.get("reasons") or []) + ["main_worktree_dirty"],
        }

    existing_attempt_rows = list_repair_attempts(conn, incident.incident_id)
    existing_attempts = [_attempt_from_dict(row) for row in existing_attempt_rows]
    budget.patch_attempts_used = min(len(existing_attempts), budget.max_patch_attempts)
    budget.model_invocations_used = min(
        budget.max_model_invocations,
        budget.model_invocations_used + len(existing_attempts),
    )
    attempts: List[PatchAttempt] = []
    report_paths: Dict[str, str] = {}
    next_attempt_number = len(existing_attempts) + 1

    if guard.get("allowed") and next_attempt_number <= budget.max_patch_attempts:
        if next_attempt_number == 1:
            primary_prompt_version = repair_prompt_version("PRIMARY")
            record_patch_attempt(budget)
            primary_prompt = build_primary_patch_prompt(
                incident=incident,
                context_files=context_files,
                attempt_number=next_attempt_number,
                budget_state=budget_state(budget),
            )
            primary_attempt = _run_patch_attempt(
                conn,
                repo_path=repo,
                audit_task_id=audit_task_id,
                incident=incident,
                attempt_number=next_attempt_number,
                stage="primary_patch",
                prompt=primary_prompt,
                prompt_version=primary_prompt_version,
                budget=budget,
                verification_checks=verification_checks,
                baseline_verification=verification,
            )
            attempts.append(primary_attempt)
            incident.attempt_count = len(existing_attempts) + len(attempts)
            save_incident(conn, incident.as_dict())
            if primary_attempt.success:
                _save_state(
                    conn,
                    incident,
                    "resolved",
                    reason="primary repair attempt verified and committed",
                    model_used=primary_attempt.model_used,
                    attempt_id=primary_attempt.attempt_id,
                    attempt_number=primary_attempt.attempt_number,
                    extra={"branch": primary_attempt.branch},
                )
                if write_report:
                    report_paths = write_repair_artifacts(
                        repo_path=repo,
                        incident=incident,
                        attempts=existing_attempts + attempts,
                        plan=None,
                        verification_report=primary_attempt.verification_results,
                        status=incident.current_state,
                    )
                append_event(
                    conn,
                    audit_task_id,
                    EventType.INCIDENT_RESOLVED,
                    {
                        "incident_id": incident.incident_id,
                        "attempt_id": primary_attempt.attempt_id,
                        "attempt_number": primary_attempt.attempt_number,
                        "branch": primary_attempt.branch,
                        "commit_sha": str(primary_attempt.metadata.get("commit_sha") or ""),
                        "summary": incident.summary,
                        "current_state": incident.current_state,
                        "report_path": str(report_paths.get("json_path") or ""),
                        "markdown_path": str(report_paths.get("markdown_path") or ""),
                    },
                )
                metric_log(
                    "incident_repair_resolved",
                    incident_id=incident.incident_id,
                    attempts=incident.attempt_count,
                )
                return {
                    "ok": True,
                    "resolved": True,
                    "status": incident.current_state,
                    "incident": incident.as_dict(),
                    "attempts": [attempt.as_dict() for attempt in existing_attempts + attempts],
                    "plan": {},
                    "verification_report": primary_attempt.verification_results,
                    "report_path": str(report_paths.get("json_path") or ""),
                    "artifact_paths": report_paths,
                "budget": budget.as_dict(),
                    "repair_history": {
                        "incident": get_incident(conn, incident.incident_id),
                        "attempts": list_repair_attempts(conn, incident.incident_id),
                        "latest_plan": get_latest_repair_plan(conn, incident.incident_id),
                    },
                }

            next_attempt_number = len(existing_attempts) + len(attempts) + 1

        if next_attempt_number == 2:
            challenger_prompt_version = repair_prompt_version("CHALLENGER")
            record_patch_attempt(budget)
            failed_attempt = attempts[-1] if attempts else existing_attempts[-1]
            challenger_prompt = build_challenger_patch_prompt(
                incident=incident,
                failed_attempt=failed_attempt,
                context_files=context_files,
                attempt_number=next_attempt_number,
                budget_state=budget_state(budget),
            )
            challenger_attempt = _run_patch_attempt(
                conn,
                repo_path=repo,
                audit_task_id=audit_task_id,
                incident=incident,
                attempt_number=next_attempt_number,
                stage="challenger_patch",
                prompt=challenger_prompt,
                prompt_version=challenger_prompt_version,
                budget=budget,
                verification_checks=verification_checks,
                baseline_verification=verification,
            )
            attempts.append(challenger_attempt)
            incident.attempt_count = len(existing_attempts) + len(attempts)
            save_incident(conn, incident.as_dict())
            if challenger_attempt.success:
                _save_state(
                    conn,
                    incident,
                    "resolved",
                    reason="challenger repair attempt verified and committed",
                    model_used=challenger_attempt.model_used,
                    attempt_id=challenger_attempt.attempt_id,
                    attempt_number=challenger_attempt.attempt_number,
                    extra={"branch": challenger_attempt.branch},
                )
                if write_report:
                    report_paths = write_repair_artifacts(
                        repo_path=repo,
                        incident=incident,
                        attempts=existing_attempts + attempts,
                        plan=None,
                        verification_report=challenger_attempt.verification_results,
                        status=incident.current_state,
                    )
                append_event(
                    conn,
                    audit_task_id,
                    EventType.INCIDENT_RESOLVED,
                    {
                        "incident_id": incident.incident_id,
                        "attempt_id": challenger_attempt.attempt_id,
                        "attempt_number": challenger_attempt.attempt_number,
                        "branch": challenger_attempt.branch,
                        "commit_sha": str(challenger_attempt.metadata.get("commit_sha") or ""),
                        "summary": incident.summary,
                        "current_state": incident.current_state,
                        "report_path": str(report_paths.get("json_path") or ""),
                        "markdown_path": str(report_paths.get("markdown_path") or ""),
                    },
                )
                metric_log(
                    "incident_repair_resolved",
                    incident_id=incident.incident_id,
                    attempts=incident.attempt_count,
                )
                return {
                    "ok": True,
                    "resolved": True,
                    "status": incident.current_state,
                    "incident": incident.as_dict(),
                    "attempts": [attempt.as_dict() for attempt in existing_attempts + attempts],
                    "plan": {},
                    "verification_report": challenger_attempt.verification_results,
                    "report_path": str(report_paths.get("json_path") or ""),
                    "artifact_paths": report_paths,
                    "budget": budget.as_dict(),
                    "repair_history": {
                        "incident": get_incident(conn, incident.incident_id),
                        "attempts": list_repair_attempts(conn, incident.incident_id),
                        "latest_plan": get_latest_repair_plan(conn, incident.incident_id),
                    },
                }

    _save_state(
        conn,
        incident,
        "planning_escalation",
        reason="lightweight repair exhausted or unsafe",
        extra={
            "guard_reasons": list(guard.get("reasons") or []),
            "worktree_clean": bool(clean_state.get("clean")),
        },
    )
    deep_prompt_version = repair_prompt_version("DEEP")
    deep_prompt = build_deep_debug_prompt(
        incident=incident,
        attempts=existing_attempts + attempts,
        context_files=context_files,
        budget_state=budget_state(budget),
    )
    record_model_invocation(budget, deep_prompt)
    deep_result = run_role_json(
        role="deep_debug",
        prompt=deep_prompt,
        incident_id=incident.incident_id,
        repo_path=repo,
    )
    plan = (
        _plan_from_model(
            deep_result,
            incident_id=incident.incident_id,
            prompt_version=deep_prompt_version,
        )
        if deep_result.get("ok")
        else RepairPlan(
            plan_id=new_plan_id(),
            incident_id=incident.incident_id,
            model_used=_model_used(deep_result),
            prompt_version=deep_prompt_version,
            root_cause="Deep repair planning failed before a reliable plan was produced.",
            steps=[],
            files_to_modify=[],
            risks=["Model planning lane failed."],
            verification_plan=[],
            stop_conditions=["Stop and hand off for human review."],
            status="failed",
            metadata={
                "error": str(deep_result.get("error") or ""),
                "routing_meta": _routing_meta(deep_result),
            },
        )
    )
    save_repair_plan(conn, plan.as_dict())
    append_event(
        conn,
        audit_task_id,
        EventType.REPAIR_PLAN_CREATED,
        {
            "incident_id": incident.incident_id,
            "plan_id": plan.plan_id,
            "model_used": plan.model_used,
            "root_cause": plan.root_cause,
            "summary": incident.summary,
            "current_state": incident.current_state,
            "prompt_version": plan.prompt_version,
        },
    )

    if write_report:
        report_paths = write_repair_artifacts(
            repo_path=repo,
            incident=incident,
            attempts=existing_attempts + attempts,
            plan=plan,
            verification_report=verification,
            status=incident.current_state,
        )
        plan.cursor_handoff_payload = {
            **plan.cursor_handoff_payload,
            **report_paths,
        }
        save_repair_plan(conn, plan.as_dict())

    cursor_handoff: Dict[str, Any] = {}
    if cursor_execute and clean_state.get("clean"):
        handoff_verification = (
            attempts[-1].verification_results
            if attempts and isinstance(attempts[-1].verification_results, dict)
            else verification
        )
        cursor_handoff = run_cursor_repair_handoff(
            repo_path=repo,
            incident=incident,
            plan=plan,
            attempts=existing_attempts + attempts,
            verification_checks=verification_checks,
            verification_report=handoff_verification,
            cursor_mode=(os.environ.get("ANDREA_REPAIR_CURSOR_MODE") or "auto").strip() or "auto",
        )
        if cursor_handoff.get("ok"):
            _save_state(
                conn,
                incident,
                "cursor_handoff_ready",
                reason="cursor handoff package generated",
                extra={
                    "branch": str(cursor_handoff.get("branch") or ""),
                    "agent_url": str(cursor_handoff.get("agent_url") or ""),
                    "pr_url": str(cursor_handoff.get("pr_url") or ""),
                },
            )
            append_event(
                conn,
                audit_task_id,
                EventType.REPAIR_HANDOFF_RECORDED,
                {
                    "incident_id": incident.incident_id,
                    "plan_id": plan.plan_id,
                    "branch": str(cursor_handoff.get("branch") or ""),
                    "agent_url": str(cursor_handoff.get("agent_url") or ""),
                    "pr_url": str(cursor_handoff.get("pr_url") or ""),
                    "summary": incident.summary,
                    "current_state": incident.current_state,
                    "report_path": str(report_paths.get("json_path") or ""),
                    "markdown_path": str(report_paths.get("markdown_path") or ""),
                },
            )

    final_state = "human_review_required"
    if cursor_handoff.get("ok"):
        final_state = "cursor_handoff_ready"
    _save_state(
        conn,
        incident,
        final_state,
        reason="repair escalated beyond safe lightweight attempts",
        extra={"plan_id": plan.plan_id},
    )
    append_event(
        conn,
        audit_task_id,
        EventType.INCIDENT_ESCALATED,
        {
            "incident_id": incident.incident_id,
            "plan_id": plan.plan_id,
            "summary": incident.summary,
            "error": "; ".join(guard.get("reasons") or []) or str(deep_result.get("error") or ""),
            "report_path": str(report_paths.get("json_path") or ""),
            "markdown_path": str(report_paths.get("markdown_path") or ""),
            "current_state": incident.current_state,
        },
    )
    structured_log(
        "incident_repair_escalated",
        incident_id=incident.incident_id,
        plan_id=plan.plan_id,
        cursor_execute=cursor_execute,
    )
    return {
        "ok": True,
        "resolved": False,
        "status": incident.current_state,
        "incident": incident.as_dict(),
        "attempts": [attempt.as_dict() for attempt in existing_attempts + attempts],
        "plan": plan.as_dict(),
        "verification_report": verification,
        "guard": guard,
        "cursor_handoff": cursor_handoff,
        "report_path": str(report_paths.get("json_path") or ""),
        "artifact_paths": report_paths,
        "budget": budget.as_dict(),
        "repair_history": {
            "incident": get_incident(conn, incident.incident_id),
            "attempts": list_repair_attempts(conn, incident.incident_id),
            "latest_plan": get_latest_repair_plan(conn, incident.incident_id),
        },
    }

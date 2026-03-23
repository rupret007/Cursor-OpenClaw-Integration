"""Incident-driven multi-model self-healing pipeline for Andrea."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from .observability import metric_log, structured_log
from .repair_adapters import run_role_json
from .repair_executor import (
    apply_unified_diff,
    build_default_verification_checks,
    cleanup_worktree,
    commit_worktree_if_clean,
    create_sandbox_worktree,
    main_worktree_clean,
    run_cursor_repair_handoff,
    run_verification_suite,
    write_incident_report,
)
from .repair_policy import (
    budget_state,
    default_repair_budget,
    incident_auto_attempt_guard,
    normalize_repo_paths,
    patch_guardrails,
    record_model_invocation,
    record_patch_attempt,
)
from .repair_prompts import (
    build_challenger_patch_prompt,
    build_deep_debug_prompt,
    build_primary_patch_prompt,
    build_triage_prompt,
)
from .repair_types import (
    Incident,
    PatchAttempt,
    PatchProposal,
    RepairPlan,
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
FILE_PATH_RE = re.compile(r"([\w./-]+\.(?:py|sh|md|json|ya?ml|ts|tsx|js|jsx|toml|ini))")


def _clip(value: Any, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _run_subprocess(argv: List[str], *, cwd: Path) -> Dict[str, Any]:
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": int(proc.returncode),
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
    }


def _audit_task_id(conn: Any, source_task_id: str) -> str:
    tid = str(source_task_id or "").strip()
    if tid and task_exists(conn, tid):
        return tid
    ensure_system_task(conn)
    return SYSTEM_TASK_ID


def _recent_diff_summary(repo_path: Path) -> str:
    status = _run_subprocess(["git", "status", "--short"], cwd=repo_path)
    diff = _run_subprocess(["git", "diff", "--stat"], cwd=repo_path)
    parts = []
    if str(status.get("stdout") or "").strip():
        parts.append("git status --short:\n" + str(status.get("stdout") or "").strip())
    if str(diff.get("stdout") or "").strip():
        parts.append("git diff --stat:\n" + str(diff.get("stdout") or "").strip())
    if not parts:
        head = _run_subprocess(["git", "show", "--stat", "--oneline", "--no-patch", "HEAD"], cwd=repo_path)
        if str(head.get("stdout") or "").strip():
            parts.append(str(head.get("stdout") or "").strip())
    return _clip("\n\n".join(parts), 2000)


def _extract_suspected_files(text: Any, *, repo_path: Path) -> List[str]:
    out: List[str] = []
    for match in FILE_PATH_RE.finditer(str(text or "")):
        path = str(match.group(1) or "").strip()
        if not path or path.startswith("/"):
            continue
        candidate = (repo_path / path).resolve()
        try:
            candidate.relative_to(repo_path.resolve())
        except ValueError:
            continue
        rel = str(candidate.relative_to(repo_path.resolve()))
        if rel not in out:
            out.append(rel)
        if len(out) >= 12:
            break
    return out


def _extract_failing_tests(text: Any) -> List[str]:
    out: List[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("FAIL:", "ERROR:")):
            out.append(line)
        elif "::" in line and any(token in line.lower() for token in ("failed", "error")):
            out.append(line)
        if len(out) >= 10:
            break
    return out


def _heuristic_triage(text: str) -> Dict[str, Any]:
    lowered = text.lower()
    if any(token in lowered for token in ("permission denied", "secret", "token", "billing", "migration")):
        return {
            "classification": "unclear_or_unsafe",
            "probable_root_cause": "The failure touches a protected or high-risk area that should not be auto-fixed blindly.",
            "confidence": 0.8,
            "safe_to_auto_attempt": False,
            "needs_human_review": True,
        }
    if any(token in lowered for token in ("modulenotfounderror", "importerror", "no module named", "command not found")):
        return {
            "classification": "dependency_issue",
            "probable_root_cause": "A required dependency or executable appears to be missing or not importable.",
            "confidence": 0.8,
            "safe_to_auto_attempt": True,
            "needs_human_review": False,
        }
    if any(token in lowered for token in ("env", "config", "configuration", "settings", "keyerror")):
        return {
            "classification": "config_issue",
            "probable_root_cause": "The failure looks tied to configuration or runtime settings.",
            "confidence": 0.62,
            "safe_to_auto_attempt": True,
            "needs_human_review": False,
        }
    if any(token in lowered for token in ("connection refused", "timed out", "dns", "network", "502", "503")):
        return {
            "classification": "infra_issue",
            "probable_root_cause": "The failure looks external or environment-bound rather than a narrow code defect.",
            "confidence": 0.75,
            "safe_to_auto_attempt": False,
            "needs_human_review": True,
        }
    if any(token in lowered for token in ("schema", "contract", "validationerror", "unexpected field", "missing field")):
        return {
            "classification": "data_contract_issue",
            "probable_root_cause": "A producer/consumer contract or validation rule appears out of sync.",
            "confidence": 0.7,
            "safe_to_auto_attempt": True,
            "needs_human_review": False,
        }
    if any(token in lowered for token in ("flaky", "intermittent", "sporadic", "race condition")):
        return {
            "classification": "flaky_test",
            "probable_root_cause": "The failure appears non-deterministic or timing-sensitive.",
            "confidence": 0.7,
            "safe_to_auto_attempt": False,
            "needs_human_review": True,
        }
    return {
        "classification": "code_bug",
        "probable_root_cause": "The failure most likely comes from a narrow code-level defect.",
        "confidence": 0.58,
        "safe_to_auto_attempt": True,
        "needs_human_review": False,
    }


def _detect_incident_from_verification(
    *,
    repo_path: Path,
    verification_report: Dict[str, Any],
    source_task_id: str,
) -> Incident | None:
    checks = verification_report.get("checks") if isinstance(verification_report.get("checks"), list) else []
    failed = [row for row in checks if isinstance(row, dict) and not bool(row.get("passed"))]
    if not failed:
        return None
    primary = failed[0]
    text_blob = "\n\n".join(
        _clip(row.get("output_excerpt") or "", 1200) for row in failed[:3] if row.get("output_excerpt")
    )
    triage = _heuristic_triage(text_blob or primary.get("label") or "")
    summary = _clip(
        f"{primary.get('label') or primary.get('check_id') or 'verification'} failed: "
        f"{(text_blob or primary.get('command') or 'verification failure').splitlines()[0]}",
        500,
    )
    recent_diff = _recent_diff_summary(repo_path)
    fingerprint = hashlib.sha256(
        "|".join(
            [
                str(primary.get("check_id") or ""),
                summary,
                _clip(text_blob, 400),
            ]
        ).encode("utf-8")
    ).hexdigest()[:20]
    return Incident(
        incident_id=new_incident_id(),
        timestamp=time.time(),
        source="verification",
        error_type=str(triage["classification"]),
        summary=summary,
        stack_trace=_clip(text_blob, 2400),
        failing_tests=_extract_failing_tests(text_blob),
        suspected_files=_extract_suspected_files(text_blob + "\n" + recent_diff, repo_path=repo_path),
        recent_diff=[recent_diff] if recent_diff else [],
        confidence=float(triage["confidence"]),
        safe_to_attempt=bool(triage["safe_to_auto_attempt"]),
        attempt_count=0,
        status="open",
        probable_root_cause=str(triage["probable_root_cause"]),
        recommended_repair_scope="1-3 files in allowed auto-repair roots.",
        source_task_id=source_task_id,
        fingerprint=fingerprint,
        verification=dict(verification_report),
        metadata={"failed_check": dict(primary)},
    )


def _select_context_files(repo_path: Path, incident: Incident) -> List[Dict[str, Any]]:
    paths = normalize_repo_paths(incident.suspected_files)
    context_files: List[Dict[str, Any]] = []
    max_bytes = int(os.environ.get("ANDREA_REPAIR_CONTEXT_BYTES", "6000"))
    for rel in paths[:6]:
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


def _proposal_from_model(result: Dict[str, Any]) -> PatchProposal:
    payload = dict(result.get("payload") or {})
    return PatchProposal(
        model_used=_model_used(result),
        reasoning_summary=_clip(payload.get("reasoning_summary") or payload.get("critique_of_previous_attempt") or "", 1200),
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


def _plan_from_model(result: Dict[str, Any], *, incident_id: str) -> RepairPlan:
    payload = dict(result.get("payload") or {})
    return RepairPlan(
        plan_id=new_plan_id(),
        incident_id=incident_id,
        model_used=_model_used(result),
        root_cause=_clip(payload.get("root_cause") or "", 1600),
        steps=[str(v) for v in (payload.get("steps") or []) if str(v).strip()][:16],
        files_to_modify=normalize_repo_paths(payload.get("files_to_modify")),
        risks=[str(v) for v in (payload.get("risks") or []) if str(v).strip()][:12],
        verification_plan=[str(v) for v in (payload.get("verification_plan") or []) if str(v).strip()][:12],
        stop_conditions=[str(v) for v in (payload.get("stop_conditions") or []) if str(v).strip()][:12],
        cursor_handoff_prompt=_clip(payload.get("handoff_summary") or "", 2000),
        status="planned",
        metadata=payload,
    )


def _commit_message(incident: Incident) -> str:
    kind = str(incident.error_type or "incident").replace("_", "-")
    return f"fix(repair): resolve {kind} incident {incident.incident_id[:8]}"


def _run_patch_attempt(
    conn: Any,
    *,
    repo_path: Path,
    audit_task_id: str,
    incident: Incident,
    attempt_number: int,
    stage: str,
    prompt: str,
    budget: Any,
    verification_checks: List[VerificationCheck],
) -> PatchAttempt:
    attempt = PatchAttempt(
        attempt_id=new_attempt_id(),
        incident_id=incident.incident_id,
        attempt_number=attempt_number,
        stage=stage,
        model_used="",
        status="running",
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
            },
        )
        return attempt
    proposal = _proposal_from_model(model_result)
    attempt.model_used = proposal.model_used
    attempt.files_touched = list(proposal.files_touched)
    attempt.diff = proposal.diff
    attempt.reasoning_summary = proposal.reasoning_summary
    guard = patch_guardrails(proposal.as_dict(), attempt_number=attempt_number)
    if not proposal.safe_to_apply or not guard.get("allowed"):
        attempt.status = "blocked"
        attempt.error = "; ".join(guard.get("reasons") or []) or "model_marked_patch_unsafe"
        attempt.updated_at = time.time()
        attempt.metadata["guardrails"] = guard
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
            },
        )
        return attempt
    verification = run_verification_suite(
        checks=verification_checks,
        cwd_override=Path(attempt.worktree_path),
        repo_path=repo_path,
    )
    attempt.verification_results = verification
    if verification.get("passed"):
        commit = commit_worktree_if_clean(
            worktree_path=Path(attempt.worktree_path),
            message=_commit_message(incident),
        )
        if not commit.get("ok"):
            cleanup_worktree(
                repo_path=repo_path,
                worktree_path=attempt.worktree_path,
                branch=attempt.branch,
                keep_branch=False,
            )
            attempt.rollback_performed = True
            attempt.status = "failed"
            attempt.error = str(commit.get("error") or "commit failed")
            attempt.updated_at = time.time()
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
    source_task_id: str = "",
    cursor_execute: bool = False,
    write_report: bool = True,
) -> Dict[str, Any]:
    ensure_system_task(conn)
    repo = Path(repo_path or REPO_ROOT).expanduser()
    audit_task_id = _audit_task_id(conn, source_task_id)
    verification = dict(verification_report or {})
    verification_checks = build_default_verification_checks(repo)
    if not verification:
        verification = run_verification_suite(
            checks=verification_checks,
            cwd_override=repo,
            repo_path=repo,
        )
    incident = (
        Incident.from_dict(incident_payload or {})
        if incident_payload
        else _detect_incident_from_verification(
            repo_path=repo,
            verification_report=verification,
            source_task_id=source_task_id,
        )
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
    incident.verification = verification
    save_incident(conn, incident.as_dict())
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
        },
    )
    structured_log(
        "incident_repair_opened",
        incident_id=incident.incident_id,
        source=incident.source,
        error_type=incident.error_type,
    )

    budget = default_repair_budget()
    context_files = _select_context_files(repo, incident)
    triage_prompt = build_triage_prompt(
        incident=incident,
        verification_report=verification,
        recent_diff_summary=_recent_diff_summary(repo),
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
    heuristic = _heuristic_triage(incident.stack_trace or incident.summary)
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
    incident.confidence = float(
        triage_payload.get("confidence") or incident.confidence or heuristic["confidence"]
    )
    incident.safe_to_attempt = bool(
        triage_payload.get("safe_to_auto_attempt")
        if triage_result.get("ok")
        else incident.safe_to_attempt or heuristic["safe_to_auto_attempt"]
    )
    incident.status = "triaged"
    save_incident(conn, incident.as_dict())
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
        },
    )
    context_files = _select_context_files(repo, incident)

    guard = incident_auto_attempt_guard(incident)
    clean_state = main_worktree_clean(repo)
    attempts: List[PatchAttempt] = []
    report_path = ""
    if not clean_state.get("clean"):
        guard = {
            **guard,
            "allowed": False,
            "reasons": list(guard.get("reasons") or []) + ["main_worktree_dirty"],
        }

    if guard.get("allowed"):
        record_patch_attempt(budget)
        primary_prompt = build_primary_patch_prompt(
            incident=incident,
            context_files=context_files,
            attempt_number=1,
            budget_state=budget_state(budget),
        )
        primary_attempt = _run_patch_attempt(
            conn,
            repo_path=repo,
            audit_task_id=audit_task_id,
            incident=incident,
            attempt_number=1,
            stage="primary_patch",
            prompt=primary_prompt,
            budget=budget,
            verification_checks=verification_checks,
        )
        attempts.append(primary_attempt)
        incident.attempt_count = 1
        if primary_attempt.success:
            incident.status = "resolved"
            save_incident(conn, incident.as_dict())
            append_event(
                conn,
                audit_task_id,
                EventType.INCIDENT_RESOLVED,
                {
                    "incident_id": incident.incident_id,
                    "attempt_id": primary_attempt.attempt_id,
                    "attempt_number": 1,
                    "branch": primary_attempt.branch,
                    "commit_sha": str(primary_attempt.metadata.get("commit_sha") or ""),
                    "summary": incident.summary,
                },
            )
            metric_log("incident_repair_resolved", incident_id=incident.incident_id, attempts=1)
            return {
                "ok": True,
                "resolved": True,
                "status": incident.status,
                "incident": incident.as_dict(),
                "attempts": [attempt.as_dict() for attempt in attempts],
                "plan": {},
                "verification_report": verification,
                "report_path": report_path,
            }

        if budget.patch_attempts_used < budget.max_patch_attempts:
            record_patch_attempt(budget)
            challenger_prompt = build_challenger_patch_prompt(
                incident=incident,
                failed_attempt=primary_attempt,
                context_files=context_files,
                attempt_number=2,
                budget_state=budget_state(budget),
            )
            challenger_attempt = _run_patch_attempt(
                conn,
                repo_path=repo,
                audit_task_id=audit_task_id,
                incident=incident,
                attempt_number=2,
                stage="challenger_patch",
                prompt=challenger_prompt,
                budget=budget,
                verification_checks=verification_checks,
            )
            attempts.append(challenger_attempt)
            incident.attempt_count = 2
            if challenger_attempt.success:
                incident.status = "resolved"
                save_incident(conn, incident.as_dict())
                append_event(
                    conn,
                    audit_task_id,
                    EventType.INCIDENT_RESOLVED,
                    {
                        "incident_id": incident.incident_id,
                        "attempt_id": challenger_attempt.attempt_id,
                        "attempt_number": 2,
                        "branch": challenger_attempt.branch,
                        "commit_sha": str(challenger_attempt.metadata.get("commit_sha") or ""),
                        "summary": incident.summary,
                    },
                )
                metric_log("incident_repair_resolved", incident_id=incident.incident_id, attempts=2)
                return {
                    "ok": True,
                    "resolved": True,
                    "status": incident.status,
                    "incident": incident.as_dict(),
                    "attempts": [attempt.as_dict() for attempt in attempts],
                    "plan": {},
                    "verification_report": verification,
                    "report_path": report_path,
                }

    deep_prompt = build_deep_debug_prompt(
        incident=incident,
        attempts=attempts,
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
        _plan_from_model(deep_result, incident_id=incident.incident_id)
        if deep_result.get("ok")
        else RepairPlan(
            plan_id=new_plan_id(),
            incident_id=incident.incident_id,
            model_used=_model_used(deep_result),
            root_cause="Deep repair planning failed before a reliable plan was produced.",
            steps=[],
            files_to_modify=[],
            risks=["Model planning lane failed."],
            verification_plan=[],
            stop_conditions=["Stop and hand off for human review."],
            status="failed",
            metadata={"error": str(deep_result.get("error") or "")},
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
        },
    )

    cursor_handoff: Dict[str, Any] = {}
    if cursor_execute and clean_state.get("clean"):
        cursor_handoff = run_cursor_repair_handoff(
            repo_path=repo,
            incident=incident,
            plan=plan,
            attempts=attempts,
            verification_checks=verification_checks,
            cursor_mode=(os.environ.get("ANDREA_REPAIR_CURSOR_MODE") or "auto").strip() or "auto",
        )
        if cursor_handoff.get("ok"):
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
                },
            )

    incident.status = "escalated"
    save_incident(conn, incident.as_dict())
    if write_report:
        report_path = write_incident_report(
            repo_path=repo,
            incident=incident,
            attempts=attempts,
            plan=plan,
            verification_report=verification,
            status=incident.status,
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
            "report_path": report_path,
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
        "status": incident.status,
        "incident": incident.as_dict(),
        "attempts": [attempt.as_dict() for attempt in attempts],
        "plan": plan.as_dict(),
        "verification_report": verification,
        "guard": guard,
        "cursor_handoff": cursor_handoff,
        "report_path": report_path,
        "repair_history": {
            "incident": get_incident(conn, incident.incident_id),
            "attempts": list_repair_attempts(conn, incident.incident_id),
            "latest_plan": get_latest_repair_plan(conn, incident.incident_id),
        },
    }

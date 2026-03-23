"""Incident detection and normalization helpers for Andrea repairs."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from .repair_policy import normalize_repo_paths
from .repair_types import Incident

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


def recent_diff_summary(repo_path: Path) -> str:
    status = _run_subprocess(["git", "status", "--short"], cwd=repo_path)
    diff = _run_subprocess(["git", "diff", "--stat"], cwd=repo_path)
    parts: List[str] = []
    if str(status.get("stdout") or "").strip():
        parts.append("git status --short:\n" + str(status.get("stdout") or "").strip())
    if str(diff.get("stdout") or "").strip():
        parts.append("git diff --stat:\n" + str(diff.get("stdout") or "").strip())
    if not parts:
        head = _run_subprocess(
            ["git", "show", "--stat", "--oneline", "--no-patch", "HEAD"],
            cwd=repo_path,
        )
        if str(head.get("stdout") or "").strip():
            parts.append(str(head.get("stdout") or "").strip())
    return _clip("\n\n".join(parts), 2000)


def extract_suspected_files(text: Any, *, repo_path: Path) -> List[str]:
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


def extract_failing_tests(text: Any) -> List[str]:
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


def heuristic_triage(text: str) -> Dict[str, Any]:
    lowered = str(text or "").lower()
    if any(
        token in lowered
        for token in ("permission denied", "secret", "token", "billing", "payment", "migration")
    ):
        return {
            "classification": "unclear_or_unsafe",
            "probable_root_cause": (
                "The failure touches a protected or high-risk area that should not be auto-fixed blindly."
            ),
            "confidence": 0.82,
            "safe_to_auto_attempt": False,
            "needs_human_review": True,
        }
    if any(
        token in lowered
        for token in ("modulenotfounderror", "importerror", "no module named", "command not found")
    ):
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
    if any(
        token in lowered
        for token in ("schema", "contract", "validationerror", "unexpected field", "missing field")
    ):
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


def _first_nonempty_line(text: Any) -> str:
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if line:
            return line
    return ""


def _environment_name(payload: Dict[str, Any]) -> str:
    return (
        str(payload.get("environment") or "").strip()
        or str(os.environ.get("ANDREA_ENVIRONMENT") or "").strip()
        or str(os.environ.get("APP_ENV") or "").strip()
        or str(os.environ.get("ENVIRONMENT") or "").strip()
        or "local"
    )


def _service_name(payload: Dict[str, Any]) -> str:
    return (
        str(payload.get("service_name") or "").strip()
        or str(os.environ.get("ANDREA_SERVICE_NAME") or "").strip()
        or "andrea_sync"
    )


def _fingerprint(*parts: Any) -> str:
    blob = "|".join(_clip(part, 500) for part in parts if _clip(part, 500))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


def _base_incident(
    *,
    source: str,
    summary: str,
    details_text: str,
    repo_path: Path,
    source_task_id: str,
    payload: Dict[str, Any],
    error_type_hint: str = "",
) -> Incident:
    triage = heuristic_triage(details_text or summary or error_type_hint)
    incident = Incident(
        incident_id=str(payload.get("incident_id") or ""),
        created_at=float(payload.get("created_at") or payload.get("timestamp") or time.time()),
        updated_at=float(payload.get("updated_at") or payload.get("created_at") or payload.get("timestamp") or time.time()),
        source=source,
        service_name=_service_name(payload),
        environment=_environment_name(payload),
        error_type=str(payload.get("error_type") or error_type_hint or triage["classification"]),
        summary=_clip(payload.get("summary") or summary, 500),
        stack_trace=_clip(payload.get("stack_trace") or details_text, 2400),
        failing_tests=extract_failing_tests(payload.get("stack_trace") or details_text),
        suspected_files=normalize_repo_paths(payload.get("suspected_files"))
        or extract_suspected_files(details_text, repo_path=repo_path),
        recent_diff=normalize_repo_paths(payload.get("recent_diff"))
        if isinstance(payload.get("recent_diff"), list)
        else [recent_diff_summary(repo_path)],
        triage_confidence=float(payload.get("triage_confidence") or payload.get("confidence") or triage["confidence"]),
        safe_to_attempt=bool(
            payload.get("safe_to_attempt")
            if payload.get("safe_to_attempt") is not None
            else triage["safe_to_auto_attempt"]
        ),
        attempt_count=int(payload.get("attempt_count") or 0),
        current_state=str(payload.get("current_state") or payload.get("status") or "detected"),
        history=list(payload.get("history") or []),
        probable_root_cause=_clip(
            payload.get("probable_root_cause") or triage["probable_root_cause"],
            1200,
        ),
        recommended_repair_scope=_clip(
            payload.get("recommended_repair_scope")
            or "1-3 files in allowed auto-repair roots.",
            500,
        ),
        source_task_id=str(payload.get("source_task_id") or source_task_id or ""),
        fingerprint=str(payload.get("fingerprint") or ""),
        verification=dict(payload.get("verification") or {}) if isinstance(payload.get("verification"), dict) else {},
        metadata=dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {},
    )
    if not incident.recent_diff:
        incident.recent_diff = [recent_diff_summary(repo_path)]
    if not incident.fingerprint:
        incident.fingerprint = _fingerprint(
            incident.source,
            incident.error_type,
            incident.summary,
            incident.stack_trace,
        )
    if not incident.history:
        incident.record_state(
            incident.current_state or "detected",
            reason=f"{incident.source} incident detected",
            extra={
                "service_name": incident.service_name,
                "environment": incident.environment,
            },
        )
    return incident


def incident_from_manual_payload(
    *,
    repo_path: Path,
    payload: Dict[str, Any],
    source_task_id: str,
) -> Incident:
    manual = Incident.from_dict(payload or {})
    if not manual.service_name:
        manual.service_name = _service_name(payload)
    if not manual.environment:
        manual.environment = _environment_name(payload)
    if not manual.source:
        manual.source = "manual_submission"
    if not manual.summary:
        manual.summary = _clip(_first_nonempty_line(manual.stack_trace) or "Manual incident submission", 500)
    if not manual.fingerprint:
        manual.fingerprint = _fingerprint(
            manual.source,
            manual.error_type,
            manual.summary,
            manual.stack_trace,
        )
    if not manual.recent_diff:
        manual.recent_diff = [recent_diff_summary(repo_path)]
    if not manual.source_task_id:
        manual.source_task_id = str(source_task_id or "")
    if not manual.history:
        manual.record_state(
            manual.current_state or "detected",
            reason="manual incident submitted",
            extra={
                "service_name": manual.service_name,
                "environment": manual.environment,
            },
        )
    return manual


def incident_from_verification_report(
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
        _clip(row.get("output_excerpt") or "", 1200)
        for row in failed[:3]
        if row.get("output_excerpt")
    )
    label = str(primary.get("label") or primary.get("check_id") or "verification").strip()
    check_id = str(primary.get("check_id") or "").strip().lower()
    source = "test_failure"
    if check_id == "health" or "health" in label.lower():
        source = "health_check_failure"
    summary = _clip(
        f"{label} failed: {(text_blob or primary.get('command') or 'verification failure').splitlines()[0]}",
        500,
    )
    incident = _base_incident(
        source=source,
        summary=summary,
        details_text=text_blob or str(primary.get("command") or ""),
        repo_path=repo_path,
        source_task_id=source_task_id,
        payload={
            "error_type": check_id or "verification_failure",
            "verification": dict(verification_report),
            "metadata": {"failed_check": dict(primary)},
        },
        error_type_hint=check_id or "verification_failure",
    )
    incident.failing_tests = extract_failing_tests(text_blob) or incident.failing_tests
    incident.suspected_files = normalize_repo_paths(incident.suspected_files) or extract_suspected_files(
        text_blob + "\n" + recent_diff_summary(repo_path),
        repo_path=repo_path,
    )
    return incident


def incident_from_runtime_error(
    *,
    repo_path: Path,
    runtime_error: Dict[str, Any],
    source_task_id: str,
) -> Incident | None:
    if not runtime_error:
        return None
    summary = _clip(
        runtime_error.get("summary")
        or _first_nonempty_line(runtime_error.get("error") or runtime_error.get("stack_trace"))
        or "Runtime exception detected",
        500,
    )
    details = "\n".join(
        part
        for part in (
            str(runtime_error.get("error") or "").strip(),
            str(runtime_error.get("stack_trace") or "").strip(),
            str(runtime_error.get("log_excerpt") or "").strip(),
        )
        if part
    )
    incident = _base_incident(
        source="runtime_exception",
        summary=summary,
        details_text=details,
        repo_path=repo_path,
        source_task_id=source_task_id,
        payload=runtime_error,
        error_type_hint=str(runtime_error.get("exception_type") or runtime_error.get("error_type") or "runtime_exception"),
    )
    if runtime_error.get("suspected_files"):
        incident.suspected_files = normalize_repo_paths(runtime_error.get("suspected_files"))
    return incident


def incident_from_health_failure(
    *,
    repo_path: Path,
    health_failure: Dict[str, Any],
    source_task_id: str,
) -> Incident | None:
    if not health_failure:
        return None
    summary = _clip(
        health_failure.get("summary")
        or _first_nonempty_line(health_failure.get("error") or health_failure.get("details"))
        or "Health check failed",
        500,
    )
    details = "\n".join(
        part
        for part in (
            str(health_failure.get("error") or "").strip(),
            str(health_failure.get("details") or "").strip(),
            str(health_failure.get("check") or "").strip(),
        )
        if part
    )
    return _base_incident(
        source="health_check_failure",
        summary=summary,
        details_text=details,
        repo_path=repo_path,
        source_task_id=source_task_id,
        payload=health_failure,
        error_type_hint=str(health_failure.get("error_type") or "health_check_failure"),
    )


def incident_from_log_alert(
    *,
    repo_path: Path,
    log_alert: Dict[str, Any],
    source_task_id: str,
) -> Incident | None:
    if not log_alert:
        return None
    summary = _clip(
        log_alert.get("summary")
        or _first_nonempty_line(log_alert.get("message") or log_alert.get("alert") or log_alert.get("excerpt"))
        or "Log alert detected",
        500,
    )
    details = "\n".join(
        part
        for part in (
            str(log_alert.get("message") or "").strip(),
            str(log_alert.get("excerpt") or "").strip(),
            str(log_alert.get("stack_trace") or "").strip(),
        )
        if part
    )
    return _base_incident(
        source="log_alert",
        summary=summary,
        details_text=details,
        repo_path=repo_path,
        source_task_id=source_task_id,
        payload=log_alert,
        error_type_hint=str(log_alert.get("error_type") or "log_alert"),
    )


def detect_incident(
    *,
    repo_path: Path,
    incident_payload: Dict[str, Any] | None = None,
    verification_report: Dict[str, Any] | None = None,
    runtime_error: Dict[str, Any] | None = None,
    health_failure: Dict[str, Any] | None = None,
    log_alert: Dict[str, Any] | None = None,
    source_task_id: str = "",
) -> Incident | None:
    if incident_payload:
        return incident_from_manual_payload(
            repo_path=repo_path,
            payload=dict(incident_payload),
            source_task_id=source_task_id,
        )
    if runtime_error:
        return incident_from_runtime_error(
            repo_path=repo_path,
            runtime_error=dict(runtime_error),
            source_task_id=source_task_id,
        )
    if health_failure:
        return incident_from_health_failure(
            repo_path=repo_path,
            health_failure=dict(health_failure),
            source_task_id=source_task_id,
        )
    if log_alert:
        return incident_from_log_alert(
            repo_path=repo_path,
            log_alert=dict(log_alert),
            source_task_id=source_task_id,
        )
    if verification_report:
        return incident_from_verification_report(
            repo_path=repo_path,
            verification_report=dict(verification_report),
            source_task_id=source_task_id,
        )
    return None

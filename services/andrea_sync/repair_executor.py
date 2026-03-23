"""Execution helpers for the incident-driven repair pipeline."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .cursor_plan_execute import (
    executor_model_for_lane,
    extract_plan_text_from_conversation,
    fetch_agent_conversation_payload,
    plan_first_enabled,
    planner_model_for_lane,
    plan_text_usable,
    run_cursor_handoff_cli,
)
from .repair_prompts import (
    build_cursor_handoff_prompt,
    build_cursor_planner_prompt,
)
from .repair_types import Incident, PatchAttempt, RepairPlan, VerificationCheck

REPO_ROOT = Path(__file__).resolve().parents[2]
CURSOR_HANDOFF_SCRIPT = REPO_ROOT / "skills" / "cursor_handoff" / "scripts" / "cursor_handoff.py"
REPAIR_ARTIFACT_VERSION = "v2"

# Align with skills/cursor_handoff/scripts/cursor_handoff.py TERMINAL_STATUSES
CURSOR_TERMINAL_AGENT_STATUSES = frozenset({"FINISHED", "FAILED", "CANCELLED", "STOPPED", "EXPIRED"})


def _cursor_handoff_poll_max_attempts() -> int:
    raw = (os.environ.get("ANDREA_REPAIR_CURSOR_POLL_MAX_ATTEMPTS") or "3").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 3
    return max(0, min(n, 120))


def _cursor_handoff_poll_interval_seconds() -> float:
    raw = (os.environ.get("ANDREA_REPAIR_CURSOR_POLL_INTERVAL_SECONDS") or "3").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 3.0


def _post_cursor_verify_enabled() -> bool:
    raw = (os.environ.get("ANDREA_REPAIR_POST_CURSOR_VERIFY") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def post_cursor_verification_enabled() -> bool:
    """Public alias: whether post-Cursor detached worktree verification is enabled (repair + self-heal)."""
    return _post_cursor_verify_enabled()


def resolve_branch_for_verification(repo_path: Path, branch: str) -> Dict[str, Any]:
    """Resolve a local or origin/* ref for post-Cursor verification worktrees."""
    b = str(branch or "").strip()
    if not b:
        return {"ok": False, "error": "empty_branch"}
    heads = _run_subprocess(
        ["git", "show-ref", "--verify", f"refs/heads/{b}"],
        cwd=repo_path,
        timeout_seconds=30,
    )
    if heads.get("ok"):
        return {"ok": True, "ref": b, "source": "local_branch"}
    origin_ref = f"origin/{b}"
    rem = _run_subprocess(["git", "rev-parse", "--verify", origin_ref], cwd=repo_path, timeout_seconds=30)
    if rem.get("ok"):
        return {"ok": True, "ref": origin_ref, "source": "remote_tracking"}
    fetch = _run_subprocess(
        ["git", "fetch", "origin", b],
        cwd=repo_path,
        timeout_seconds=max(30, int(os.environ.get("ANDREA_REPAIR_CURSOR_FETCH_TIMEOUT_SECONDS", "300"))),
    )
    if not fetch.get("ok"):
        err_txt = str(fetch.get("stderr") or fetch.get("stdout") or "git fetch origin failed").strip()
        if len(err_txt) > 800:
            err_txt = err_txt[:797] + "..."
        return {"ok": False, "error": err_txt}
    rem2 = _run_subprocess(["git", "rev-parse", "--verify", origin_ref], cwd=repo_path, timeout_seconds=30)
    if rem2.get("ok"):
        return {"ok": True, "ref": origin_ref, "source": "fetched"}
    return {"ok": False, "error": f"branch not found after fetch: {b}"}


def create_detached_verification_worktree(
    repo_path: Path,
    *,
    git_ref: str,
    incident_id: str,
    stage_suffix: str,
) -> Dict[str, Any]:
    root = Path(
        os.environ.get("ANDREA_REPAIR_WORKTREE_ROOT", str(repo_path / ".andrea-repair-worktrees"))
    )
    root.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9._-]+", "-", stage_suffix.lower()).strip("-") or "verify"
    wt_name = f"verify-{incident_id[:10]}-{safe}-{uuid.uuid4().hex[:6]}"
    worktree_path = root / wt_name
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
    result = _run_subprocess(
        ["git", "worktree", "add", "--detach", str(worktree_path), git_ref],
        cwd=repo_path,
        timeout_seconds=int(os.environ.get("ANDREA_REPAIR_WORKTREE_TIMEOUT_SECONDS", "120")),
    )
    if not result.get("ok"):
        err_txt = str(result.get("stderr") or result.get("stdout") or "worktree add failed").strip()
        if len(err_txt) > 800:
            err_txt = err_txt[:797] + "..."
        return {"ok": False, "error": err_txt, "worktree_path": str(worktree_path)}
    return {"ok": True, "worktree_path": str(worktree_path), "git_ref": git_ref}


def verify_cursor_branch_in_isolated_worktree(
    *,
    repo_path: Path,
    branch: str,
    incident_id: str,
    verification_checks: List[VerificationCheck],
) -> Dict[str, Any]:
    """
    Run the default verification suite on the given branch in a detached worktree.
    Does not merge to main. Removes the temporary worktree when done; keeps remote/local branches.
    """
    resolved = resolve_branch_for_verification(repo_path, branch)
    if not resolved.get("ok"):
        return {
            "ok": False,
            "passed": False,
            "error": str(resolved.get("error") or "resolve_branch_failed"),
            "verification_report": {},
            "ref_source": "",
        }
    git_ref = str(resolved.get("ref") or "")
    wt = create_detached_verification_worktree(
        repo_path,
        git_ref=git_ref,
        incident_id=incident_id,
        stage_suffix="post-cursor",
    )
    if not wt.get("ok"):
        return {
            "ok": False,
            "passed": False,
            "error": str(wt.get("error") or "worktree_failed"),
            "verification_report": {},
            "ref_source": str(resolved.get("source") or ""),
        }
    wt_path = str(wt.get("worktree_path") or "")
    try:
        report = run_verification_suite(
            checks=verification_checks,
            cwd_override=Path(wt_path),
            repo_path=repo_path,
        )
        passed = bool(report.get("passed"))
        return {
            "ok": True,
            "passed": passed,
            "verification_report": report,
            "ref_source": str(resolved.get("source") or ""),
            "git_ref": git_ref,
            "error": "" if passed else str(report.get("summary") or "verification_failed"),
        }
    finally:
        cleanup_worktree(
            repo_path=repo_path,
            worktree_path=wt_path,
            branch="",
            keep_branch=True,
        )


def _clip(value: Any, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _run_subprocess(
    command: List[str] | str,
    *,
    cwd: Path,
    shell: bool = False,
    timeout_seconds: int = 900,
) -> Dict[str, Any]:
    proc = subprocess.run(
        command,
        cwd=str(cwd),
        shell=shell,
        text=True,
        capture_output=True,
        check=False,
        timeout=max(5, int(timeout_seconds)),
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": int(proc.returncode),
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "command": command,
    }


def build_default_verification_checks(repo_path: Path) -> List[VerificationCheck]:
    tests_dir = repo_path / "tests"
    lint_command = os.environ.get("ANDREA_REPAIR_LINT_COMMAND", "").strip()
    typecheck_command = os.environ.get("ANDREA_REPAIR_TYPECHECK_COMMAND", "").strip()
    unit_command = os.environ.get(
        "ANDREA_REPAIR_UNIT_COMMAND", "python3 -m unittest discover -p 'test_*.py'"
    ).strip()
    integration_command = os.environ.get(
        "ANDREA_REPAIR_INTEGRATION_COMMAND", "bash scripts/test_integration.sh"
    ).strip()
    build_command = os.environ.get(
        "ANDREA_REPAIR_BUILD_COMMAND", "python3 -m compileall services scripts"
    ).strip()
    smoke_command = os.environ.get("ANDREA_REPAIR_SMOKE_COMMAND", "").strip()
    health_command = os.environ.get("ANDREA_REPAIR_HEALTH_COMMAND", "").strip()
    if not health_command and os.environ.get("ANDREA_SYNC_URL"):
        health_command = "python3 scripts/andrea_sync_health.py"
    return [
        VerificationCheck(
            check_id="lint",
            label="Lint",
            command=lint_command,
            cwd=str(repo_path),
            required=False,
            enabled=bool(lint_command),
            timeout_seconds=int(os.environ.get("ANDREA_REPAIR_LINT_TIMEOUT_SECONDS", "600")),
            tags=["lint"],
        ),
        VerificationCheck(
            check_id="typecheck",
            label="Typecheck",
            command=typecheck_command,
            cwd=str(repo_path),
            required=False,
            enabled=bool(typecheck_command),
            timeout_seconds=int(os.environ.get("ANDREA_REPAIR_TYPECHECK_TIMEOUT_SECONDS", "600")),
            tags=["typecheck"],
        ),
        VerificationCheck(
            check_id="unit",
            label="Unit Tests",
            command=unit_command,
            cwd=str(tests_dir if tests_dir.is_dir() else repo_path),
            required=True,
            enabled=bool(unit_command),
            timeout_seconds=int(os.environ.get("ANDREA_REPAIR_UNIT_TIMEOUT_SECONDS", "900")),
            tags=["tests", "unit"],
        ),
        VerificationCheck(
            check_id="integration",
            label="Integration Tests",
            command=integration_command,
            cwd=str(repo_path),
            required=False,
            enabled=bool(integration_command),
            timeout_seconds=int(
                os.environ.get("ANDREA_REPAIR_INTEGRATION_TIMEOUT_SECONDS", "1200")
            ),
            tags=["tests", "integration"],
        ),
        VerificationCheck(
            check_id="build",
            label="Build/Compile",
            command=build_command,
            cwd=str(repo_path),
            required=False,
            enabled=bool(build_command),
            timeout_seconds=int(os.environ.get("ANDREA_REPAIR_BUILD_TIMEOUT_SECONDS", "600")),
            tags=["build"],
        ),
        VerificationCheck(
            check_id="smoke",
            label="Smoke Test",
            command=smoke_command,
            cwd=str(repo_path),
            required=False,
            enabled=bool(smoke_command),
            timeout_seconds=int(os.environ.get("ANDREA_REPAIR_SMOKE_TIMEOUT_SECONDS", "600")),
            tags=["smoke"],
        ),
        VerificationCheck(
            check_id="health",
            label="Health Check",
            command=health_command,
            cwd=str(repo_path),
            required=False,
            enabled=bool(health_command),
            timeout_seconds=int(os.environ.get("ANDREA_REPAIR_HEALTH_TIMEOUT_SECONDS", "300")),
            tags=["health"],
        ),
    ]


def run_verification_suite(
    *,
    checks: List[VerificationCheck],
    cwd_override: Path | None = None,
    repo_path: Path | None = None,
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    failing_required = False
    for check in checks:
        if not check.enabled or not check.command.strip():
            continue
        started_at = time.time()
        command_cwd = Path(check.cwd)
        if cwd_override is not None:
            if command_cwd.is_absolute() and repo_path is not None:
                try:
                    rel = command_cwd.resolve().relative_to(repo_path.resolve())
                    command_cwd = (cwd_override / rel).resolve()
                except ValueError:
                    command_cwd = cwd_override
            elif not command_cwd.is_absolute():
                command_cwd = (cwd_override / command_cwd).resolve()
        result = _run_subprocess(
            check.command,
            cwd=command_cwd,
            shell=True,
            timeout_seconds=check.timeout_seconds,
        )
        duration_seconds = max(0.0, time.time() - started_at)
        stdout_summary = _clip(result.get("stdout") or "", 1200)
        stderr_summary = _clip(result.get("stderr") or "", 1200)
        output_excerpt = _clip(
            "\n".join(
                part for part in (result.get("stdout"), result.get("stderr")) if str(part).strip()
            ),
            2500,
        )
        row = {
            "check_id": check.check_id,
            "label": check.label,
            "command": check.command,
            "cwd": str(command_cwd),
            "passed": bool(result.get("ok")),
            "required": bool(check.required),
            "exit_code": int(result.get("returncode") or 0),
            "duration_seconds": round(duration_seconds, 3),
            "stdout_summary": stdout_summary,
            "stderr_summary": stderr_summary,
            "output_excerpt": output_excerpt,
            "tags": list(check.tags),
        }
        if not row["passed"] and row["required"]:
            failing_required = True
        rows.append(row)
    failed_labels = [row["label"] for row in rows if not row["passed"]]
    failed_check_ids = [row["check_id"] for row in rows if not row["passed"]]
    return {
        "passed": not failing_required and not any(
            row["required"] and not row["passed"] for row in rows
        ),
        "checks": rows,
        "failed_checks": failed_labels,
        "failed_check_ids": failed_check_ids,
        "summary": (
            "All enabled required verification checks passed."
            if not failed_labels
            else "Failed checks: " + ", ".join(failed_labels)
        ),
    }


def compare_verification_reports(
    *,
    baseline_report: Dict[str, Any],
    candidate_report: Dict[str, Any],
) -> Dict[str, Any]:
    baseline_checks = (
        baseline_report.get("checks") if isinstance(baseline_report.get("checks"), list) else []
    )
    candidate_checks = (
        candidate_report.get("checks") if isinstance(candidate_report.get("checks"), list) else []
    )

    def failed_ids(rows: List[Dict[str, Any]]) -> set[str]:
        out: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or bool(row.get("passed")):
                continue
            check_id = str(row.get("check_id") or row.get("label") or "").strip()
            if check_id:
                out.add(check_id)
        return out

    def failed_required_ids(rows: List[Dict[str, Any]]) -> set[str]:
        out: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or bool(row.get("passed")) or not bool(row.get("required")):
                continue
            check_id = str(row.get("check_id") or row.get("label") or "").strip()
            if check_id:
                out.add(check_id)
        return out

    baseline_failed = failed_ids(baseline_checks)
    candidate_failed = failed_ids(candidate_checks)
    baseline_required_failed = failed_required_ids(baseline_checks)
    candidate_required_failed = failed_required_ids(candidate_checks)
    new_failures = sorted(candidate_failed - baseline_failed)
    resolved_failures = sorted(baseline_failed - candidate_failed)
    worse_than_baseline = bool(new_failures or (candidate_required_failed - baseline_required_failed))
    return {
        "baseline_failed": sorted(baseline_failed),
        "candidate_failed": sorted(candidate_failed),
        "new_failures": new_failures,
        "resolved_failures": resolved_failures,
        "worse_than_baseline": worse_than_baseline,
        "summary": (
            "Verification did not regress from baseline."
            if not worse_than_baseline
            else "New failing checks after repair attempt: " + ", ".join(new_failures or sorted(candidate_required_failed - baseline_required_failed))
        ),
    }


def main_worktree_clean(repo_path: Path) -> Dict[str, Any]:
    result = _run_subprocess(["git", "status", "--porcelain"], cwd=repo_path, timeout_seconds=60)
    if not result.get("ok"):
        return {
            "ok": False,
            "clean": False,
            "error": _clip(result.get("stderr") or result.get("stdout") or "git status failed", 500),
        }
    dirty = str(result.get("stdout") or "").strip()
    return {
        "ok": True,
        "clean": not bool(dirty),
        "status": dirty,
    }


def create_sandbox_worktree(repo_path: Path, *, incident_id: str, stage: str) -> Dict[str, Any]:
    root = Path(
        os.environ.get("ANDREA_REPAIR_WORKTREE_ROOT", str(repo_path / ".andrea-repair-worktrees"))
    )
    root.mkdir(parents=True, exist_ok=True)
    safe_stage = re.sub(r"[^a-z0-9._-]+", "-", stage.lower()).strip("-") or "attempt"
    branch = f"repair/{incident_id[:10]}-{safe_stage}-{uuid.uuid4().hex[:6]}"
    worktree_path = root / branch.replace("/", "__")
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
    result = _run_subprocess(
        ["git", "worktree", "add", "-b", branch, str(worktree_path), "HEAD"],
        cwd=repo_path,
        timeout_seconds=int(os.environ.get("ANDREA_REPAIR_WORKTREE_TIMEOUT_SECONDS", "120")),
    )
    if not result.get("ok"):
        return {
            "ok": False,
            "error": _clip(result.get("stderr") or result.get("stdout") or "worktree add failed", 800),
            "branch": branch,
            "worktree_path": str(worktree_path),
        }
    return {
        "ok": True,
        "branch": branch,
        "worktree_path": str(worktree_path),
    }


def sanitize_unified_diff(diff_text: Any) -> str:
    text = str(diff_text or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def apply_unified_diff(*, worktree_path: Path, diff_text: str) -> Dict[str, Any]:
    diff = sanitize_unified_diff(diff_text)
    if not diff:
        return {"ok": False, "error": "diff is empty"}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".patch") as handle:
        handle.write(diff)
        patch_path = Path(handle.name)
    try:
        check = _run_subprocess(
            ["git", "apply", "--check", "--whitespace=nowarn", str(patch_path)],
            cwd=worktree_path,
            timeout_seconds=60,
        )
        use_three_way = False
        if not check.get("ok"):
            check = _run_subprocess(
                ["git", "apply", "--3way", "--check", "--whitespace=nowarn", str(patch_path)],
                cwd=worktree_path,
                timeout_seconds=60,
            )
            use_three_way = bool(check.get("ok"))
        if not check.get("ok"):
            return {
                "ok": False,
                "error": _clip(check.get("stderr") or check.get("stdout") or "git apply check failed", 1200),
            }
        apply_cmd = ["git", "apply", "--whitespace=nowarn", str(patch_path)]
        if use_three_way:
            apply_cmd = ["git", "apply", "--3way", "--whitespace=nowarn", str(patch_path)]
        result = _run_subprocess(apply_cmd, cwd=worktree_path, timeout_seconds=60)
        if not result.get("ok"):
            return {
                "ok": False,
                "error": _clip(result.get("stderr") or result.get("stdout") or "git apply failed", 1200),
            }
        return {"ok": True, "used_three_way": use_three_way}
    finally:
        patch_path.unlink(missing_ok=True)


def commit_worktree_if_clean(*, worktree_path: Path, message: str) -> Dict[str, Any]:
    status = _run_subprocess(["git", "status", "--porcelain"], cwd=worktree_path, timeout_seconds=60)
    if not status.get("ok"):
        return {"ok": False, "error": _clip(status.get("stderr") or status.get("stdout") or "git status failed", 500)}
    if not str(status.get("stdout") or "").strip():
        return {"ok": True, "skipped": True, "reason": "no_changes"}
    add = _run_subprocess(["git", "add", "-A"], cwd=worktree_path, timeout_seconds=60)
    if not add.get("ok"):
        return {"ok": False, "error": _clip(add.get("stderr") or add.get("stdout") or "git add failed", 500)}
    commit = _run_subprocess(["git", "commit", "-m", message], cwd=worktree_path, timeout_seconds=120)
    if not commit.get("ok"):
        return {
            "ok": False,
            "error": _clip(commit.get("stderr") or commit.get("stdout") or "git commit failed", 1200),
        }
    rev = _run_subprocess(["git", "rev-parse", "HEAD"], cwd=worktree_path, timeout_seconds=60)
    return {
        "ok": True,
        "commit_sha": str(rev.get("stdout") or "").strip(),
    }


def cleanup_worktree(
    *,
    repo_path: Path,
    worktree_path: str,
    branch: str,
    keep_branch: bool,
) -> Dict[str, Any]:
    results: List[str] = []
    path = Path(worktree_path)
    remove = _run_subprocess(
        ["git", "worktree", "remove", "--force", str(path)],
        cwd=repo_path,
        timeout_seconds=120,
    )
    if remove.get("ok"):
        results.append("worktree_removed")
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)
        results.append("worktree_deleted_locally")
    if branch and not keep_branch:
        delete = _run_subprocess(["git", "branch", "-D", branch], cwd=repo_path, timeout_seconds=60)
        if delete.get("ok"):
            results.append("branch_deleted")
    return {"ok": True, "actions": results}


def write_incident_report(
    *,
    repo_path: Path,
    incident: Incident,
    attempts: List[PatchAttempt],
    plan: RepairPlan | None,
    verification_report: Dict[str, Any],
    status: str,
) -> str:
    artifacts = write_repair_artifacts(
        repo_path=repo_path,
        incident=incident,
        attempts=attempts,
        plan=plan,
        verification_report=verification_report,
        status=status,
    )
    return str(artifacts.get("json_path") or "")


def build_cursor_handoff_markdown(
    *,
    incident: Incident,
    attempts: List[PatchAttempt],
    plan: RepairPlan | None,
    verification_report: Dict[str, Any],
    status: str,
) -> str:
    lines = [
        f"# Incident {incident.incident_id}",
        "",
        "## Summary",
        f"- Source: {incident.source}",
        f"- Service: {incident.service_name or 'andrea_sync'}",
        f"- Environment: {incident.environment or 'local'}",
        f"- State: {status}",
        f"- Error type: {incident.error_type}",
        f"- Fingerprint: {incident.fingerprint or 'n/a'}",
        f"- Summary: {incident.summary}",
        "",
        "## Root Cause Hypothesis",
        plan.root_cause if plan and plan.root_cause else (incident.probable_root_cause or "- pending"),
        "",
        "## Attempts",
    ]
    if attempts:
        for attempt in attempts:
            lines.append(
                (
                    f"- Attempt {attempt.attempt_number} `{attempt.stage}` via `{attempt.model_used or 'unknown'}`: "
                    f"{attempt.status}."
                )
            )
            if attempt.prompt_version:
                lines.append(f"  Prompt version: {attempt.prompt_version}")
            if attempt.files_touched:
                lines.append(f"  Files: {', '.join(attempt.files_touched[:6])}")
            if attempt.reasoning_summary:
                lines.append(f"  Reasoning: {_clip(attempt.reasoning_summary, 240)}")
            if attempt.error:
                lines.append(f"  Error: {_clip(attempt.error, 240)}")
    else:
        lines.append("- No lightweight repair attempts were recorded.")
    lines.extend(["", "## Verification", f"- Summary: {_clip(verification_report.get('summary') or '', 240) or 'n/a'}"])
    checks = verification_report.get("checks") if isinstance(verification_report.get("checks"), list) else []
    if checks:
        for check in checks[:8]:
            if not isinstance(check, dict):
                continue
            label = str(check.get("label") or check.get("check_id") or "check").strip()
            status_label = "passed" if bool(check.get("passed")) else "failed"
            excerpt = _clip(check.get("output_excerpt") or "", 240)
            line = f"- {label}: {status_label}"
            if excerpt:
                line += f" :: {excerpt}"
            lines.append(line)
    else:
        lines.append("- No verification checks recorded.")
    lines.extend(
        [
            "",
            "## Recommended Files",
            *(f"- {path}" for path in (plan.files_to_modify if plan else incident.suspected_files)[:12]),
        ]
    )
    if plan:
        lines.extend(
            [
                "",
                "## Repair Plan",
                f"- Planner model: {plan.model_used or 'unknown'}",
                f"- Prompt version: {plan.prompt_version or 'n/a'}",
                *(f"- {step}" for step in plan.steps[:12]),
                "",
                "## Risks",
                *(f"- {risk}" for risk in plan.risks[:10]),
                "",
                "## Stop Conditions",
                *(f"- {item}" for item in plan.stop_conditions[:10]),
                "",
                "## Success Criteria",
                *(f"- {item}" for item in (plan.verification_plan or ["Run the configured verification suite successfully."])[:10]),
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_repair_artifacts(
    *,
    repo_path: Path,
    incident: Incident,
    attempts: List[PatchAttempt],
    plan: RepairPlan | None,
    verification_report: Dict[str, Any],
    status: str,
) -> Dict[str, str]:
    root = Path(
        os.environ.get("ANDREA_REPAIR_REPORT_DIR", str(repo_path / "data" / "repair_reports"))
    )
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / f"{incident.incident_id}.json"
    markdown_path = root / f"{incident.incident_id}.md"
    payload = {
        "artifact_version": REPAIR_ARTIFACT_VERSION,
        "incident": incident.as_dict(),
        "attempts": [attempt.as_dict() for attempt in attempts],
        "plan": plan.as_dict() if plan else {},
        "verification_report": dict(verification_report or {}),
        "status": status,
        "generated_at": time.time(),
    }
    markdown_body = build_cursor_handoff_markdown(
        incident=incident,
        attempts=attempts,
        plan=plan,
        verification_report=verification_report,
        status=status,
    )
    payload["markdown_body"] = markdown_body
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown_path.write_text(
        markdown_body,
        encoding="utf-8",
    )
    return {
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
    }


def _try_repair_cursor_plan_first_handoff(
    *,
    repo_path: Path,
    incident: Incident,
    plan: RepairPlan,
    attempts: List[PatchAttempt],
    verification_checks: List[VerificationCheck],
    verification_report: Dict[str, Any] | None,
    cursor_mode: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    If plan-first is enabled and planner model is set, run read-only planner then executor.
    Returns (None, reason) to fall back to single-pass; reason is set only when plan-first was
    attempted or enabled-but-misconfigured (for conductor traceability).
    """
    if not plan_first_enabled("repair"):
        return None, None
    pm = planner_model_for_lane("repair")
    if not pm:
        return None, "no_planner_model"
    em = executor_model_for_lane("repair")
    poll_max = _cursor_handoff_poll_max_attempts()
    poll_iv = _cursor_handoff_poll_interval_seconds()
    default_timeout = 900 if poll_max > 0 else 180
    timeout_sec = int(os.environ.get("ANDREA_REPAIR_CURSOR_TIMEOUT_SECONDS", str(default_timeout)))

    branch_plan = f"repair/{incident.incident_id[:10]}-plan-{uuid.uuid4().hex[:6]}"
    branch_exec = f"repair/{incident.incident_id[:10]}-cursor-{uuid.uuid4().hex[:6]}"

    planner_prompt = build_cursor_planner_prompt(
        incident=incident,
        plan=plan,
        attempts=attempts,
        verification_checks=verification_checks,
    )
    r1 = run_cursor_handoff_cli(
        repo_path=repo_path,
        prompt=planner_prompt,
        branch=branch_plan,
        cursor_mode=cursor_mode,
        read_only=True,
        model=pm,
        poll_max_attempts=poll_max,
        poll_interval_seconds=poll_iv,
        timeout_seconds=timeout_sec,
    )
    p1 = r1.get("payload") if isinstance(r1.get("payload"), dict) else {}
    if not r1.get("ok") or not p1.get("ok"):
        return None, "planner_submission_failed"
    planner_agent = str(p1.get("agent_id") or "").strip()
    if not planner_agent:
        return None, "planner_missing_agent_id"

    conv = fetch_agent_conversation_payload(repo_path=repo_path, agent_id=planner_agent)
    resp = conv.get("response") if conv.get("ok") else None
    plan_text = extract_plan_text_from_conversation(resp) if resp is not None else ""
    if not plan_text_usable(plan_text):
        return None, "plan_unusable" if conv.get("ok") else "planner_conversation_unavailable"

    handoff_prompt = build_cursor_handoff_prompt(
        incident=incident,
        plan=plan,
        attempts=attempts,
        verification_checks=verification_checks,
    )
    artifact_markdown = build_cursor_handoff_markdown(
        incident=incident,
        attempts=attempts,
        plan=plan,
        verification_report=dict(verification_report or incident.verification or {}),
        status=incident.current_state,
    )
    prompt = (
        f"{handoff_prompt}\n\n"
        "## Cursor planner output\n\n"
        f"{plan_text}\n\n"
        "Reference incident artifact:\n\n"
        f"{artifact_markdown}"
    )

    r2 = run_cursor_handoff_cli(
        repo_path=repo_path,
        prompt=prompt,
        branch=branch_exec,
        cursor_mode=cursor_mode,
        read_only=False,
        model=em,
        poll_max_attempts=poll_max,
        poll_interval_seconds=poll_iv,
        timeout_seconds=timeout_sec,
    )
    p2 = r2.get("payload") if isinstance(r2.get("payload"), dict) else {}
    stdout2 = str(r2.get("stdout") or "").strip()
    if not r2.get("ok") or not p2.get("ok"):
        return {
            "ok": False,
            "branch": branch_exec,
            "prompt": prompt,
            "error": _clip(
                p2.get("error")
                or p2.get("response")
                or r2.get("stderr")
                or stdout2
                or "cursor executor handoff failed",
                1200,
            ),
            "cursor_strategy": "plan_first",
            "planner_model": pm,
            "executor_model": em,
            "planner_agent_id": planner_agent,
            "planner_branch": branch_plan,
            "planner_status": str(p1.get("status") or ""),
            "execution_agent_id": str(p2.get("agent_id") or ""),
            "agent_id": str(p2.get("agent_id") or ""),
            "plan_summary": _clip(plan_text, 2000),
            "artifact_markdown": artifact_markdown,
        }, None
    return {
        "ok": True,
        "branch": str(p2.get("branch") or branch_exec),
        "backend": str(p2.get("backend") or ""),
        "agent_id": str(p2.get("agent_id") or ""),
        "execution_agent_id": str(p2.get("agent_id") or ""),
        "agent_url": str(p2.get("agent_url") or ""),
        "pr_url": str(p2.get("pr_url") or ""),
        "status": str(p2.get("status") or ""),
        "prompt": prompt,
        "artifact_markdown": artifact_markdown,
        "cursor_strategy": "plan_first",
        "planner_model": pm,
        "executor_model": em,
        "planner_agent_id": planner_agent,
        "planner_branch": branch_plan,
        "planner_status": str(p1.get("status") or ""),
        "plan_summary": _clip(plan_text, 2000),
    }, None


def run_cursor_repair_handoff(
    *,
    repo_path: Path,
    incident: Incident,
    plan: RepairPlan,
    attempts: List[PatchAttempt],
    verification_checks: List[VerificationCheck],
    verification_report: Dict[str, Any] | None = None,
    cursor_mode: str,
) -> Dict[str, Any]:
    two_pass, plan_first_fallback_reason = _try_repair_cursor_plan_first_handoff(
        repo_path=repo_path,
        incident=incident,
        plan=plan,
        attempts=attempts,
        verification_checks=verification_checks,
        verification_report=verification_report,
        cursor_mode=cursor_mode,
    )
    if two_pass is not None:
        return two_pass

    branch = f"repair/{incident.incident_id[:10]}-cursor-{uuid.uuid4().hex[:6]}"
    handoff_prompt = build_cursor_handoff_prompt(
        incident=incident,
        plan=plan,
        attempts=attempts,
        verification_checks=verification_checks,
    )
    artifact_markdown = build_cursor_handoff_markdown(
        incident=incident,
        attempts=attempts,
        plan=plan,
        verification_report=dict(verification_report or incident.verification or {}),
        status=incident.current_state,
    )
    prompt = (
        f"{handoff_prompt}\n\n"
        "Reference incident artifact:\n\n"
        f"{artifact_markdown}"
    )
    poll_max = _cursor_handoff_poll_max_attempts()
    poll_iv = _cursor_handoff_poll_interval_seconds()
    exec_model = executor_model_for_lane("repair")
    cmd = [
        os.environ.get("PYTHON", "python3"),
        str(CURSOR_HANDOFF_SCRIPT),
        "--repo",
        str(repo_path),
        "--prompt",
        prompt,
        "--mode",
        cursor_mode,
        "--read-only",
        "false",
        "--model",
        exec_model,
        "--json",
        "--poll-max-attempts",
        str(poll_max),
        "--poll-interval-seconds",
        str(poll_iv),
        "--cli-timeout-seconds",
        "0",
        "--branch",
        branch,
    ]
    default_timeout = 900 if poll_max > 0 else 180
    result = _run_subprocess(
        cmd,
        cwd=repo_path,
        timeout_seconds=int(
            os.environ.get("ANDREA_REPAIR_CURSOR_TIMEOUT_SECONDS", str(default_timeout))
        ),
    )
    stdout = str(result.get("stdout") or "").strip()
    payload: Dict[str, Any] = {}
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {}
    if not result.get("ok") or not payload.get("ok"):
        out: Dict[str, Any] = {
            "ok": False,
            "branch": branch,
            "prompt": prompt,
            "error": _clip(
                payload.get("error")
                or payload.get("response")
                or result.get("stderr")
                or stdout
                or "cursor handoff failed",
                1200,
            ),
            "cursor_strategy": "single_pass_fallback" if plan_first_fallback_reason else "single_pass",
            "executor_model": exec_model,
        }
        if plan_first_fallback_reason:
            out["plan_first_fallback_reason"] = plan_first_fallback_reason
        return out
    out_ok: Dict[str, Any] = {
        "ok": True,
        "branch": str(payload.get("branch") or branch),
        "backend": str(payload.get("backend") or ""),
        "agent_id": str(payload.get("agent_id") or ""),
        "agent_url": str(payload.get("agent_url") or ""),
        "pr_url": str(payload.get("pr_url") or ""),
        "status": str(payload.get("status") or ""),
        "prompt": prompt,
        "artifact_markdown": artifact_markdown,
        "cursor_strategy": "single_pass_fallback" if plan_first_fallback_reason else "single_pass",
        "executor_model": exec_model,
    }
    if plan_first_fallback_reason:
        out_ok["plan_first_fallback_reason"] = plan_first_fallback_reason
    return out_ok

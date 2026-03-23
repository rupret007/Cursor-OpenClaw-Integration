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
from typing import Any, Dict, List

from .repair_prompts import build_cursor_handoff_prompt
from .repair_types import Incident, PatchAttempt, RepairPlan, VerificationCheck

REPO_ROOT = Path(__file__).resolve().parents[2]
CURSOR_HANDOFF_SCRIPT = REPO_ROOT / "skills" / "cursor_handoff" / "scripts" / "cursor_handoff.py"


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
            "output_excerpt": output_excerpt,
            "tags": list(check.tags),
        }
        if not row["passed"] and row["required"]:
            failing_required = True
        rows.append(row)
    failed_labels = [row["label"] for row in rows if not row["passed"]]
    return {
        "passed": not failing_required and not any(
            row["required"] and not row["passed"] for row in rows
        ),
        "checks": rows,
        "failed_checks": failed_labels,
        "summary": (
            "All enabled required verification checks passed."
            if not failed_labels
            else "Failed checks: " + ", ".join(failed_labels)
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
    root = Path(
        os.environ.get("ANDREA_REPAIR_REPORT_DIR", str(repo_path / "data" / "repair_reports"))
    )
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{incident.incident_id}.json"
    payload = {
        "incident": incident.as_dict(),
        "attempts": [attempt.as_dict() for attempt in attempts],
        "plan": plan.as_dict() if plan else {},
        "verification_report": dict(verification_report or {}),
        "status": status,
        "generated_at": time.time(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path)


def run_cursor_repair_handoff(
    *,
    repo_path: Path,
    incident: Incident,
    plan: RepairPlan,
    attempts: List[PatchAttempt],
    verification_checks: List[VerificationCheck],
    cursor_mode: str,
) -> Dict[str, Any]:
    branch = f"repair/{incident.incident_id[:10]}-cursor-{uuid.uuid4().hex[:6]}"
    prompt = build_cursor_handoff_prompt(
        incident=incident,
        plan=plan,
        attempts=attempts,
        verification_checks=verification_checks,
    )
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
        "--json",
        "--poll-max-attempts",
        "0",
        "--cli-timeout-seconds",
        "0",
        "--branch",
        branch,
    ]
    result = _run_subprocess(
        cmd,
        cwd=repo_path,
        timeout_seconds=int(os.environ.get("ANDREA_REPAIR_CURSOR_TIMEOUT_SECONDS", "180")),
    )
    stdout = str(result.get("stdout") or "").strip()
    payload: Dict[str, Any] = {}
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {}
    if not result.get("ok") or not payload.get("ok"):
        return {
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
        }
    return {
        "ok": True,
        "branch": str(payload.get("branch") or branch),
        "backend": str(payload.get("backend") or ""),
        "agent_id": str(payload.get("agent_id") or ""),
        "agent_url": str(payload.get("agent_url") or ""),
        "pr_url": str(payload.get("pr_url") or ""),
        "status": str(payload.get("status") or ""),
        "prompt": prompt,
    }

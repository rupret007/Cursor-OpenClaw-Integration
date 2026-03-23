"""Autonomous optimization loop for Andrea orchestration quality."""
from __future__ import annotations

import json
import os
import subprocess
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from .kill_switch import kill_switch_status
from .observability import metric_log, structured_log
from .policy import evaluate_skill_absence_claim
from .projector import project_task_dict
from .schema import EventType
from .store import (
    SYSTEM_TASK_ID,
    append_event,
    ensure_system_task,
    load_events_for_task,
    list_tasks,
    task_exists,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CURSOR_HANDOFF_SCRIPT = REPO_ROOT / "skills" / "cursor_handoff" / "scripts" / "cursor_handoff.py"
SAFE_AUTO_HEAL_ROOTS = (
    "services/andrea_sync/",
    "tests/",
    "scripts/",
    "skills/",
)


CATEGORY_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "overdelegation": {
        "title": "Tighten direct-vs-delegate routing",
        "severity": "high",
        "target_files": [
            "services/andrea_sync/andrea_router.py",
            "services/andrea_sync/telegram_continuation.py",
            "tests/test_andrea_sync.py",
            "tests/test_andrea_sync_http.py",
        ],
        "problem": "Recent tasks show lightweight/meta turns still being treated like delegated work.",
        "action": "Refine router and continuation heuristics so direct Andrea answers win more reliably.",
    },
    "visibility_noise": {
        "title": "Reduce low-value collaboration visibility",
        "severity": "medium",
        "target_files": [
            "services/andrea_sync/server.py",
            "services/andrea_sync/telegram_format.py",
            "tests/test_andrea_sync.py",
        ],
        "problem": "Recent tasks requested full visibility or showed visible collaboration without enough collaborative value.",
        "action": "Tighten visibility escalation and progress-copy rules so full dialogue remains meaningful.",
    },
    "continuation_risk": {
        "title": "Harden Telegram continuation boundaries",
        "severity": "high",
        "target_files": [
            "services/andrea_sync/telegram_continuation.py",
            "services/andrea_sync/server.py",
            "tests/test_andrea_sync.py",
            "tests/test_andrea_sync_http.py",
        ],
        "problem": "Recent tasks indicate heavy continuation attachment or thread-merging risk.",
        "action": "Favor explicit reply threading and reduce timing-only continuation behavior.",
    },
    "execution_failure": {
        "title": "Reduce delegated execution failures",
        "severity": "high",
        "target_files": [
            "services/andrea_sync/server.py",
            "scripts/andrea_sync_openclaw_hybrid.py",
            "tests/test_andrea_sync.py",
        ],
        "problem": "Recent delegated runs failed before producing a trustworthy answer.",
        "action": "Improve execution fallback, error handling, and lane selection before escalating to the user.",
    },
    "blocked_capability": {
        "title": "Harden blocked collaboration fallback UX",
        "severity": "high",
        "target_files": [
            "services/andrea_sync/server.py",
            "scripts/andrea_sync_openclaw_hybrid.py",
            "skills/cursor_handoff/SKILL.md",
            "tests/test_andrea_sync.py",
        ],
        "problem": "Recent collaboration attempts hit internal capability limits and needed a calmer fallback contract.",
        "action": "Keep exact runtime diagnostics internal while giving the user a product-level limitation or fallback explanation.",
    },
    "runtime_leakage": {
        "title": "Stop runtime jargon from reaching users",
        "severity": "high",
        "target_files": [
            "services/andrea_sync/server.py",
            "services/andrea_sync/andrea_router.py",
            "services/andrea_sync/telegram_format.py",
            "scripts/andrea_sync_openclaw_hybrid.py",
            "tests/test_andrea_sync.py",
            "tests/test_andrea_sync_http.py",
        ],
        "problem": "Recent user-visible replies leaked internal runtime, session, or tool mechanics.",
        "action": "Route exact diagnostics into internal traces and keep user-facing summaries product-level.",
    },
    "planner_failure": {
        "title": "Stabilize planning phase failures",
        "severity": "high",
        "target_files": [
            "services/andrea_sync/server.py",
            "scripts/andrea_sync_openclaw_hybrid.py",
            "services/andrea_sync/schema.py",
            "tests/test_andrea_sync.py",
        ],
        "problem": "Recent orchestration runs failed before the planning phase produced a stable handoff plan.",
        "action": "Harden planning outputs, phase tracking, and fallbacks before escalating or replying.",
    },
    "critic_failure": {
        "title": "Strengthen critique and verification",
        "severity": "medium",
        "target_files": [
            "services/andrea_sync/server.py",
            "scripts/andrea_sync_openclaw_hybrid.py",
            "services/andrea_sync/schema.py",
            "tests/test_andrea_sync_openclaw_hybrid.py",
        ],
        "problem": "Recent collaborative runs missed or failed the critique pass that should catch weak plans before execution.",
        "action": "Make critique outputs explicit, auditable, and required when collaboration is requested.",
    },
    "executor_failure": {
        "title": "Reduce execution lane regressions",
        "severity": "high",
        "target_files": [
            "services/andrea_sync/server.py",
            "skills/cursor_handoff/SKILL.md",
            "tests/test_andrea_sync.py",
            "tests/test_andrea_sync_http.py",
        ],
        "problem": "Recent orchestration runs failed during the execution phase after planning succeeded.",
        "action": "Tighten Cursor handoff, execution retries, and synthesis after execution finishes.",
    },
    "proactive_delivery": {
        "title": "Harden proactive reminder delivery",
        "severity": "medium",
        "target_files": [
            "services/andrea_sync/bus.py",
            "services/andrea_sync/server.py",
            "services/andrea_sync/store.py",
            "tests/test_andrea_sync.py",
        ],
        "problem": "Recent proactive follow-through attempts were created but not delivered cleanly.",
        "action": "Keep reminders quiet but reliable by improving delivery-target resolution and reminder sweep safeguards.",
    },
    "user_dissatisfaction": {
        "title": "Investigate recent negative user outcomes",
        "severity": "medium",
        "target_files": [
            "services/andrea_sync/andrea_router.py",
            "services/andrea_sync/server.py",
            "services/andrea_sync/telegram_format.py",
        ],
        "problem": "Recent task feedback suggests the experience felt wrong even when the task completed.",
        "action": "Review routing, visibility, and wording decisions for the negative-feedback examples.",
    },
}


def _clip(value: Any, limit: int = 400) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def collect_recent_task_outcomes(
    conn: sqlite3.Connection, *, limit: int = 60
) -> List[Dict[str, Any]]:
    outcomes: List[Dict[str, Any]] = []
    for row in list_tasks(conn, limit=max(1, int(limit))):
        task_id = str(row.get("task_id") or "")
        channel = str(row.get("channel") or "")
        if not task_id or not channel or task_id == SYSTEM_TASK_ID or channel == "internal":
            continue
        proj = project_task_dict(conn, task_id, channel)
        meta = proj.get("meta") if isinstance(proj.get("meta"), dict) else {}
        outcome = meta.get("outcome") if isinstance(meta.get("outcome"), dict) else {}
        outcomes.append(
            {
                "task_id": task_id,
                "channel": channel,
                "status": str(proj.get("status") or ""),
                "summary": str(proj.get("summary") or ""),
                "updated_at": row.get("updated_at"),
                "outcome": dict(outcome),
                "meta": meta,
            }
        )
    return outcomes


def detect_failure_categories(outcomes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for item in outcomes:
        outcome = item.get("outcome") if isinstance(item.get("outcome"), dict) else {}
        flags = outcome.get("ux_flags") if isinstance(outcome.get("ux_flags"), list) else []
        task_id = str(item.get("task_id") or "")

        categories_for_task: List[str] = []
        if "overdelegated_meta_question" in flags:
            categories_for_task.append("overdelegation")
        if "low_value_full_visibility" in flags:
            categories_for_task.append("visibility_noise")
        if "continuation_heavy" in flags:
            categories_for_task.append("continuation_risk")
        if "execution_failed" in flags or str(outcome.get("terminal_status") or "") == "failed":
            categories_for_task.append("execution_failure")
        if "blocked_capability" in flags or "internal_runtime_trace" in flags:
            categories_for_task.append("blocked_capability")
        if "runtime_jargon_leaked" in flags:
            categories_for_task.append("runtime_leakage")
        if "planner_failure" in flags:
            categories_for_task.append("planner_failure")
        if "critic_failure" in flags or "critic_missing" in flags:
            categories_for_task.append("critic_failure")
        if "executor_failure" in flags:
            categories_for_task.append("executor_failure")
        if "proactive_delivery_failed" in flags:
            categories_for_task.append("proactive_delivery")
        if "negative_feedback" in flags:
            categories_for_task.append("user_dissatisfaction")

        for category in categories_for_task:
            bucket = buckets.setdefault(
                category,
                {
                    "category": category,
                    "severity": CATEGORY_TEMPLATES.get(category, {}).get("severity", "medium"),
                    "count": 0,
                    "task_ids": [],
                    "examples": [],
                },
            )
            bucket["count"] += 1
            if task_id and task_id not in bucket["task_ids"]:
                bucket["task_ids"].append(task_id)
            if len(bucket["examples"]) < 4:
                bucket["examples"].append(
                    {
                        "task_id": task_id,
                        "summary": _clip(item.get("summary"), 160),
                        "result_kind": str(outcome.get("result_kind") or ""),
                    }
                )
    return sorted(buckets.values(), key=lambda row: (-int(row["count"]), row["category"]))


def build_openclaw_optimization_prompt(
    findings: List[Dict[str, Any]], proposals: List[Dict[str, Any]]
) -> str:
    finding_lines = []
    for row in findings[:8]:
        finding_lines.append(
            f"- {row['category']}: count={row['count']} severity={row['severity']} tasks={', '.join(row['task_ids'][:5])}"
        )
    proposal_lines = []
    for row in proposals[:8]:
        proposal_lines.append(
            f"- {row['proposal_id']}: {row['title']} -> lane={row['preferred_execution_lane']} status={row['status']}"
        )
    findings_block = "\n".join(finding_lines) or "- no major recurring failures found"
    proposals_block = "\n".join(proposal_lines) or "- no proposals generated"
    return (
        "You are Andrea's optimization analyst.\n"
        "Review these recurring orchestration issues and the draft optimization proposals.\n"
        "Return a concise operator-facing analysis that prioritizes trust, calm UX, and correct lane selection.\n\n"
        "Findings:\n"
        f"{findings_block}\n\n"
        "Draft proposals:\n"
        f"{proposals_block}\n"
    )


def build_structured_proposals(
    findings: List[Dict[str, Any]], *, gate: Dict[str, Any]
) -> List[Dict[str, Any]]:
    proposals: List[Dict[str, Any]] = []
    for row in findings:
        template = CATEGORY_TEMPLATES.get(row["category"])
        if not template:
            continue
        proposal_id = f"prop_{uuid.uuid4().hex[:10]}"
        gate_allowed = bool(gate.get("allowed"))
        proposals.append(
            {
                "proposal_id": proposal_id,
                "title": template["title"],
                "category": row["category"],
                "severity": row["severity"],
                "status": "branch_prep_ready" if gate_allowed else "gated",
                "problem_statement": (
                    f"{template['problem']} Observed in {row['count']} recent task(s)."
                ),
                "recommended_action": template["action"],
                "target_files": list(template["target_files"]),
                "preferred_execution_lane": "cursor_branch_prep",
                "analysis_lane": "openclaw",
                "evidence_task_ids": list(row.get("task_ids") or [])[:8],
                "branch_prep_allowed": gate_allowed,
                "gate_reasons": list(gate.get("reasons") or []),
            }
        )
    return proposals


def evaluate_autonomy_gate(
    conn: sqlite3.Connection,
    *,
    regression_report: Dict[str, Any] | None,
    required_skills: List[str] | None,
) -> Dict[str, Any]:
    ks = kill_switch_status(conn)
    report = regression_report if isinstance(regression_report, dict) else {}
    total = int(report.get("total") or 0)
    passed = bool(report.get("passed")) and total > 0
    skill_checks: List[Dict[str, Any]] = []
    reasons: List[str] = []
    allowed = True

    if ks.get("engaged"):
        allowed = False
        reasons.append("kill_switch_engaged")
    if not passed:
        allowed = False
        reasons.append("regression_report_missing_or_failed")

    for skill in required_skills or []:
        verdict = evaluate_skill_absence_claim(conn, str(skill), max_age_seconds=900.0)
        skill_checks.append({"skill": str(skill), **verdict})
        if verdict.get("must_refresh"):
            allowed = False
            reasons.append(f"capability_digest_refresh_required:{skill}")

    return {
        "allowed": allowed,
        "kill_switch_engaged": bool(ks.get("engaged")),
        "regression_report": {"passed": passed, "total": total},
        "skill_checks": skill_checks,
        "reasons": reasons,
    }


def record_regression_report(
    conn: sqlite3.Connection,
    report: Dict[str, Any] | None,
    *,
    actor: str = "internal",
) -> Dict[str, Any]:
    ensure_system_task(conn)
    payload = {
        "passed": bool((report or {}).get("passed")),
        "total": int((report or {}).get("total") or 0),
        "command": str((report or {}).get("command") or ""),
        "actor": actor,
    }
    append_event(conn, SYSTEM_TASK_ID, EventType.REGRESSION_RECORDED, payload)
    return payload


def get_optimization_proposal(
    conn: sqlite3.Connection, proposal_id: str
) -> Dict[str, Any]:
    if not task_exists(conn, SYSTEM_TASK_ID):
        return {}
    target = str(proposal_id or "").strip()
    if not target:
        return {}
    for _seq, _ts, et, payload in reversed(load_events_for_task(conn, SYSTEM_TASK_ID)):
        if et != EventType.OPTIMIZATION_PROPOSAL.value:
            continue
        if str(payload.get("proposal_id") or "").strip() == target:
            return dict(payload)
    return {}


def _normalize_target_files(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for raw in value:
        text = str(raw or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _proposal_apply_gate(proposal: Dict[str, Any]) -> Dict[str, Any]:
    reasons: List[str] = []
    allowed = True
    if not bool(proposal.get("branch_prep_allowed")):
        allowed = False
        reasons.append("branch_prep_not_allowed")
    status = str(proposal.get("status") or "").strip()
    if status not in {"branch_prep_ready", "approved", "proposed"}:
        allowed = False
        reasons.append(f"unsupported_status:{status or 'unknown'}")
    target_files = _normalize_target_files(proposal.get("target_files"))
    if len(target_files) > 12:
        allowed = False
        reasons.append("too_many_target_files")
    for path in target_files:
        if not any(path.startswith(prefix) for prefix in SAFE_AUTO_HEAL_ROOTS):
            allowed = False
            reasons.append(f"disallowed_target:{path}")
    if any(secret in " ".join(target_files).lower() for secret in (".env", "credentials", "secret", "token")):
        allowed = False
        reasons.append("sensitive_target_path")
    return {"allowed": allowed, "reasons": reasons, "target_files": target_files}


def _build_auto_heal_prompt(proposal: Dict[str, Any]) -> str:
    target_files = _normalize_target_files(proposal.get("target_files"))
    files_block = "\n".join(f"- {path}" for path in target_files[:12]) or "- no explicit file list"
    evidence_block = "\n".join(
        f"- {task_id}" for task_id in (proposal.get("evidence_task_ids") or [])[:8]
    ) or "- no task ids recorded"
    return (
        "You are Andrea's closed-loop local self-healing runner.\n"
        "Apply a safe, minimal local fix for this optimization proposal.\n"
        "Stay within the target files when possible. Do not touch secrets, env files, or unrelated areas.\n"
        "Prefer surgical fixes plus tests. Summarize what changed, what you verified, and any residual risk.\n\n"
        f"Proposal: {proposal.get('title')}\n"
        f"Category: {proposal.get('category')}\n"
        f"Problem: {proposal.get('problem_statement')}\n"
        f"Recommended action: {proposal.get('recommended_action')}\n"
        "Target files:\n"
        f"{files_block}\n\n"
        "Evidence task ids:\n"
        f"{evidence_block}\n"
    )


def _run_cursor_handoff_prompt(
    *,
    repo_path: Path,
    prompt: str,
    branch: str,
    cursor_mode: str,
) -> Dict[str, Any]:
    cmd = [
        sys.executable,
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
    proc = subprocess.run(
        cmd,
        cwd=str(repo_path),
        text=True,
        capture_output=True,
        check=False,
    )
    stdout = proc.stdout.strip()
    payload: Dict[str, Any]
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"cursor_handoff returned invalid JSON: {stdout[:400]}") from exc
    else:
        payload = {}
    if proc.returncode != 0 or not payload.get("ok"):
        detail = str(payload.get("error") or payload.get("response") or proc.stderr or stdout[:400])
        raise RuntimeError(detail)
    return payload


def apply_optimization_proposal(
    conn: sqlite3.Connection,
    *,
    proposal_payload: Dict[str, Any],
    repo_path: Path,
    actor: str = "internal",
) -> Dict[str, Any]:
    ensure_system_task(conn)
    proposal = dict(proposal_payload)
    if proposal.get("proposal_id") and not proposal.get("title"):
        proposal = {**get_optimization_proposal(conn, str(proposal.get("proposal_id"))), **proposal}
    proposal_id = str(proposal.get("proposal_id") or "").strip()
    if not proposal_id:
        return {"ok": False, "error": "proposal_id required"}
    gate = _proposal_apply_gate(proposal)
    branch = f"openclaw/autoheal-{proposal_id[:10]}"
    append_event(
        conn,
        SYSTEM_TASK_ID,
        EventType.LOCAL_AUTO_HEAL_STARTED,
        {
            "proposal_id": proposal_id,
            "title": str(proposal.get("title") or ""),
            "category": str(proposal.get("category") or ""),
            "actor": actor,
            "branch": branch,
        },
    )
    if not gate["allowed"]:
        error = ", ".join(gate["reasons"]) or "gate_blocked"
        append_event(
            conn,
            SYSTEM_TASK_ID,
            EventType.LOCAL_AUTO_HEAL_FAILED,
            {
                "proposal_id": proposal_id,
                "title": str(proposal.get("title") or ""),
                "category": str(proposal.get("category") or ""),
                "actor": actor,
                "branch": branch,
                "error": error,
            },
        )
        return {
            "ok": False,
            "proposal_id": proposal_id,
            "error": error,
            "gate": gate,
        }
    cursor_mode = (
        str(proposal.get("cursor_mode") or "").strip()
        or os.environ.get("ANDREA_SELF_HEAL_CURSOR_MODE", "").strip()
        or "auto"
    )
    try:
        payload = _run_cursor_handoff_prompt(
            repo_path=repo_path,
            prompt=_build_auto_heal_prompt(proposal),
            branch=branch,
            cursor_mode=cursor_mode,
        )
    except Exception as exc:  # noqa: BLE001
        append_event(
            conn,
            SYSTEM_TASK_ID,
            EventType.LOCAL_AUTO_HEAL_FAILED,
            {
                "proposal_id": proposal_id,
                "title": str(proposal.get("title") or ""),
                "category": str(proposal.get("category") or ""),
                "actor": actor,
                "branch": branch,
                "error": str(exc),
            },
        )
        return {"ok": False, "proposal_id": proposal_id, "error": str(exc), "gate": gate}
    result = {
        "ok": True,
        "proposal_id": proposal_id,
        "title": str(proposal.get("title") or ""),
        "category": str(proposal.get("category") or ""),
        "branch": str(payload.get("branch") or branch),
        "backend": str(payload.get("backend") or ""),
        "agent_id": str(payload.get("agent_id") or ""),
        "agent_url": str(payload.get("agent_url") or ""),
        "pr_url": str(payload.get("pr_url") or ""),
        "status": str(payload.get("status") or ""),
        "gate": gate,
    }
    append_event(
        conn,
        SYSTEM_TASK_ID,
        EventType.LOCAL_AUTO_HEAL_COMPLETED,
        {
            "proposal_id": proposal_id,
            "title": result["title"],
            "category": result["category"],
            "actor": actor,
            "branch": result["branch"],
            "backend": result["backend"],
            "agent_id": result["agent_id"],
            "agent_url": result["agent_url"],
            "pr_url": result["pr_url"],
            "status": result["status"],
        },
    )
    return result


def apply_ready_proposals(
    conn: sqlite3.Connection,
    *,
    proposals: List[Dict[str, Any]] | None,
    repo_path: Path,
    actor: str = "internal",
    max_apply: int = 1,
) -> Dict[str, Any]:
    selected = [
        dict(row)
        for row in (proposals or [])
        if bool(row.get("branch_prep_allowed"))
        and str(row.get("status") or "").strip() == "branch_prep_ready"
    ][: max(1, int(max_apply))]
    results = [
        apply_optimization_proposal(
            conn,
            proposal_payload=row,
            repo_path=repo_path,
            actor=actor,
        )
        for row in selected
    ]
    return {
        "applied": [row for row in results if row.get("ok")],
        "failed": [row for row in results if not row.get("ok")],
    }


def run_optimization_cycle(
    conn: sqlite3.Connection,
    *,
    limit: int = 60,
    regression_report: Dict[str, Any] | None = None,
    required_skills: List[str] | None = None,
    emit_proposals: bool = True,
    actor: str = "internal",
    analysis_mode: str = "heuristic",
) -> Dict[str, Any]:
    ensure_system_task(conn)
    run_id = f"opt_{uuid.uuid4().hex[:12]}"
    append_event(
        conn,
        SYSTEM_TASK_ID,
        EventType.OPTIMIZATION_RUN_STARTED,
        {
            "run_id": run_id,
            "actor": actor,
            "limit": int(limit),
            "analysis_mode": analysis_mode,
            "started_ts": time.time(),
        },
    )
    structured_log(
        "optimization_cycle_started",
        run_id=run_id,
        actor=actor,
        limit=limit,
        analysis_mode=analysis_mode,
    )
    try:
        if isinstance(regression_report, dict) and regression_report:
            record_regression_report(conn, regression_report, actor=actor)
        outcomes = collect_recent_task_outcomes(conn, limit=limit)
        findings = detect_failure_categories(outcomes)
        gate = evaluate_autonomy_gate(
            conn,
            regression_report=regression_report,
            required_skills=required_skills,
        )
        proposals = build_structured_proposals(findings, gate=gate) if emit_proposals else []
        openclaw_prompt = build_openclaw_optimization_prompt(findings, proposals)

        for finding in findings:
            append_event(
                conn,
                SYSTEM_TASK_ID,
                EventType.EVALUATION_RECORDED,
                {
                    "run_id": run_id,
                    "category": finding["category"],
                    "severity": finding["severity"],
                    "summary": (
                        f"{finding['category']} appeared in {finding['count']} recent task(s)."
                    ),
                    "count": finding["count"],
                    "evidence_task_ids": finding.get("task_ids", []),
                    "recommended_action": CATEGORY_TEMPLATES.get(
                        finding["category"], {}
                    ).get("action", ""),
                },
            )
            metric_log(
                "optimization_finding_recorded",
                run_id=run_id,
                category=finding["category"],
                severity=finding["severity"],
                count=finding["count"],
            )

        for proposal in proposals:
            append_event(conn, SYSTEM_TASK_ID, EventType.OPTIMIZATION_PROPOSAL, proposal)
            metric_log(
                "optimization_proposal_created",
                run_id=run_id,
                category=proposal["category"],
                status=proposal["status"],
                branch_prep_allowed=proposal["branch_prep_allowed"],
            )

        append_event(
            conn,
            SYSTEM_TASK_ID,
            EventType.OPTIMIZATION_RUN_COMPLETED,
            {
                "run_id": run_id,
                "actor": actor,
                "analysis_mode": analysis_mode,
                "outcome_count": len(outcomes),
                "finding_count": len(findings),
                "proposal_count": len(proposals),
                "gate_allowed": bool(gate.get("allowed")),
                "gate_reasons": list(gate.get("reasons") or []),
            },
        )
        metric_log(
            "optimization_cycle_completed",
            run_id=run_id,
            outcomes=len(outcomes),
            findings=len(findings),
            proposals=len(proposals),
            gate_allowed=bool(gate.get("allowed")),
        )
        structured_log(
            "optimization_cycle_completed",
            run_id=run_id,
            outcomes=len(outcomes),
            findings=len(findings),
            proposals=len(proposals),
            gate_allowed=bool(gate.get("allowed")),
        )
        result = {
            "ok": True,
            "task_id": SYSTEM_TASK_ID,
            "run_id": run_id,
            "outcomes_analyzed": len(outcomes),
            "findings": findings,
            "proposals": proposals,
            "gate": gate,
        }
        if analysis_mode != "heuristic":
            result["openclaw_analysis_prompt"] = openclaw_prompt
        return result
    except Exception as exc:  # noqa: BLE001
        append_event(
            conn,
            SYSTEM_TASK_ID,
            EventType.OPTIMIZATION_RUN_FAILED,
            {
                "run_id": run_id,
                "actor": actor,
                "analysis_mode": analysis_mode,
                "error": str(exc),
            },
        )
        metric_log("optimization_cycle_failed", run_id=run_id)
        structured_log("optimization_cycle_failed", run_id=run_id, error=str(exc))
        return {
            "ok": False,
            "task_id": SYSTEM_TASK_ID,
            "run_id": run_id,
            "error": str(exc),
        }

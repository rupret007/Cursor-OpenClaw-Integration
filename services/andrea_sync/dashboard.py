"""Operator dashboard helpers for Andrea lockstep."""
from __future__ import annotations

import os
import time
import urllib.parse
from typing import Any, Dict, List

from .adapters import telegram as tg_adapt
from .collaboration_effectiveness import trusted_operator_summary
from .kill_switch import kill_switch_status
from .policy import digest_age_seconds, get_capability_digest
from .projector import project_task_dict
from .delegated_lifecycle import build_delegated_lifecycle_contract
from .optimizer import build_background_regression_report, evaluate_autonomy_gate
from .repair_policy import configured_safe_repair_roots, repair_enabled
from .policy_governance import governance_snapshot
from .resource_vocabulary import infer_resource_lane, verification_story_from_outcome
from .assistant_domain_rollout import (
    build_daily_pack_operator_snapshot,
    daily_pack_optimizer_hints,
)
from .scenario_registry import FIRST_SUPPORTED_SCENARIO_IDS, get_contract
from .schema import EventType
from .store import (
    SYSTEM_TASK_ID,
    count_active_execution_attempts,
    count_active_memories,
    count_due_reminders,
    get_latest_experience_run,
    count_pending_reminders,
    count_principals,
    list_experience_runs,
    list_incidents,
    list_recent_execution_plans,
    list_tasks,
    load_events_for_task,
    task_exists,
)


def _redact_webhook_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlparse(raw)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if "secret" in query:
        query["secret"] = ["***"]
    redacted_query = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            redacted_query,
            parsed.fragment,
        )
    )


def build_dashboard_webhook_snapshot(server: Any) -> Dict[str, Any]:
    expected_url = server._expected_webhook_url() if server.telegram_public_base else ""
    base = {
        "public_base": server.telegram_public_base,
        "autofix_enabled": bool(server.telegram_webhook_autofix),
        "expected_url": _redact_webhook_url(expected_url),
        "header_secret_configured": bool(server.telegram_header_secret),
        "query_secret_configured": bool(server.telegram_secret),
        "use_query_secret": bool(server.telegram_use_query_secret),
        "required": bool(server.telegram_bot_token or server.telegram_public_base),
    }
    if not server.telegram_bot_token:
        return {
            **base,
            "configured": False,
            "status": "unconfigured",
            "reason": "TELEGRAM_BOT_TOKEN missing",
            "healthy": False,
        }
    if not server.telegram_public_base:
        return {
            **base,
            "configured": False,
            "status": "missing_public_base",
            "reason": "ANDREA_SYNC_PUBLIC_BASE missing",
            "healthy": False,
        }
    try:
        info = tg_adapt.get_webhook_info(server.telegram_bot_token)
    except Exception as exc:  # noqa: BLE001
        return {
            **base,
            "configured": True,
            "status": "error",
            "reason": str(exc),
            "healthy": False,
        }
    result = info.get("result") if isinstance(info.get("result"), dict) else {}
    current_url = str(result.get("url") or "").strip()
    if not current_url:
        status = "unset"
        reason = "Telegram has no webhook registered"
    elif tg_adapt.webhook_urls_match(current_url, expected_url):
        status = "healthy"
        reason = "Telegram webhook matches Andrea expected URL"
    else:
        status = "drifted"
        reason = "Telegram webhook differs from Andrea expected URL"
    return {
        **base,
        "configured": True,
        "status": status,
        "reason": reason,
        "healthy": status == "healthy",
        "current_url": _redact_webhook_url(current_url),
        "pending_update_count": int(result.get("pending_update_count") or 0),
        "last_error_date": result.get("last_error_date"),
        "last_error_message": result.get("last_error_message"),
        "max_connections": result.get("max_connections"),
        "ip_address": result.get("ip_address"),
    }


def _capability_digest_status(conn: Any) -> tuple[float | None, bool, str]:
    capability_digest = get_capability_digest(conn)
    present = capability_digest.get("present") is True
    age = digest_age_seconds(conn)
    if not present:
        return age, False, "missing"
    if age is None:
        return age, True, "unknown"
    if age > 900.0:
        return age, True, "stale"
    return age, True, "fresh"


def build_runtime_truth_snapshot(
    conn: Any,
    server: Any,
    *,
    webhook_snapshot: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    age, present, digest_status = _capability_digest_status(conn)
    webhook = dict(webhook_snapshot or {})
    return {
        "source": "process",
        "pid": os.getpid(),
        "telegram": {
            "public_base": str(server.telegram_public_base or ""),
            "bot_token_configured": bool(server.telegram_bot_token),
            "header_secret_configured": bool(server.telegram_header_secret),
            "query_secret_configured": bool(server.telegram_secret),
            "use_query_secret": bool(server.telegram_use_query_secret),
            "notifier_enabled": bool(getattr(server, "telegram_notifier_enabled", False)),
            "quiet_lifecycle": bool(getattr(server, "telegram_quiet_lifecycle", False)),
            "auto_cursor": bool(getattr(server, "telegram_auto_cursor", False)),
            "delegate_lane": str(getattr(server, "telegram_delegate_lane", "") or ""),
        },
        "background_enabled": bool(getattr(server, "background_enabled", False)),
        "delegated_execution_enabled": bool(
            getattr(server, "delegated_execution_enabled", False)
        ),
        "background_optimizer_enabled": bool(
            getattr(server, "background_optimizer_enabled", False)
        ),
        "background_incident_repair_enabled": bool(
            getattr(server, "background_incident_repair_enabled", False)
        ),
        "openclaw_agent_id": str(getattr(server, "openclaw_agent_id", "") or ""),
        "capability_digest_present": present,
        "capability_digest_age_seconds": age,
        "capability_digest_status": digest_status,
        "webhook": webhook,
        "execution_continuity": {
            "active_execution_attempts": count_active_execution_attempts(conn),
        },
    }


def _project_incident_for_dashboard(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten incident + repair conductor fields for operators and dashboard HTML."""
    if not raw:
        return {}
    meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    conductor = meta.get("conductor") if isinstance(meta.get("conductor"), dict) else {}
    handoff = conductor.get("handoff") if isinstance(conductor.get("handoff"), dict) else {}
    reasons = conductor.get("escalation_reasons") or []
    if not isinstance(reasons, list):
        reasons = []
    reason_strs = [str(r).strip() for r in reasons if str(r).strip()][:8]
    preferred = str(conductor.get("preferred_executor") or "").strip()
    outcome = conductor.get("outcome") if isinstance(conductor.get("outcome"), dict) else {}
    sub_st = str(outcome.get("submission_status") or "").strip()
    ver_st = str(outcome.get("verification_status") or "").strip()
    next_act = str(outcome.get("next_action") or "").strip()
    summary_bits = [preferred] if preferred else []
    if reason_strs:
        summary_bits.append("reasons: " + ", ".join(reason_strs))
    if sub_st:
        summary_bits.append(f"submission: {sub_st}")
    if ver_st:
        summary_bits.append(f"verify: {ver_st}")
    if next_act:
        summary_bits.append(f"next: {next_act}")
    if handoff.get("ok"):
        summary_bits.append("Cursor handoff submitted")
    elif handoff and conductor.get("effective_cursor_execute"):
        summary_bits.append("Cursor handoff attempted")
    conductor_summary = " · ".join(summary_bits) if summary_bits else ""

    return {
        **raw,
        "conductor_preferred_executor": preferred,
        "conductor_reasons": reason_strs,
        "conductor_summary": conductor_summary,
        "conductor_recommended_cursor_execute": bool(conductor.get("recommended_cursor_execute")),
        "conductor_cursor_execute_requested": bool(conductor.get("cursor_execute_requested")),
        "conductor_auto_cursor_heavy": bool(conductor.get("auto_cursor_heavy")),
        "conductor_effective_cursor_execute": bool(conductor.get("effective_cursor_execute")),
        "conductor_worktree_clean": bool(conductor.get("worktree_clean")),
        "cursor_handoff_active": bool(handoff.get("ok")),
        "cursor_handoff_branch": str(handoff.get("branch") or ""),
        "cursor_handoff_agent_url": str(handoff.get("agent_url") or ""),
        "cursor_handoff_pr_url": str(handoff.get("pr_url") or ""),
        "cursor_handoff_error": str(handoff.get("error") or ""),
        "conductor_metrics": conductor.get("metrics")
        if isinstance(conductor.get("metrics"), dict)
        else {},
        "conductor_outcome_submission_status": sub_st,
        "conductor_outcome_verification_status": ver_st,
        "conductor_outcome_next_action": next_act,
        "conductor_outcome_terminal_cursor_status": str(outcome.get("terminal_cursor_status") or ""),
        "conductor_outcome_skip_reason": str(outcome.get("verification_skip_reason") or ""),
    }


def _task_list_item(row: Dict[str, Any], proj: Dict[str, Any]) -> Dict[str, Any]:
    meta = proj.get("meta") if isinstance(proj.get("meta"), dict) else {}
    telegram = meta.get("telegram") if isinstance(meta.get("telegram"), dict) else {}
    execution = meta.get("execution") if isinstance(meta.get("execution"), dict) else {}
    openclaw = meta.get("openclaw") if isinstance(meta.get("openclaw"), dict) else {}
    cursor = meta.get("cursor") if isinstance(meta.get("cursor"), dict) else {}
    outcome = meta.get("outcome") if isinstance(meta.get("outcome"), dict) else {}
    identity = meta.get("identity") if isinstance(meta.get("identity"), dict) else {}
    resource_lane = infer_resource_lane(execution)
    return {
        "task_id": proj.get("task_id") or row.get("task_id"),
        "channel": proj.get("channel") or row.get("channel") or "",
        "status": proj.get("status") or "created",
        "summary": proj.get("summary") or "",
        "resource_lane": resource_lane,
        "verification_story": verification_story_from_outcome(outcome),
        "delegated_lifecycle": build_delegated_lifecycle_contract(meta),
        "last_error": proj.get("last_error"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "visibility_mode": execution.get("visibility_mode") or telegram.get("visibility_mode") or "",
        "collaboration_mode": execution.get("collaboration_mode") or telegram.get("collaboration_mode") or "",
        "requested_capability": execution.get("requested_capability")
        or telegram.get("requested_capability")
        or "",
        "preferred_model_label": execution.get("preferred_model_label")
        or telegram.get("preferred_model_label")
        or "",
        "provider": openclaw.get("provider") or "",
        "model": openclaw.get("model") or "",
        "delegated_to_cursor": bool(execution.get("delegated_to_cursor")),
        "blocked_reason": outcome.get("blocked_reason") or "",
        "collaboration_trace_count": int(outcome.get("collaboration_trace_count") or 0),
        "verified_trace_count": int(outcome.get("verified_collaboration_trace_count") or 0),
        "orchestration_step_count": int(outcome.get("orchestration_step_count") or 0),
        "planner_steps": int(outcome.get("planner_steps") or 0),
        "critic_steps": int(outcome.get("critic_steps") or 0),
        "executor_steps": int(outcome.get("executor_steps") or 0),
        "synthesis_steps": int(outcome.get("synthesis_steps") or 0),
        "current_phase": outcome.get("current_phase") or "",
        "current_phase_status": outcome.get("current_phase_status") or "",
        "current_phase_lane": outcome.get("current_phase_lane") or "",
        "current_phase_summary": outcome.get("current_phase_summary") or "",
        "completed_phases": outcome.get("completed_phases") or [],
        "principal_id": identity.get("principal_id") or "",
        "pending_reminder_count": int(outcome.get("pending_reminder_count") or 0),
        "agent_url": cursor.get("agent_url") or "",
        "pr_url": cursor.get("pr_url") or "",
        "openclaw_session_id": openclaw.get("session_id") or "",
    }


def _build_optimization_summary(conn: Any) -> Dict[str, Any]:
    if not task_exists(conn, SYSTEM_TASK_ID):
        return {
            "latest_run": {},
            "recent_runs": [],
            "dominant_categories": [],
            "recent_proposals": [],
            "latest_regression": {},
            "recent_auto_heal": [],
            "latest_incident": {},
            "recent_incidents": [],
        }
    events = load_events_for_task(conn, SYSTEM_TASK_ID)
    runs: Dict[str, Dict[str, Any]] = {}
    categories: Dict[str, Dict[str, Any]] = {}
    proposals: List[Dict[str, Any]] = []
    latest_regression: Dict[str, Any] = {}
    auto_heal_events: List[Dict[str, Any]] = []
    for _seq, ts, et, payload in events:
        if et == EventType.OPTIMIZATION_RUN_STARTED.value:
            run_id = str(payload.get("run_id") or "")
            if not run_id:
                continue
            runs[run_id] = {
                "run_id": run_id,
                "status": "running",
                "actor": str(payload.get("actor") or ""),
                "analysis_mode": str(payload.get("analysis_mode") or ""),
                "started_at": ts,
                "completed_at": None,
                "gate_allowed": None,
                "proposal_count": 0,
                "finding_count": 0,
                "error": "",
            }
        elif et == EventType.OPTIMIZATION_RUN_COMPLETED.value:
            run_id = str(payload.get("run_id") or "")
            if not run_id:
                continue
            row = runs.setdefault(
                run_id,
                {
                    "run_id": run_id,
                    "status": "completed",
                    "actor": str(payload.get("actor") or ""),
                    "analysis_mode": str(payload.get("analysis_mode") or ""),
                    "started_at": None,
                    "completed_at": ts,
                    "gate_allowed": None,
                    "proposal_count": 0,
                    "finding_count": 0,
                    "error": "",
                },
            )
            row["status"] = "completed"
            row["completed_at"] = ts
            row["gate_allowed"] = bool(payload.get("gate_allowed"))
            row["proposal_count"] = int(payload.get("proposal_count") or 0)
            row["finding_count"] = int(payload.get("finding_count") or 0)
            ae = payload.get("autonomy_evidence")
            if isinstance(ae, dict) and ae:
                row["autonomy_evidence"] = dict(ae)
        elif et == EventType.OPTIMIZATION_RUN_FAILED.value:
            run_id = str(payload.get("run_id") or "")
            if not run_id:
                continue
            row = runs.setdefault(
                run_id,
                {
                    "run_id": run_id,
                    "status": "failed",
                    "actor": str(payload.get("actor") or ""),
                    "analysis_mode": str(payload.get("analysis_mode") or ""),
                    "started_at": None,
                    "completed_at": ts,
                    "gate_allowed": False,
                    "proposal_count": 0,
                    "finding_count": 0,
                    "error": "",
                },
            )
            row["status"] = "failed"
            row["completed_at"] = ts
            row["error"] = str(payload.get("error") or "")
        elif et == EventType.EVALUATION_RECORDED.value:
            category = str(payload.get("category") or "").strip()
            if not category:
                continue
            bucket = categories.setdefault(
                category,
                {"category": category, "count": 0, "severity": str(payload.get("severity") or "medium")},
            )
            bucket["count"] += int(payload.get("count") or 1)
            if payload.get("severity"):
                bucket["severity"] = str(payload.get("severity"))
        elif et == EventType.OPTIMIZATION_PROPOSAL.value:
            proposals.append(
                {
                    "proposal_id": str(payload.get("proposal_id") or ""),
                    "title": str(payload.get("title") or ""),
                    "category": str(payload.get("category") or ""),
                    "status": str(payload.get("status") or ""),
                    "preferred_execution_lane": str(
                        payload.get("preferred_execution_lane") or ""
                    ),
                    "branch_prep_allowed": bool(payload.get("branch_prep_allowed")),
                    "ts": ts,
                }
            )
        elif et == EventType.REGRESSION_RECORDED.value:
            latest_regression = {
                "passed": bool(payload.get("passed")),
                "total": int(payload.get("total") or 0),
                "command": str(payload.get("command") or ""),
                "actor": str(payload.get("actor") or ""),
                "ts": ts,
            }
        elif et in (
            EventType.LOCAL_AUTO_HEAL_STARTED.value,
            EventType.LOCAL_AUTO_HEAL_COMPLETED.value,
            EventType.LOCAL_AUTO_HEAL_FAILED.value,
        ):
            auto_heal_events.append(
                {
                    "proposal_id": str(payload.get("proposal_id") or ""),
                    "title": str(payload.get("title") or ""),
                    "category": str(payload.get("category") or ""),
                    "branch": str(payload.get("branch") or ""),
                    "status": "running"
                    if et == EventType.LOCAL_AUTO_HEAL_STARTED.value
                    else "completed"
                    if et == EventType.LOCAL_AUTO_HEAL_COMPLETED.value
                    else "failed",
                    "agent_url": str(payload.get("agent_url") or ""),
                    "pr_url": str(payload.get("pr_url") or ""),
                    "error": str(payload.get("error") or ""),
                    "submission_status": str(payload.get("submission_status") or ""),
                    "terminal_cursor_status": str(payload.get("terminal_cursor_status") or ""),
                    "verification_status": str(payload.get("verification_status") or ""),
                    "next_action": str(payload.get("next_action") or ""),
                    "ts": ts,
                }
            )

    recent_runs = sorted(
        runs.values(),
        key=lambda row: float(row.get("completed_at") or row.get("started_at") or 0.0),
        reverse=True,
    )[:6]
    dominant_categories = sorted(
        categories.values(),
        key=lambda row: (-int(row.get("count") or 0), str(row.get("category") or "")),
    )[:8]
    recent_proposals = sorted(
        proposals, key=lambda row: float(row.get("ts") or 0.0), reverse=True
    )[:8]
    raw_incidents = list_incidents(conn, limit=6)
    projected = [_project_incident_for_dashboard(row) for row in raw_incidents]
    return {
        "latest_run": recent_runs[0] if recent_runs else {},
        "recent_runs": recent_runs,
        "dominant_categories": dominant_categories,
        "recent_proposals": recent_proposals,
        "latest_regression": latest_regression,
        "recent_auto_heal": sorted(
            auto_heal_events,
            key=lambda row: float(row.get("ts") or 0.0),
            reverse=True,
        )[:8],
        "latest_incident": projected[0] if projected else {},
        "recent_incidents": projected,
    }


def _build_memory_summary(conn: Any) -> Dict[str, Any]:
    return {
        "principal_count": count_principals(conn),
        "active_memory_count": count_active_memories(conn),
        "pending_reminder_count": count_pending_reminders(conn),
        "due_reminder_count": count_due_reminders(conn),
    }


def _build_background_autonomy_summary(conn: Any) -> Dict[str, Any]:
    """Snapshot for idle background optimizer: experience-derived regression gate."""
    max_age = float(os.environ.get("ANDREA_SYNC_BACKGROUND_REGRESSION_MAX_AGE_SECONDS") or "172800")
    report, meta = build_background_regression_report(conn, max_age_seconds=max_age)
    gate = evaluate_autonomy_gate(
        conn,
        regression_report=report if report else None,
        required_skills=["cursor_handoff"],
    )
    return {
        "max_age_seconds": max_age,
        "experience_run_id": str(meta.get("run_id") or ""),
        "regression_source": str(meta.get("source") or ""),
        "fresh": bool(meta.get("fresh")),
        "age_seconds": meta.get("age_seconds"),
        "total_checks": int(meta.get("total_checks") or 0),
        "eligible_for_background_repair": bool(meta.get("eligible_for_background_repair")),
        "blocked_reason": str(meta.get("blocked_reason") or ""),
        "gate_allowed": bool(gate.get("allowed")),
        "gate_reasons": list(gate.get("reasons") or []),
    }


def _build_experience_assurance_summary(conn: Any) -> Dict[str, Any]:
    latest = get_latest_experience_run(conn)
    recent = list_experience_runs(conn, limit=6)
    if not recent:
        return {
            "latest_run": {},
            "recent_runs": [],
            "failing_scenarios": [],
            "delegated_summary": {},
            "delegated_regressions": [],
            "score_counts": {},
            "category_counts": [],
            "pass_rate": None,
        }
    latest_checks = latest.get("checks") if isinstance(latest.get("checks"), list) else []
    delegated_checks = []
    delegated_failures = []
    for row in latest_checks:
        if not isinstance(row, dict):
            continue
        tags = [str(tag or "").strip().lower() for tag in row.get("tags") or []]
        if "delegated" not in tags:
            continue
        delegated_checks.append(row)
        if not row.get("passed"):
            delegated_failures.append(row)
    delegated_average = 0.0
    if delegated_checks:
        delegated_average = round(
            sum(float(row.get("score") or 0.0) for row in delegated_checks)
            / float(len(delegated_checks)),
            2,
        )
    recent_rows: List[Dict[str, Any]] = []
    passing = 0
    for row in recent:
        if row.get("passed"):
            passing += 1
        recent_rows.append(
            {
                "run_id": str(row.get("run_id") or ""),
                "status": str(row.get("status") or "completed"),
                "passed": bool(row.get("passed")),
                "summary": str(row.get("summary") or ""),
                "average_score": float(row.get("average_score") or 0.0),
                "failed_checks": int(row.get("failed_checks") or 0),
                "total_checks": int(row.get("total_checks") or 0),
                "completed_at": float(row.get("completed_at") or row.get("updated_at") or 0.0),
            }
        )
    return {
        "latest_run": {
            "run_id": str(latest.get("run_id") or ""),
            "status": str(latest.get("status") or "completed"),
            "passed": bool(latest.get("passed")),
            "summary": str(latest.get("summary") or ""),
            "average_score": float(latest.get("average_score") or 0.0),
            "failed_checks": int(latest.get("failed_checks") or 0),
            "total_checks": int(latest.get("total_checks") or 0),
            "completed_at": float(latest.get("completed_at") or latest.get("updated_at") or 0.0),
        },
        "recent_runs": recent_rows,
        "failing_scenarios": list(latest.get("failed_scenarios") or [])[:8],
        "delegated_summary": {
            "total": len(delegated_checks),
            "failed": len(delegated_failures),
            "passed": max(0, len(delegated_checks) - len(delegated_failures)),
            "average_score": delegated_average,
        },
        "delegated_regressions": delegated_failures[:6],
        "score_counts": latest.get("score_counts") if isinstance(latest.get("score_counts"), dict) else {},
        "category_counts": list(latest.get("category_counts") or [])[:6],
        "pass_rate": round(float(passing) / float(len(recent_rows)), 2) if recent_rows else None,
    }


def _build_plan_orchestration_summary(conn: Any) -> Dict[str, Any]:
    """Active / blocked execution plans for operator visibility."""
    try:
        rows = list_recent_execution_plans(conn, limit=40)
        active = [
            r
            for r in rows
            if str(r.get("status") or "")
            not in ("completed", "failed", "abandoned")
        ]
        awaiting = [r for r in active if str(r.get("status") or "") == "awaiting_approval"]
        blocked = [r for r in active if str(r.get("status") or "") == "blocked"]
        by_scenario: Dict[str, int] = {}
        proof_coverage = {"with_proof_class": 0, "without_proof_class": 0}
        false_support_incidents = 0
        receipt_state_counts: Dict[str, int] = {}
        blocked_trust_count = 0
        draft_only_active = 0
        first_pack_active = 0
        collaboration_plan_count = 0
        collaboration_repair_strategies: Dict[str, int] = {}
        collaboration_usefulness: Dict[str, int] = {}
        collaboration_role_invocations_total = 0
        collaboration_bounded_actions: Dict[str, int] = {}
        for r in rows:
            summ = r.get("summary") if isinstance(r.get("summary"), dict) else {}
            sid = str(summ.get("scenario_id") or "").strip()
            if sid:
                by_scenario[sid] = by_scenario.get(sid, 0) + 1
            if summ.get("proof_class"):
                proof_coverage["with_proof_class"] += 1
            else:
                proof_coverage["without_proof_class"] += 1
            if str(summ.get("support_level") or "") == "unsupported" and str(
                r.get("status") or ""
            ) in ("queued", "executing"):
                false_support_incidents += 1
            rs = str(summ.get("receipt_state") or "").strip() or "pending"
            receipt_state_counts[rs] = receipt_state_counts.get(rs, 0) + 1
            if rs == "blocked_trust":
                blocked_trust_count += 1
            st = str(r.get("status") or "")
            if st in ("draft", "awaiting_approval", "queued", "executing", "verifying"):
                c = get_contract(sid) if sid else None
                if c and str(c.support_level or "") == "draft_only":
                    draft_only_active += 1
                if sid in FIRST_SUPPORTED_SCENARIO_IDS:
                    first_pack_active += 1
            collab = summ.get("collaboration") if isinstance(summ.get("collaboration"), dict) else {}
            if collab:
                collaboration_plan_count += 1
                ls = str(collab.get("last_strategy") or "").strip() or "unknown"
                collaboration_repair_strategies[ls] = collaboration_repair_strategies.get(ls, 0) + 1
                try:
                    collaboration_role_invocations_total += int(collab.get("role_invocation_count") or 0)
                except (TypeError, ValueError):
                    pass
                uh = str(collab.get("usefulness_status") or "").strip()
                if uh:
                    collaboration_usefulness[uh] = collaboration_usefulness.get(uh, 0) + 1
                if collab.get("bounded_action_executed"):
                    act = collab.get("last_executed_action")
                    at = (
                        str((act or {}).get("type") or "unknown").strip()
                        if isinstance(act, dict)
                        else "unknown"
                    )
                    collaboration_bounded_actions[at] = collaboration_bounded_actions.get(at, 0) + 1
        first_pack_health = [
            {
                "scenario_id": pack_id,
                "active_plans": sum(
                    1
                    for row in active
                    if str(
                        (row.get("summary") or {}).get("scenario_id")
                        if isinstance(row.get("summary"), dict)
                        else ""
                    )
                    == pack_id
                ),
            }
            for pack_id in sorted(FIRST_SUPPORTED_SCENARIO_IDS)
        ]
        return {
            "ok": True,
            "recent_count": len(rows),
            "active_count": len(active),
            "awaiting_approval_count": len(awaiting),
            "blocked_count": len(blocked),
            "scenario_counts": dict(
                sorted(by_scenario.items(), key=lambda kv: (-kv[1], kv[0]))[:16]
            ),
            "proof_coverage": proof_coverage,
            "false_support_incidents": false_support_incidents,
            "receipt_state_counts": receipt_state_counts,
            "blocked_trust_count": blocked_trust_count,
            "draft_only_active_count": draft_only_active,
            "first_pack_active_count": first_pack_active,
            "first_pack_health": first_pack_health,
            "collaboration_plan_count": collaboration_plan_count,
            "collaboration_repair_strategies": dict(
                sorted(collaboration_repair_strategies.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
            ),
            "collaboration_usefulness_counts": dict(
                sorted(collaboration_usefulness.items(), key=lambda kv: (-kv[1], kv[0]))[:16]
            ),
            "collaboration_role_invocations_total": collaboration_role_invocations_total,
            "collaboration_bounded_actions": dict(
                sorted(collaboration_bounded_actions.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
            ),
            "first_supported_scenario_ids": sorted(FIRST_SUPPORTED_SCENARIO_IDS),
            "awaiting_approval": [
                {
                    "plan_id": r.get("plan_id"),
                    "task_id": r.get("task_id"),
                    "goal_id": r.get("goal_id"),
                    "status": r.get("status"),
                    "approval_state": r.get("approval_state"),
                    "scenario_id": (
                        (r.get("summary") or {}).get("scenario_id")
                        if isinstance(r.get("summary"), dict)
                        else ""
                    ),
                }
                for r in awaiting[:12]
            ],
            "blocked": [
                {
                    "plan_id": r.get("plan_id"),
                    "task_id": r.get("task_id"),
                    "verification_state": r.get("verification_state"),
                    "scenario_id": (
                        (r.get("summary") or {}).get("scenario_id")
                        if isinstance(r.get("summary"), dict)
                        else ""
                    ),
                }
                for r in blocked[:12]
            ],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _build_blueprint_platform_summary(conn: Any) -> Dict[str, Any]:
    """Counts for goals/workflows/task links + governance versions (Phase 6)."""
    try:
        g_active = conn.execute(
            "SELECT COUNT(*) AS n FROM goals WHERE status = 'active'"
        ).fetchone()
        g_total = conn.execute("SELECT COUNT(*) AS n FROM goals").fetchone()
        w = conn.execute(
            """
            SELECT COUNT(*) AS n FROM workflows
            WHERE status NOT IN ('completed', 'cancelled')
            """
        ).fetchone()
        tg = conn.execute("SELECT COUNT(*) AS n FROM task_goals").fetchone()
        return {
            "ok": True,
            "active_goals": int(g_active["n"] if g_active else 0),
            "goals_total": int(g_total["n"] if g_total else 0),
            "open_workflows": int(w["n"] if w else 0),
            "task_goal_links": int(tg["n"] if tg else 0),
            "governance": governance_snapshot(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def build_dashboard_summary(
    conn: Any,
    server: Any,
    *,
    limit: int = 30,
    webhook_snapshot: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    rows = list_tasks(conn, limit=limit)
    items: List[Dict[str, Any]] = []
    by_status: Dict[str, int] = {}
    by_channel: Dict[str, int] = {}
    for row in rows:
        task_id = str(row.get("task_id") or "")
        channel = str(row.get("channel") or "")
        if not task_id or not channel or task_id == SYSTEM_TASK_ID:
            continue
        proj = project_task_dict(conn, task_id, channel)
        item = _task_list_item(row, proj)
        items.append(item)
        st = str(item.get("status") or "created")
        ch = str(item.get("channel") or "unknown")
        by_status[st] = by_status.get(st, 0) + 1
        by_channel[ch] = by_channel.get(ch, 0) + 1

    capability_digest = get_capability_digest(conn)
    runtime_snapshot = build_runtime_truth_snapshot(
        conn,
        server,
        webhook_snapshot=webhook_snapshot,
    )
    digest = capability_digest.get("digest") if isinstance(capability_digest.get("digest"), dict) else {}
    cap_rows = digest.get("rows") if isinstance(digest.get("rows"), list) else []
    digest_valid = capability_digest.get("present") is True and bool(cap_rows)
    blocked_critical = [
        row
        for row in cap_rows
        if isinstance(row, dict) and row.get("critical") and row.get("status") == "blocked"
    ]
    attention = [
        row for row in cap_rows if isinstance(row, dict) and row.get("status") != "ready"
    ]
    if capability_digest.get("present") is False:
        attention.insert(
            0,
            {
                "id": "capabilities:digest_missing",
                "category": "capability_digest",
                "detail": "published capability snapshot",
                "status": "blocked",
                "notes": "No published capability digest is available yet.",
                "critical": True,
            },
        )
    elif not digest_valid:
        attention.insert(
            0,
            {
                "id": "capabilities:digest_invalid",
                "category": "capability_digest",
                "detail": "published capability snapshot",
                "status": "blocked",
                "notes": "Capability digest is present but missing the expected rows list.",
                "critical": True,
            },
        )
    acpx_row = next(
        (row for row in cap_rows if isinstance(row, dict) and row.get("id") == "acp_tool:acpx"),
        None,
    )
    return {
        "ok": True,
        "generated_at": time.time(),
        "service": {
            "name": "andrea_sync",
            "db": str(server.db_path),
            "kill_switch": kill_switch_status(conn),
            "capability_digest_age_seconds": digest_age_seconds(conn),
            "background_enabled": bool(server.background_enabled),
            "delegated_execution_enabled": bool(server.delegated_execution_enabled),
            "telegram_delegate_lane": server.telegram_delegate_lane,
            "openclaw_agent_id": server.openclaw_agent_id,
            "background_optimizer_enabled": bool(server.background_optimizer_enabled),
            "background_incident_repair_enabled": bool(
                getattr(server, "background_incident_repair_enabled", False)
            ),
            "repair_enabled": repair_enabled(),
            "repair_cursor_mode": str(os.environ.get("ANDREA_REPAIR_CURSOR_MODE") or "auto"),
            "repair_safe_roots": list(configured_safe_repair_roots()),
        },
        "runtime": runtime_snapshot,
        "webhook": runtime_snapshot.get("webhook") if isinstance(runtime_snapshot.get("webhook"), dict) else {},
        "capabilities": {
            "summary": digest.get("summary") if isinstance(digest.get("summary"), dict) else {},
            "blocked_critical": blocked_critical[:10],
            "attention": attention[:12],
            "acpx": acpx_row,
            "digest_present": capability_digest.get("present") is True,
            "digest_valid": digest_valid,
        },
        "tasks": {
            "limit": limit,
            "count": len(items),
            "by_status": by_status,
            "by_channel": by_channel,
            "items": items,
        },
        "memory": _build_memory_summary(conn),
        "optimization": _build_optimization_summary(conn),
        "experience_assurance": _build_experience_assurance_summary(conn),
        "background_autonomy": _build_background_autonomy_summary(conn),
        "blueprint_platform": _build_blueprint_platform_summary(conn),
        "plan_orchestration": _build_plan_orchestration_summary(conn),
        "collaboration_policy": trusted_operator_summary(conn),
        "daily_assistant_pack": build_daily_pack_operator_snapshot(conn),
        "daily_assistant_optimizer_hints": daily_pack_optimizer_hints(conn),
    }


def render_dashboard_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Andrea Monitor</title>
  <style>
    :root { color-scheme: dark; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
    body { margin: 0; background: #0b1020; color: #eef2ff; }
    .wrap { max-width: 1500px; margin: 0 auto; padding: 20px; }
    .topbar, .panel, .card { background: #11182c; border: 1px solid #24314d; border-radius: 14px; }
    .topbar { padding: 16px 18px; display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 16px; }
    h1, h2, h3, p { margin: 0; }
    .subtle { color: #9db0d2; font-size: 13px; }
    .grid { display: grid; gap: 16px; }
    .cards { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); margin-bottom: 16px; }
    .card { padding: 14px 16px; }
    .label { color: #9db0d2; font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; }
    .value { font-size: 28px; font-weight: 700; margin-top: 6px; }
    .value.sm { font-size: 18px; }
    .twoCol { grid-template-columns: minmax(0, 1.15fr) minmax(360px, 0.85fr); }
    .panel { padding: 16px; min-height: 180px; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }
    th, td { padding: 10px 8px; border-bottom: 1px solid #24314d; text-align: left; vertical-align: top; }
    tr.taskRow { cursor: pointer; }
    tr.taskRow:hover { background: #17213b; }
    tr.selected { background: #1e2d4f; }
    .pill { display: inline-block; border-radius: 999px; padding: 3px 9px; font-size: 12px; font-weight: 600; }
    .ready { background: #123524; color: #8df7b9; }
    .warn { background: #3c2c11; color: #ffd178; }
    .bad { background: #4f1d26; color: #ff9cb0; }
    .muted { background: #222b41; color: #c8d5f0; }
    .list { margin-top: 12px; display: grid; gap: 10px; }
    .item { border: 1px solid #24314d; border-radius: 12px; padding: 10px 12px; }
    .timeline { margin-top: 12px; display: grid; gap: 10px; max-height: 60vh; overflow: auto; }
    .event { border-left: 3px solid #42567f; padding: 8px 10px; background: #0d1427; border-radius: 8px; }
    .event pre { margin: 8px 0 0; white-space: pre-wrap; word-break: break-word; color: #cfe0ff; }
    button { background: #315efb; color: white; border: 0; border-radius: 10px; padding: 10px 14px; cursor: pointer; font-weight: 600; }
    button:hover { background: #426cff; }
    a { color: #93b2ff; }
    @media (max-width: 1100px) { .twoCol { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>
        <h1>Andrea Monitor</h1>
        <p class="subtle">Live operator dashboard for health, webhook, tasks, and collaboration timelines.</p>
      </div>
      <div style="text-align:right">
        <button id="refreshBtn" type="button">Refresh now</button>
        <p class="subtle" id="lastUpdated">Waiting for first poll...</p>
      </div>
    </div>

    <div class="grid cards" id="cards"></div>

    <div class="grid twoCol" style="margin-bottom:16px;">
      <section class="panel">
        <h2>Optimization Loop</h2>
        <p class="subtle">Recent autonomous eval runs, gate state, and dominant orchestration failure categories.</p>
        <div class="list" id="optimizationLoop"></div>
      </section>

      <section class="panel">
        <h2>Optimization Proposals</h2>
        <p class="subtle">Branch-prep candidates generated from recurring failures on the system timeline.</p>
        <div class="list" id="optimizationProposals"></div>
      </section>
    </div>

    <div class="grid twoCol" style="margin-bottom:16px;">
      <section class="panel">
        <h2>Experience Assurance</h2>
        <p class="subtle">Deterministic scenario replay for calmness, routing selectivity, and capability honesty.</p>
        <div class="list" id="experienceAssurance"></div>
      </section>

      <section class="panel">
        <h2>Experience Regressions</h2>
        <p class="subtle">Latest failing scenarios, score drops, and likely files if the experience slips.</p>
        <div class="list" id="experienceFailures"></div>
      </section>
    </div>

    <div class="grid twoCol" style="margin-bottom:16px;">
      <section class="panel">
        <h2>Daily Assistant pack</h2>
        <p class="subtle">Trusted low-risk continuity: receipts, Telegram continuation records, reminder repair signals, onboarding states, and receipt evidence vs rollout thresholds.</p>
        <div class="list" id="dailyAssistantPack"></div>
      </section>
      <section class="panel">
        <h2>Follow-through &amp; closure</h2>
        <p class="subtle">Open loops, closure decisions, follow-up recommendations (shadow by default), stale workflow/delivery signals, and operator status override via internal API.</p>
        <div class="list" id="followthroughBoard"></div>
      </section>
    </div>

    <div class="grid" style="margin-bottom:16px;">
      <section class="panel">
        <h2>Collaboration rollout workspace</h2>
        <p class="subtle">Promotion revisions, operator actions, scenario onboarding, live-vs-shadow comparison history, and evidence-gated candidates.</p>
        <div class="list" id="collaborationPromotion"></div>
      </section>
    </div>

    <div class="grid twoCol">
      <section class="panel">
        <h2>Recent Tasks</h2>
        <p class="subtle">Latest projected tasks across Telegram, Alexa, CLI, and delegated lanes.</p>
        <div id="tasks"></div>
      </section>

      <section class="panel">
        <h2>Attention Queue</h2>
        <p class="subtle">Capability blockers, webhook state, and ACP router readiness.</p>
        <div class="list" id="attention"></div>
      </section>
    </div>

    <div class="grid twoCol" style="margin-top:16px;">
      <section class="panel">
        <h2>Task Detail</h2>
        <p class="subtle" id="detailSummary">Select a task to inspect projected metadata and links.</p>
        <div class="list" id="taskMeta"></div>
      </section>

      <section class="panel">
        <h2>Event Timeline</h2>
        <p class="subtle">Append-only task events from the lockstep store.</p>
        <div class="timeline" id="timeline"></div>
      </section>
    </div>
  </div>

  <script>
    let selectedTaskId = "";
    let latestSummary = null;

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
    }

    function formatTs(ts) {
      if (ts === null || ts === undefined || ts === "") return "n/a";
      const num = Number(ts);
      if (!Number.isFinite(num)) return escapeHtml(ts);
      return new Date(num * 1000).toLocaleString();
    }

    function pillClass(status) {
      if (status === "ready" || status === "healthy" || status === "completed") return "ready";
      if (status === "blocked" || status === "drifted" || status === "failed" || status === "error") return "bad";
      return "warn";
    }

    async function fetchJson(url) {
      const resp = await fetch(url, { cache: "no-store" });
      if (!resp.ok) throw new Error(`${url} -> ${resp.status}`);
      return await resp.json();
    }

    function renderCards(data) {
      const latestRun = (data.optimization || {}).latest_run || {};
      const latestExperience = (data.experience_assurance || {}).latest_run || {};
      const bgAuto = data.background_autonomy || {};
      const runtime = data.runtime || {};
      const runtimeTelegram = runtime.telegram || {};
      const experienceStatus = !latestExperience.run_id
        ? "warn"
        : (latestExperience.passed ? "healthy" : "failed");
      const bgGateOk = !!bgAuto.gate_allowed;
      const bgFresh = !!bgAuto.fresh;
      const bgLabel = !bgAuto.experience_run_id ? "no_run" : (bgFresh ? "fresh" : "stale");
      const cards = [
        { label: "Kill Switch", value: data.service.kill_switch.engaged ? "ENGAGED" : "Released", status: data.service.kill_switch.engaged ? "blocked" : "ready", note: "Server safety state" },
        { label: "Webhook", value: data.webhook.status, status: data.webhook.status, note: data.webhook.reason || "Telegram webhook state" },
        { label: "Public Base", value: runtimeTelegram.public_base || "unset", status: runtimeTelegram.public_base ? "ready" : "warn", note: `Process-authoritative Telegram base · pid ${runtime.pid || "n/a"}` },
        { label: "Recent Tasks", value: String(data.tasks.count), status: "ready", note: `Limit ${data.tasks.limit}` },
        { label: "Blocked Caps", value: String((data.capabilities.summary || {}).blocked || 0), status: ((data.capabilities.summary || {}).blocked || 0) > 0 ? "blocked" : "ready", note: "Published capability digest" },
        { label: "ACPX", value: data.capabilities.acpx ? data.capabilities.acpx.status : "digest-missing", status: data.capabilities.acpx ? data.capabilities.acpx.status : "blocked", note: data.capabilities.acpx ? data.capabilities.acpx.notes : "No published acpx row is available yet" },
        { label: "Digest Age", value: `${Math.round(Number(data.service.capability_digest_age_seconds || 0))}s`, status: Number(data.service.capability_digest_age_seconds || 0) > 1800 ? "warn" : "ready", note: "Capability snapshot freshness" },
        { label: "Repair Loop", value: data.service.repair_enabled ? "enabled" : "disabled", status: data.service.repair_enabled ? "ready" : "warn", note: `Cursor ${data.service.repair_cursor_mode || "auto"} · safe roots ${(data.service.repair_safe_roots || []).join(", ") || "default"}` },
        { label: "Optimizer", value: latestRun.status || "idle", status: latestRun.status || "warn", note: latestRun.run_id ? `Latest run ${latestRun.run_id}` : "No optimization run recorded yet" },
        { label: "Experience", value: latestExperience.run_id ? `${Math.round(Number(latestExperience.average_score || 0))}` : "idle", status: experienceStatus, note: latestExperience.run_id ? `${latestExperience.failed_checks || 0}/${latestExperience.total_checks || 0} failed in ${latestExperience.run_id}` : "No experience replay has been recorded yet" },
        { label: "Bg autonomy", value: bgLabel, status: bgGateOk ? "healthy" : "warn", note: bgAuto.experience_run_id ? `Gate ${bgGateOk ? "open" : "closed"} · age ${bgAuto.age_seconds != null ? Math.round(Number(bgAuto.age_seconds)) + "s" : "n/a"} · max ${Math.round(Number(bgAuto.max_age_seconds || 0))}s${(bgAuto.gate_reasons || []).length ? " · " + (bgAuto.gate_reasons || []).join(", ") : ""}` : (bgAuto.blocked_reason || "Run experience replay to unlock idle optimizer gate") }
      ];
      document.getElementById("cards").innerHTML = cards.map((card) => `
        <div class="card">
          <div class="label">${escapeHtml(card.label)}</div>
          <div class="value ${String(card.value).length > 16 ? "sm" : ""}">${escapeHtml(card.value)}</div>
          <div class="pill ${pillClass(card.status)}" style="margin-top:10px;">${escapeHtml(card.status)}</div>
          <p class="subtle" style="margin-top:10px;">${escapeHtml(card.note)}</p>
        </div>
      `).join("");
    }

    function renderAttention(data) {
      const items = [];
      const runtime = data.runtime || {};
      const runtimeTelegram = runtime.telegram || {};
      items.push({
        title: `Webhook: ${data.webhook.status}`,
        note: data.webhook.reason || "No webhook note",
        extra: data.webhook.current_url ? `Current: ${data.webhook.current_url}` : (data.webhook.expected_url ? `Expected: ${data.webhook.expected_url}` : ""),
        status: data.webhook.status
      });
      items.push({
        title: "Runtime truth",
        note: `Process pid ${runtime.pid || "n/a"} · public base ${runtimeTelegram.public_base || "unset"}`,
        extra: `Digest ${runtime.capability_digest_status || "unknown"}${runtime.capability_digest_age_seconds !== undefined && runtime.capability_digest_age_seconds !== null ? ` · age ${Math.round(Number(runtime.capability_digest_age_seconds || 0))}s` : ""}`,
        status: runtime.capability_digest_status === "stale" ? "warn" : (runtime.capability_digest_status === "missing" ? "blocked" : "ready")
      });
      for (const row of data.capabilities.blocked_critical || []) {
        items.push({
          title: row.id,
          note: row.notes || row.detail || "",
          extra: row.detail || "",
          status: row.status || "blocked"
        });
      }
      if ((data.capabilities.blocked_critical || []).length === 0) {
        for (const row of data.capabilities.attention || []) {
          items.push({
            title: row.id,
            note: row.notes || row.detail || "",
            extra: row.detail || "",
            status: row.status || "ready_with_limits"
          });
          if (items.length >= 8) break;
        }
      }
      document.getElementById("attention").innerHTML = items.map((item) => `
        <div class="item">
          <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;">
            <strong>${escapeHtml(item.title)}</strong>
            <span class="pill ${pillClass(item.status)}">${escapeHtml(item.status)}</span>
          </div>
          <p class="subtle" style="margin-top:8px;">${escapeHtml(item.note)}</p>
          ${item.extra ? `<p class="subtle" style="margin-top:6px;">${escapeHtml(item.extra)}</p>` : ""}
        </div>
      `).join("") || `<div class="item"><strong>No active issues</strong><p class="subtle" style="margin-top:8px;">Capability digest and webhook look healthy.</p></div>`;
    }

    function renderOptimization(data) {
      const opt = data.optimization || {};
      const latest = opt.latest_run || {};
      const categories = opt.dominant_categories || [];
      const recentRuns = opt.recent_runs || [];
      const proposals = opt.recent_proposals || [];
      const incidents = opt.recent_incidents || [];

      const loopItems = [];
      if (latest.run_id) {
        loopItems.push({
          title: `Latest run: ${latest.run_id}`,
          note: `Status ${latest.status || "unknown"}${latest.analysis_mode ? ` · ${latest.analysis_mode}` : ""}`,
          extra: `Findings ${latest.finding_count || 0} · Proposals ${latest.proposal_count || 0} · Gate ${latest.gate_allowed === true ? "open" : latest.gate_allowed === false ? "gated" : "n/a"}`,
          status: latest.status || "warn"
        });
      }
      if (incidents.length) {
        const latestIncident = incidents[0] || {};
        const incidentState = latestIncident.current_state || latestIncident.status || "open";
        const incidentPill = incidentState === "resolved"
          ? "ready"
          : (incidentState === "cursor_handoff_ready" || incidentState === "human_review_required" || incidentState === "rolled_back" || incidentState === "cursor_verifying")
            ? "warn"
            : "bad";
        const cond = latestIncident.conductor_preferred_executor || "";
        const reasons = (latestIncident.conductor_reasons || []).join(", ");
        const condLine = cond
          ? `Preferred executor ${cond}${reasons ? ` · ${reasons}` : ""}`
          : (reasons ? `Escalation: ${reasons}` : "");
        const handoffBits = [];
        if (latestIncident.cursor_handoff_active) {
          handoffBits.push("Cursor handoff ok");
          if (latestIncident.cursor_handoff_branch) handoffBits.push(latestIncident.cursor_handoff_branch);
        } else if (latestIncident.cursor_handoff_error) {
          handoffBits.push(`Handoff: ${latestIncident.cursor_handoff_error}`);
        } else if (latestIncident.conductor_effective_cursor_execute && !latestIncident.cursor_handoff_active) {
          handoffBits.push("Cursor handoff attempted");
        }
        const outcomeBits = [];
        if (latestIncident.conductor_outcome_verification_status) {
          outcomeBits.push(`verify ${latestIncident.conductor_outcome_verification_status}`);
        }
        if (latestIncident.conductor_outcome_next_action) {
          outcomeBits.push(`next ${latestIncident.conductor_outcome_next_action}`);
        }
        const extraParts = [
          `State ${incidentState}${latestIncident.confidence !== undefined ? ` · confidence ${latestIncident.confidence}` : ""}`,
          condLine,
          handoffBits.join(" · "),
          outcomeBits.join(" · "),
        ].filter(Boolean);
        loopItems.push({
          title: `Latest incident: ${latestIncident.error_type || latestIncident.incident_id || "incident"}`,
          note: latestIncident.summary || "No incident summary",
          extra: extraParts.join(" · "),
          status: incidentPill
        });
      }
      for (const row of categories.slice(0, 4)) {
        loopItems.push({
          title: row.category,
          note: `Observed ${row.count} time(s)`,
          extra: `Severity ${row.severity || "medium"}`,
          status: row.severity === "high" ? "bad" : "warn"
        });
      }
      document.getElementById("optimizationLoop").innerHTML = loopItems.map((item) => `
        <div class="item">
          <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;">
            <strong>${escapeHtml(item.title)}</strong>
            <span class="pill ${pillClass(item.status)}">${escapeHtml(item.status)}</span>
          </div>
          <p class="subtle" style="margin-top:8px;">${escapeHtml(item.note)}</p>
          ${item.extra ? `<p class="subtle" style="margin-top:6px;">${escapeHtml(item.extra)}</p>` : ""}
        </div>
      `).join("") || `<div class="item"><strong>No optimizer runs yet</strong><p class="subtle" style="margin-top:8px;">Once Andrea reviews recent outcomes, the autonomous loop will appear here.</p></div>`;

      document.getElementById("optimizationProposals").innerHTML = proposals.map((proposal) => `
        <div class="item">
          <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;">
            <strong>${escapeHtml(proposal.title || proposal.proposal_id || "proposal")}</strong>
            <span class="pill ${pillClass(proposal.branch_prep_allowed ? "ready" : (proposal.status || "warn"))}">${escapeHtml(proposal.status || "proposed")}</span>
          </div>
          <p class="subtle" style="margin-top:8px;">${escapeHtml(proposal.category || "uncategorized")} · ${escapeHtml(proposal.preferred_execution_lane || "n/a")}</p>
          <p class="subtle" style="margin-top:6px;">${escapeHtml(formatTs(proposal.ts))}</p>
        </div>
      `).join("") || `<div class="item"><strong>No proposals yet</strong><p class="subtle" style="margin-top:8px;">The optimizer will list branch-prep candidates here once recurring failures are detected.</p></div>`;
    }

    function renderExperience(data) {
      const exp = data.experience_assurance || {};
      const latest = exp.latest_run || {};
      const recentRuns = exp.recent_runs || [];
      const failures = exp.failing_scenarios || [];
      const delegated = exp.delegated_summary || {};
      const delegatedFailures = exp.delegated_regressions || [];
      const categories = exp.category_counts || [];
      const scoreCounts = exp.score_counts || {};

      const runItems = [];
      if (latest.run_id) {
        runItems.push({
          title: `Latest run: ${latest.run_id}`,
          note: latest.summary || "No experience summary",
          extra: `Avg score ${Math.round(Number(latest.average_score || 0))} · Failed ${latest.failed_checks || 0}/${latest.total_checks || 0}${exp.pass_rate !== null && exp.pass_rate !== undefined ? ` · recent pass rate ${Math.round(Number(exp.pass_rate) * 100)}%` : ""}`,
          status: latest.passed ? "ready" : "failed"
        });
      }
      if (Object.keys(scoreCounts).length) {
        runItems.push({
          title: "Score distribution",
          note: `Excellent ${scoreCounts.excellent || 0} · Warn ${scoreCounts.warn || 0} · Failed ${scoreCounts.failed || 0}`,
          extra: "",
          status: (scoreCounts.failed || 0) > 0 ? "failed" : "ready"
        });
      }
      if (delegated.total) {
        runItems.push({
          title: "Delegated lane replay",
          note: `Passed ${delegated.passed || 0}/${delegated.total || 0} delegated scenarios`,
          extra: `Avg score ${Math.round(Number(delegated.average_score || 0))} · Failed ${delegated.failed || 0}`,
          status: (delegated.failed || 0) > 0 ? "warn" : "ready"
        });
      }
      for (const row of categories.slice(0, 3)) {
        runItems.push({
          title: row.category || "experience",
          note: `Regressed ${row.count || 0} scenario(s)`,
          extra: (row.issue_codes || []).join(", "),
          status: "warn"
        });
      }
      for (const row of recentRuns.slice(1, 4)) {
        runItems.push({
          title: row.run_id,
          note: row.summary || "No experience summary",
          extra: `Avg score ${Math.round(Number(row.average_score || 0))} · Failed ${row.failed_checks || 0}/${row.total_checks || 0} · ${formatTs(row.completed_at)}`,
          status: row.passed ? "ready" : "warn"
        });
      }
      document.getElementById("experienceAssurance").innerHTML = runItems.map((item) => `
        <div class="item">
          <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;">
            <strong>${escapeHtml(item.title)}</strong>
            <span class="pill ${pillClass(item.status)}">${escapeHtml(item.status)}</span>
          </div>
          <p class="subtle" style="margin-top:8px;">${escapeHtml(item.note)}</p>
          ${item.extra ? `<p class="subtle" style="margin-top:6px;">${escapeHtml(item.extra)}</p>` : ""}
        </div>
      `).join("") || `<div class="item"><strong>No assurance runs yet</strong><p class="subtle" style="margin-top:8px;">Run the local experience replay to populate deterministic UX checks.</p></div>`;

      const failureRows = [...delegatedFailures, ...failures.filter((row) => !delegatedFailures.some((delegatedRow) => delegatedRow.scenario_id === row.scenario_id))];
      document.getElementById("experienceFailures").innerHTML = failureRows.map((row) => `
        <div class="item">
          <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;">
            <strong>${escapeHtml((delegatedFailures.some((delegatedRow) => delegatedRow.scenario_id === row.scenario_id) ? "[delegated] " : "") + (row.title || row.scenario_id || "scenario"))}</strong>
            <span class="pill ${pillClass(Number(row.score || 0) >= 70 ? "warn" : "failed")}">${escapeHtml(String(row.score || 0))}</span>
          </div>
          <p class="subtle" style="margin-top:8px;">${escapeHtml(row.summary || "No failure summary")}</p>
          <p class="subtle" style="margin-top:6px;">${escapeHtml((row.issue_codes || []).join(", ") || "No issue codes")}</p>
          ${row.suspected_files && row.suspected_files.length ? `<p class="subtle" style="margin-top:6px;">Likely files: ${escapeHtml((row.suspected_files || []).join(", "))}</p>` : ""}
        </div>
      `).join("") || `<div class="item"><strong>No active experience regressions</strong><p class="subtle" style="margin-top:8px;">The latest deterministic replay passed.</p></div>`;
    }

    function renderDailyAssistantPack(data) {
      const pack = data.daily_assistant_pack || {};
      const hints = data.daily_assistant_optimizer_hints || [];
      const ft = pack.followthrough_board || {};
      const ftItems = [];
      if (ft.metrics) {
        const m = ft.metrics;
        ftItems.push({
          title: `Follow-through status: ${ft.followthrough_pack_status || "n/a"}`,
          note: `Closure rate (7d): ${(m.closure_rate !== null && m.closure_rate !== undefined) ? Math.round(Number(m.closure_rate) * 100) + "%" : "n/a"} · open-loop rows: ${m.open_loop_count || 0} · closure decisions: ${m.closure_decision_count || 0}`,
          extra: ft.quiet_auto_exec ? "Quiet auto-exec flag ON (ledger)" : "Quiet auto-exec off",
          status: ft.followthrough_pack_status === "frozen" ? "blocked" : "ready",
        });
      }
      for (const row of (ft.recent_closure_decisions || []).slice(0, 6)) {
        ftItems.push({
          title: `Closure → ${row.closure_state || "n/a"}`,
          note: (row.reason || "").slice(0, 220),
          extra: row.task_id || "",
          status: row.closure_state === "needs_repair" ? "bad" : (row.closure_state === "completed" ? "ready" : "warn"),
        });
      }
      for (const row of (ft.recent_followup_recommendations || []).slice(0, 4)) {
        ftItems.push({
          title: `Follow-up reco (${row.shadow_only ? "shadow" : "live"})`,
          note: (row.why_now || "").slice(0, 220),
          extra: row.recommended_action || "",
          status: row.shadow_only ? "muted" : "warn",
        });
      }
      document.getElementById("followthroughBoard").innerHTML = ftItems.map((item) => `
        <div class="item">
          <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;">
            <strong>${escapeHtml(item.title)}</strong>
            <span class="pill ${pillClass(item.status)}">${escapeHtml(item.status)}</span>
          </div>
          <p class="subtle" style="margin-top:8px;">${escapeHtml(item.note)}</p>
          ${item.extra ? `<p class="subtle" style="margin-top:6px;">${escapeHtml(item.extra)}</p>` : ""}
        </div>
      `).join("") || `<div class="item"><strong>No follow-through rows yet</strong><p class="subtle" style="margin-top:8px;">Enable ANDREA_FOLLOWTHROUGH_ENABLED and flow daily-pack receipts.</p></div>`;

      const items = [];
      const ps = pack.proving_signals || {};
      const evg = (pack.live_rollout_evidence && pack.live_rollout_evidence.evidence_gate_detail) || {};
      const covLabel = (ps.receipt_coverage_rate_7d !== null && ps.receipt_coverage_rate_7d !== undefined)
        ? Math.round(Number(ps.receipt_coverage_rate_7d) * 100) + "%"
        : (Number(ps.routed_task_count_7d || 0) > 0 ? "n/a" : "insufficient sample");
      const qualLabel = (ps.receipt_quality_rate_7d !== null && ps.receipt_quality_rate_7d !== undefined)
        ? Math.round(Number(ps.receipt_quality_rate_7d) * 100) + "%" : "n/a";
      const blockers = (evg.blocking_signals && evg.blocking_signals.length)
        ? evg.blocking_signals.join(", ")
        : ((pack.live_rollout_evidence && pack.live_rollout_evidence.evidence_notes) || []).join(", ");
      items.push({
        title: `Pack ${pack.pack_id || "trusted_daily_continuity_v1"}`,
        note: (pack.live_rollout_slice && pack.live_rollout_slice.description) ? pack.live_rollout_slice.description.slice(0, 280) : "Low-risk daily assistant continuity and productivity.",
        extra: `Receipts 7d: ${(pack.receipt_metrics && pack.receipt_metrics.receipt_count) || 0} · pass rate ${(pack.receipt_metrics && pack.receipt_metrics.receipt_pass_rate !== null && pack.receipt_metrics.receipt_pass_rate !== undefined) ? Math.round(Number(pack.receipt_metrics.receipt_pass_rate) * 100) + "%" : "n/a"} · routed tasks 7d: ${ps.routed_task_count_7d ?? 0} · receipt coverage ${covLabel} · quality ${qualLabel}${blockers ? " · blockers: " + blockers : ""}`,
        status: (pack.live_rollout_evidence && pack.live_rollout_evidence.evidence_ok) ? "ready" : "warn",
      });
      for (const row of (pack.scenarios || [])) {
        items.push({
          title: `Scenario ${row.scenario_id} → ${row.effective_onboarding_state}`,
          note: row.blocks_live_advisory ? "Live collaboration advisory blocked (direct-first pack default)." : "Live collaboration advisory allowed if other gates pass.",
          extra: row.daily_assistant_pack ? "daily pack member" : "",
          status: row.blocks_live_advisory ? "muted" : "ready",
        });
      }
      for (const row of (pack.recent_continuations || []).slice(0, 6)) {
        items.push({
          title: `Continuation → ${row.linked_task_id}`,
          note: row.reason || "telegram continuation",
          extra: row.confidence_band || "",
          status: "muted",
        });
      }
      for (const row of (pack.recent_domain_repairs || []).slice(0, 4)) {
        items.push({
          title: `Domain repair: ${row.repair_family}`,
          note: row.result || "",
          extra: row.scenario_id || "",
          status: "warn",
        });
      }
      for (const h of hints.slice(0, 4)) {
        items.push({
          title: h.title || "Daily pack hint",
          note: h.detail || "",
          extra: h.category || "",
          status: h.severity === "high" ? "bad" : "warn",
        });
      }
      document.getElementById("dailyAssistantPack").innerHTML = items.map((item) => `
        <div class="item">
          <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;">
            <strong>${escapeHtml(item.title)}</strong>
            <span class="pill ${pillClass(item.status)}">${escapeHtml(item.status)}</span>
          </div>
          <p class="subtle" style="margin-top:8px;">${escapeHtml(item.note)}</p>
          ${item.extra ? `<p class="subtle" style="margin-top:6px;">${escapeHtml(item.extra)}</p>` : ""}
        </div>
      `).join("") || `<div class="item"><strong>No daily pack rows yet</strong><p class="subtle" style="margin-top:8px;">Receipts and continuation records will populate as low-risk turns flow.</p></div>`;
    }

    function renderCollaborationPromotion(data) {
      const pol = data.collaboration_policy || {};
      const pr = pol.promotion_state || {};
      const rw = pol.rollout_workspace || {};
      const items = [];
      if (rw.rollout_manager_version) {
        items.push({
          title: `Rollout manager ${rw.rollout_manager_version}`,
          note: "Internal API: GET /v1/internal/rollout/candidates and POST /v1/internal/rollout (Bearer ANDREA_SYNC_INTERNAL_TOKEN).",
          extra: "",
          status: "muted",
        });
      }
      if (!pr.promotion_controller_enabled) {
        items.push({
          title: "Promotion controller disabled",
          note: "Set ANDREA_SYNC_COLLAB_PROMOTION_ENABLED=1 to enforce persisted promotion and rollback semantics.",
          extra: "",
          status: "muted",
        });
      } else {
        items.push({
          title: pr.promotion_global_freeze ? "Global promotion freeze ON" : "Global promotion freeze off",
          note: `Rollback auto: ${pr.rollback_enabled ? "on" : "off"} · static allowlist ${(pr.allowlist || []).join(", ") || "defaults"}`,
          extra: `Promotion ${pr.promotion_controller_version || "n/a"} · dynamic grants ${(rw.dynamic_subject_grants || []).length} · effective subjects ${(pr.effective_allowlist || pr.allowlist || []).length}`,
          status: pr.promotion_global_freeze ? "blocked" : "ready",
        });
      }
      for (const row of rw.scenario_onboarding || []) {
        items.push({
          title: `Scenario onboarding: ${row.scenario_id} → ${row.effective_state}`,
          note: row.draft_only ? "Draft-only catalog entry: live advisory blocked at onboarding layer." : (row.blocks_live_advisory ? "Live advisory blocked until onboarding advances." : "Live advisory allowed if promotion gates pass."),
          extra: "",
          status: row.blocks_live_advisory ? "warn" : "ready",
        });
      }
      for (const row of (rw.operator_actions_recent || []).slice(0, 8)) {
        items.push({
          title: `Operator ${row.action_kind}: ${row.subject_key || "n/a"}`,
          note: `Actor ${row.actor || "n/a"} · ${row.decision || ""}`,
          extra: row.revision_id ? `revision ${row.revision_id}` : (row.reason || "").slice(0, 120),
          status: row.action_kind === "rollback" ? "bad" : (row.action_kind === "freeze" ? "blocked" : "ready"),
        });
      }
      for (const row of (rw.live_shadow_comparisons_recent || []).slice(0, 6)) {
        items.push({
          title: `Compare: ${row.subject_key}`,
          note: JSON.stringify(row.deltas || {}),
          extra: row.comparison_id || "",
          status: "muted",
        });
      }
      for (const g of (rw.dynamic_subject_grants || []).slice(0, 6)) {
        items.push({
          title: `Grant: ${g.subject_key}`,
          note: `Actor ${g.actor || "n/a"}`,
          extra: g.notes || "",
          status: "warn",
        });
      }
      for (const row of pr.active_promotions || []) {
        items.push({
          title: `${row.subject_key} → ${row.promotion_level}`,
          note: `revision ${row.revision_id || "n/a"} · op_ack ${row.operator_ack ? "yes" : "no"}`,
          extra: row.risk_notes || "",
          status: row.promotion_level === "frozen" ? "blocked" : (row.promotion_level === "bounded_action" ? "warn" : "ready"),
        });
      }
      for (const c of pr.promotion_candidates || []) {
        items.push({
          title: `Candidate: ${c.subject_key}`,
          note: "Meets live-advisory promotion evidence thresholds (operator confirm still required to persist).",
          extra: `useful_rate ${(c.stats && c.stats.useful_rate) || "n/a"} · samples ${(c.stats && c.stats.samples) || "n/a"}`,
          status: "warn",
        });
      }
      for (const r of pr.recent_rollbacks || []) {
        items.push({
          title: `Rollback: ${r.subject_key}`,
          note: (r.reason_codes || []).join(", ") || r.trigger_type || "rollback",
          extra: `revision ${r.revision_id || ""}`,
          status: "bad",
        });
      }
      document.getElementById("collaborationPromotion").innerHTML = items.map((item) => `
        <div class="item">
          <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;">
            <strong>${escapeHtml(item.title)}</strong>
            <span class="pill ${pillClass(item.status)}">${escapeHtml(item.status)}</span>
          </div>
          <p class="subtle" style="margin-top:8px;">${escapeHtml(item.note)}</p>
          ${item.extra ? `<p class="subtle" style="margin-top:6px;">${escapeHtml(item.extra)}</p>` : ""}
        </div>
      `).join("") || `<div class="item"><strong>No promotion rows yet</strong><p class="subtle" style="margin-top:8px;">Ledger-driven candidates and revisions will appear once collaboration outcomes accumulate.</p></div>`;
    }

    function renderTasks(data) {
      const rows = data.tasks.items || [];
      const header = `
        <table>
          <thead>
            <tr>
              <th>Task</th>
              <th>Status</th>
              <th>Channel</th>
              <th>Lane</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map((task) => {
              const lane = task.delegated_to_cursor
                ? "OpenClaw -> Cursor"
                : (task.provider || task.preferred_model_label || task.collaboration_mode || "direct");
              const summary = task.summary || task.last_error || "";
              const cls = task.task_id === selectedTaskId ? "taskRow selected" : "taskRow";
              return `
                <tr class="${cls}" data-task-id="${escapeHtml(task.task_id)}">
                  <td><strong>${escapeHtml(task.task_id)}</strong><br><span class="subtle">${escapeHtml(summary.slice(0, 100) || "No summary yet")}</span></td>
                  <td><span class="pill ${pillClass(task.status)}">${escapeHtml(task.status)}</span></td>
                  <td>${escapeHtml(task.channel)}</td>
                  <td>${escapeHtml(lane)}</td>
                  <td>${escapeHtml(formatTs(task.updated_at))}</td>
                </tr>
              `;
            }).join("")}
          </tbody>
        </table>`;
      document.getElementById("tasks").innerHTML = rows.length ? header : `<div class="item"><strong>No tasks yet</strong><p class="subtle" style="margin-top:8px;">Once Telegram, Alexa, or CLI tasks land, they will appear here.</p></div>`;
      document.querySelectorAll("tr.taskRow").forEach((row) => {
        row.addEventListener("click", () => {
          const taskId = row.getAttribute("data-task-id") || "";
          if (taskId) {
            selectedTaskId = taskId;
            renderTasks(latestSummary);
            loadTask(taskId).catch(showError);
          }
        });
      });
    }

    function renderTaskMeta(task) {
      const meta = [
        ["Task", task.task_id],
        ["Status", task.status],
        ["Channel", task.channel],
        ["Summary", task.summary || "n/a"],
        ["Last error", task.last_error || "n/a"],
        ["Cursor agent", task.cursor_agent_id || (((task.meta || {}).cursor || {}).agent_url || "n/a")],
        ["Provider/model", `${(((task.meta || {}).openclaw || {}).provider || "")} ${(((task.meta || {}).openclaw || {}).model || "")}`.trim() || "n/a"],
        ["Preferred lane", (((task.meta || {}).execution || {}).preferred_model_label || ((task.meta || {}).telegram || {}).preferred_model_label || "n/a")],
        ["Collaboration", (((task.meta || {}).execution || {}).collaboration_mode || ((task.meta || {}).telegram || {}).collaboration_mode || "n/a")]
      ];
      document.getElementById("taskMeta").innerHTML = meta.map(([label, value]) => `
        <div class="item">
          <div class="label">${escapeHtml(label)}</div>
          <div style="margin-top:6px;">${escapeHtml(value)}</div>
        </div>
      `).join("");
      document.getElementById("detailSummary").textContent = `Task ${task.task_id} projected state and collaboration metadata.`;
    }

    function renderTimeline(events) {
      document.getElementById("timeline").innerHTML = (events || []).map((event) => `
        <div class="event">
          <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;">
            <strong>${escapeHtml(event.event_type)}</strong>
            <span class="subtle">${escapeHtml(formatTs(event.ts))}</span>
          </div>
          <div class="subtle" style="margin-top:4px;">seq ${escapeHtml(event.seq)}</div>
          <pre>${escapeHtml(JSON.stringify(event.payload || {}, null, 2))}</pre>
        </div>
      `).join("") || `<div class="item"><strong>No events</strong><p class="subtle" style="margin-top:8px;">This task has no stored events yet.</p></div>`;
    }

    async function loadTask(taskId) {
      const data = await fetchJson(`/v1/tasks/${encodeURIComponent(taskId)}`);
      renderTaskMeta(data.task || {});
      renderTimeline(data.events || []);
    }

    async function loadSummary() {
      latestSummary = await fetchJson("/v1/dashboard/summary?limit=30");
      renderCards(latestSummary);
      renderAttention(latestSummary);
      renderOptimization(latestSummary);
      renderExperience(latestSummary);
      renderCollaborationPromotion(latestSummary);
      renderDailyAssistantPack(latestSummary);
      renderTasks(latestSummary);
      document.getElementById("lastUpdated").textContent = `Last updated ${new Date().toLocaleTimeString()} (auto-refresh every 5s)`;
      const tasks = latestSummary.tasks.items || [];
      if (!tasks.length) {
        selectedTaskId = "";
      }
      if (!selectedTaskId && tasks.length) {
        selectedTaskId = tasks[0].task_id;
      }
      if (selectedTaskId) {
        const stillVisible = tasks.some((task) => task.task_id === selectedTaskId);
        if (!stillVisible && tasks.length) {
          selectedTaskId = tasks[0].task_id;
        }
        if (selectedTaskId) {
          renderTasks(latestSummary);
          await loadTask(selectedTaskId);
        }
      }
    }

    function showError(err) {
      document.getElementById("lastUpdated").textContent = `Dashboard error: ${err}`;
    }

    document.getElementById("refreshBtn").addEventListener("click", () => loadSummary().catch(showError));
    loadSummary().catch(showError);
    setInterval(() => loadSummary().catch(showError), 5000);
  </script>
</body>
</html>
"""

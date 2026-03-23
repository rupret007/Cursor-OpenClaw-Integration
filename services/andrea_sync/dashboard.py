"""Operator dashboard helpers for Andrea lockstep."""
from __future__ import annotations

import time
import urllib.parse
from typing import Any, Dict, List

from .adapters import telegram as tg_adapt
from .kill_switch import kill_switch_status
from .policy import digest_age_seconds, get_capability_digest
from .projector import project_task_dict
from .schema import EventType
from .store import (
    SYSTEM_TASK_ID,
    count_active_memories,
    count_due_reminders,
    count_pending_reminders,
    count_principals,
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
    }
    if not server.telegram_bot_token:
        return {
            **base,
            "configured": False,
            "status": "unconfigured",
            "reason": "TELEGRAM_BOT_TOKEN missing",
        }
    if not server.telegram_public_base:
        return {
            **base,
            "configured": False,
            "status": "missing_public_base",
            "reason": "ANDREA_SYNC_PUBLIC_BASE missing",
        }
    try:
        info = tg_adapt.get_webhook_info(server.telegram_bot_token)
    except Exception as exc:  # noqa: BLE001
        return {
            **base,
            "configured": True,
            "status": "error",
            "reason": str(exc),
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
        "current_url": _redact_webhook_url(current_url),
        "pending_update_count": int(result.get("pending_update_count") or 0),
        "last_error_date": result.get("last_error_date"),
        "last_error_message": result.get("last_error_message"),
        "max_connections": result.get("max_connections"),
        "ip_address": result.get("ip_address"),
    }


def _task_list_item(row: Dict[str, Any], proj: Dict[str, Any]) -> Dict[str, Any]:
    meta = proj.get("meta") if isinstance(proj.get("meta"), dict) else {}
    telegram = meta.get("telegram") if isinstance(meta.get("telegram"), dict) else {}
    execution = meta.get("execution") if isinstance(meta.get("execution"), dict) else {}
    openclaw = meta.get("openclaw") if isinstance(meta.get("openclaw"), dict) else {}
    cursor = meta.get("cursor") if isinstance(meta.get("cursor"), dict) else {}
    outcome = meta.get("outcome") if isinstance(meta.get("outcome"), dict) else {}
    identity = meta.get("identity") if isinstance(meta.get("identity"), dict) else {}
    return {
        "task_id": proj.get("task_id") or row.get("task_id"),
        "channel": proj.get("channel") or row.get("channel") or "",
        "status": proj.get("status") or "created",
        "summary": proj.get("summary") or "",
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
    }


def _build_memory_summary(conn: Any) -> Dict[str, Any]:
    return {
        "principal_count": count_principals(conn),
        "active_memory_count": count_active_memories(conn),
        "pending_reminder_count": count_pending_reminders(conn),
        "due_reminder_count": count_due_reminders(conn),
    }


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
        },
        "webhook": webhook_snapshot if isinstance(webhook_snapshot, dict) else {},
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
      const cards = [
        { label: "Kill Switch", value: data.service.kill_switch.engaged ? "ENGAGED" : "Released", status: data.service.kill_switch.engaged ? "blocked" : "ready", note: "Server safety state" },
        { label: "Webhook", value: data.webhook.status, status: data.webhook.status, note: data.webhook.reason || "Telegram webhook state" },
        { label: "Recent Tasks", value: String(data.tasks.count), status: "ready", note: `Limit ${data.tasks.limit}` },
        { label: "Blocked Caps", value: String((data.capabilities.summary || {}).blocked || 0), status: ((data.capabilities.summary || {}).blocked || 0) > 0 ? "blocked" : "ready", note: "Published capability digest" },
        { label: "ACPX", value: data.capabilities.acpx ? data.capabilities.acpx.status : "digest-missing", status: data.capabilities.acpx ? data.capabilities.acpx.status : "blocked", note: data.capabilities.acpx ? data.capabilities.acpx.notes : "No published acpx row is available yet" },
        { label: "Digest Age", value: `${Math.round(Number(data.service.capability_digest_age_seconds || 0))}s`, status: Number(data.service.capability_digest_age_seconds || 0) > 1800 ? "warn" : "ready", note: "Capability snapshot freshness" },
        { label: "Optimizer", value: latestRun.status || "idle", status: latestRun.status || "warn", note: latestRun.run_id ? `Latest run ${latestRun.run_id}` : "No optimization run recorded yet" }
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
      items.push({
        title: `Webhook: ${data.webhook.status}`,
        note: data.webhook.reason || "No webhook note",
        extra: data.webhook.current_url ? `Current: ${data.webhook.current_url}` : (data.webhook.expected_url ? `Expected: ${data.webhook.expected_url}` : ""),
        status: data.webhook.status
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

      const loopItems = [];
      if (latest.run_id) {
        loopItems.push({
          title: `Latest run: ${latest.run_id}`,
          note: `Status ${latest.status || "unknown"}${latest.analysis_mode ? ` · ${latest.analysis_mode}` : ""}`,
          extra: `Findings ${latest.finding_count || 0} · Proposals ${latest.proposal_count || 0} · Gate ${latest.gate_allowed === true ? "open" : latest.gate_allowed === false ? "gated" : "n/a"}`,
          status: latest.status || "warn"
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

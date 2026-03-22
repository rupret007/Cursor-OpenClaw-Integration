"""Operator dashboard helpers for Andrea lockstep."""
from __future__ import annotations

import time
import urllib.parse
from typing import Any, Dict, List

from .adapters import telegram as tg_adapt
from .kill_switch import kill_switch_status
from .policy import digest_age_seconds, get_capability_digest
from .projector import project_task_dict
from .store import list_tasks


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


def _webhook_snapshot(server: Any) -> Dict[str, Any]:
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
        "preferred_model_label": execution.get("preferred_model_label")
        or telegram.get("preferred_model_label")
        or "",
        "provider": openclaw.get("provider") or "",
        "model": openclaw.get("model") or "",
        "delegated_to_cursor": bool(execution.get("delegated_to_cursor")),
        "agent_url": cursor.get("agent_url") or "",
        "pr_url": cursor.get("pr_url") or "",
        "openclaw_session_id": openclaw.get("session_id") or "",
    }


def build_dashboard_summary(conn: Any, server: Any, *, limit: int = 30) -> Dict[str, Any]:
    rows = list_tasks(conn, limit=limit)
    items: List[Dict[str, Any]] = []
    by_status: Dict[str, int] = {}
    by_channel: Dict[str, int] = {}
    for row in rows:
        task_id = str(row.get("task_id") or "")
        channel = str(row.get("channel") or "")
        if not task_id or not channel:
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
    blocked_critical = [
        row
        for row in cap_rows
        if isinstance(row, dict) and row.get("critical") and row.get("status") == "blocked"
    ]
    attention = [
        row for row in cap_rows if isinstance(row, dict) and row.get("status") != "ready"
    ]
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
        "webhook": _webhook_snapshot(server),
        "capabilities": {
            "summary": digest.get("summary") if isinstance(digest.get("summary"), dict) else {},
            "blocked_critical": blocked_critical[:10],
            "attention": attention[:12],
            "acpx": acpx_row,
        },
        "tasks": {
            "limit": limit,
            "count": len(items),
            "by_status": by_status,
            "by_channel": by_channel,
            "items": items,
        },
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
      const cards = [
        { label: "Kill Switch", value: data.service.kill_switch.engaged ? "ENGAGED" : "Released", status: data.service.kill_switch.engaged ? "blocked" : "ready", note: "Server safety state" },
        { label: "Webhook", value: data.webhook.status, status: data.webhook.status, note: data.webhook.reason || "Telegram webhook state" },
        { label: "Recent Tasks", value: String(data.tasks.count), status: "ready", note: `Limit ${data.tasks.limit}` },
        { label: "Blocked Caps", value: String((data.capabilities.summary || {}).blocked || 0), status: ((data.capabilities.summary || {}).blocked || 0) > 0 ? "blocked" : "ready", note: "Published capability digest" },
        { label: "ACPX", value: data.capabilities.acpx ? data.capabilities.acpx.status : "unknown", status: data.capabilities.acpx ? data.capabilities.acpx.status : "ready_with_limits", note: data.capabilities.acpx ? data.capabilities.acpx.notes : "No published acpx row yet" },
        { label: "Digest Age", value: `${Math.round(Number(data.service.capability_digest_age_seconds || 0))}s`, status: Number(data.service.capability_digest_age_seconds || 0) > 1800 ? "warn" : "ready", note: "Capability snapshot freshness" }
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
      renderTasks(latestSummary);
      document.getElementById("lastUpdated").textContent = `Last updated ${new Date().toLocaleTimeString()} (auto-refresh every 5s)`;
      const tasks = latestSummary.tasks.items || [];
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

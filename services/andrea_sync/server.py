"""HTTP server for Andrea lockstep (local-first)."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, Dict, Optional

from .adapters import alexa as alexa_adapt
from .adapters import telegram as tg_adapt
from .alexa_request_verify import verify_alexa_http_request
from .andrea_router import AndreaRouteDecision, route_message
from .bus import handle_command
from .dashboard import (
    build_dashboard_summary,
    build_runtime_truth_snapshot,
    build_dashboard_webhook_snapshot,
    render_dashboard_html,
)
from .kill_switch import is_kill_switch_engaged, kill_switch_status
from .observability import metric_log, structured_log
from .policy import (
    digest_age_seconds,
    evaluate_skill_absence_claim,
    get_capability_digest,
    resolve_skill_truth,
)
from .projector import project_task_dict
from .schema import EventType
from .telegram_continuation import attach_continuation_if_applicable
from .store import (
    append_event,
    count_active_memories,
    count_due_reminders,
    count_pending_reminders,
    connect,
    delete_meta,
    ensure_system_task,
    get_meta,
    get_principal_preferences,
    get_task_principal_id,
    get_task_channel,
    list_tasks,
    list_principal_memories,
    load_recent_principal_history,
    load_events_for_task,
    load_recent_telegram_history,
    migrate,
    set_meta,
)
from .telegram_format import (
    format_ack_message,
    format_alexa_session_summary,
    format_continuation_notice,
    format_direct_message,
    format_final_message,
    format_late_chunk_notice,
    format_progress_message,
    format_running_message,
)
from .user_surface import (
    dedupe_user_surface_items,
    sanitize_user_surface_text as shared_sanitize_user_surface_text,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_db_path() -> Path:
    raw = os.environ.get("ANDREA_SYNC_DB")
    if raw:
        return Path(raw).expanduser()
    return _repo_root() / "data" / "andrea_sync.db"


TERMINAL_CURSOR_STATUSES = frozenset({"FINISHED", "FAILED", "CANCELLED", "STOPPED", "EXPIRED"})


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return default


def _clip(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _sanitize_user_surface_text(text: Any, *, fallback: str = "") -> str:
    sanitized = shared_sanitize_user_surface_text(text, fallback=fallback, limit=500)
    return sanitized or "I ran into an internal limitation while working on that."


def _is_generic_openclaw_summary(text: Any) -> bool:
    return str(text or "").strip() in {
        "",
        "OpenClaw completed the delegated task.",
    }


REMIND_ME_RE = re.compile(r"^\s*(?:please\s+)?remind me(?:\s+to)?\s+(?P<body>.+?)\s*$", re.I)
REMEMBER_NOTE_RE = re.compile(
    r"^\s*(?:please\s+)?remember(?:\s+that|\s+this)?\s+(?P<body>.+?)\s*$",
    re.I,
)
RELATIVE_REMINDER_RE = re.compile(
    r"\bin\s+(?P<count>\d+)\s+(?P<unit>minute|minutes|hour|hours|day|days)\b",
    re.I,
)
MESSAGING_CAPABILITY_RE = re.compile(
    r"\b(can you|could you|are you able to|do you(?: have)?|you can|able to)\b.*\b("
    r"text|message|messages|imessage|imessages|blue bubbles|bluebubbles|whatsapp"
    r")\b",
    re.I,
)
LIVE_NEWS_RE = re.compile(r"\b(news|headline|headlines)\b", re.I)
RECENT_TEXT_MESSAGES_RE = re.compile(
    r"(?:"
    r"\b(?:recent|latest|last)\b.*\b(?:text(?:s| messages?)?|imessages?|messages?|threads?)\b|"
    r"\b(?:text(?:s| messages?)?|imessages?|threads?)\b.*\b(?:recent|latest|last)\b"
    r")",
    re.I,
)
OUTBOUND_SEND_PATTERNS = (
    re.compile(
        r"^\s*(?:(?:please|can you|could you)\s+)?send\s+(?:a\s+)?(?:message|text)\s+to\s+(?P<target>.+?)(?:\s+(?:that|saying|saying that)\s+(?P<body>.+))?\s*$",
        re.I,
    ),
    re.compile(
        r"^\s*(?:(?:please|can you|could you)\s+)?send\s+(?P<target>.+?)\s+(?:a\s+)?(?:message|text)(?:\s+(?:that|saying|saying that)\s+(?P<body>.+))?\s*$",
        re.I,
    ),
    re.compile(
        r"^\s*(?:(?:please|can you|could you)\s+)?text\s+(?P<target>.+?)(?:\s+(?P<body>.+))?\s*$",
        re.I,
    ),
    re.compile(
        r"^\s*(?:(?:please|can you|could you)\s+)?tell\s+(?P<target>.+?)\s+(?P<body>.+)\s*$",
        re.I,
    ),
)
OUTBOUND_CONFIRM_RE = re.compile(
    r"^\s*(yes|y|send it|send it now|yes send it|ok send it|okay send it|go ahead|do it|confirm|looks good)\s*[.!]?\s*$",
    re.I,
)
OUTBOUND_CANCEL_RE = re.compile(
    r"^\s*(no|cancel|don't send|do not send|stop|never mind)\s*[.!]?\s*$",
    re.I,
)
PHONE_TARGET_RE = re.compile(r"^\+?[\d()\-\s.]{7,}$")
AMBIGUOUS_TARGETS = {"her", "him", "them", "someone", "somebody", "that person"}
PENDING_OUTBOUND_DRAFT_TTL_SECONDS = 1800.0
USER_SURFACE_INTERNAL_RE = re.compile(
    r"\b("
    r"sessionkey|session key|session id|session label|runtime id|"
    r"attachments\.enabled|sessions_send|sessions_spawn|"
    r"openclaw skills install|openclaw skills update|skills info|"
    r"gateway restart|blockedbyallowlist|"
    r"missing_(?:bins|env|config|os)|"
    r"eligible(?::|=)\s*(?:true|false)|"
    r"--session-id"
    r")\b|(?:plugins\.entries|channels\.)[\w.-]+",
    re.I,
)
MESSAGING_SKILL_CANDIDATES = (
    {
        "skill_key": "bluebubbles",
        "label": "text messaging",
        "match_terms": ("blue bubbles", "bluebubbles", "imessage", "imessages", "text", "message"),
    },
    {
        "skill_key": "wacli",
        "label": "WhatsApp messaging",
        "match_terms": ("whatsapp",),
    },
)


def _format_due_time_local(due_at: float) -> str:
    tz = dt.datetime.now().astimezone().tzinfo
    when = dt.datetime.fromtimestamp(float(due_at), tz=tz)
    now = dt.datetime.now(tz)
    if when.date() == now.date():
        return when.strftime("today at %-I:%M %p")
    if when.date() == (now + dt.timedelta(days=1)).date():
        return when.strftime("tomorrow at %-I:%M %p")
    return when.strftime("%b %-d at %-I:%M %p")


class SyncServer:
    def __init__(self) -> None:
        self.repo_root = _repo_root()
        self.db_path = default_db_path()
        self.conn = connect(self.db_path)
        migrate(self.conn)
        ensure_system_task(self.conn)
        self.lock = threading.Lock()
        self._routing_inflight: set[str] = set()
        self._notification_inflight: set[str] = set()
        self.queue: Queue[Callable[[], None]] = Queue()
        self._worker = threading.Thread(target=self._run_queue, daemon=True)
        self._worker.start()
        self.telegram_secret = os.environ.get("ANDREA_SYNC_TELEGRAM_SECRET", "")
        self.telegram_header_secret = os.environ.get(
            "ANDREA_SYNC_TELEGRAM_WEBHOOK_SECRET", ""
        )
        self.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_public_base = (
            os.environ.get("ANDREA_SYNC_PUBLIC_BASE", "").strip().rstrip("/")
        )
        self.telegram_use_query_secret = _env_bool(
            "ANDREA_SYNC_TELEGRAM_URL_QUERY", True
        )
        self.telegram_webhook_autofix = _env_bool(
            "ANDREA_SYNC_TELEGRAM_WEBHOOK_AUTOFIX", True
        )
        self.telegram_webhook_autofix_interval = _env_float(
            "ANDREA_SYNC_TELEGRAM_WEBHOOK_AUTOFIX_INTERVAL_SECONDS", 10.0
        )
        self.internal_token = os.environ.get("ANDREA_SYNC_INTERNAL_TOKEN", "")
        self.alexa_edge_token = os.environ.get("ANDREA_SYNC_ALEXA_EDGE_TOKEN", "").strip()
        self.alexa_skill_id = os.environ.get("ANDREA_ALEXA_SKILL_ID", "").strip()
        self.executor_started_ttl_seconds = _env_int(
            "ANDREA_SYNC_EXECUTOR_STARTED_TTL_SECONDS", 0
        )
        self.background_enabled = _env_bool("ANDREA_SYNC_BACKGROUND_ENABLED", True)
        self.delegated_execution_enabled = _env_bool(
            "ANDREA_SYNC_DELEGATED_EXECUTION_ENABLED", True
        )
        self.telegram_auto_cursor = _env_bool("ANDREA_SYNC_TELEGRAM_AUTO_CURSOR", True)
        self.telegram_notifier_enabled = _env_bool("ANDREA_SYNC_TELEGRAM_NOTIFIER", True)
        self.telegram_quiet_lifecycle = _env_bool("ANDREA_SYNC_TELEGRAM_QUIET_LIFECYCLE", True)
        self.alexa_summary_to_telegram = _env_bool("ANDREA_SYNC_ALEXA_SUMMARY_TO_TELEGRAM", True)
        self.alexa_summary_chat_id = (
            os.environ.get("ANDREA_SYNC_ALEXA_SUMMARY_CHAT_ID", "").strip()
            or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        )
        _lane = (
            os.environ.get("ANDREA_TELEGRAM_DELEGATE_LANE", "openclaw_hybrid").strip().lower()
            or "openclaw_hybrid"
        )
        # Cursor-first Telegram lane is deprecated; hybrid orchestrates visible OpenClaw then escalates.
        if _lane in {"cursor", "cursor_direct", "direct_cursor"}:
            _lane = "openclaw_hybrid"
        self.telegram_delegate_lane = _lane
        self.cursor_repo_path = Path(
            os.environ.get("ANDREA_CURSOR_REPO", str(self.repo_root))
        ).expanduser()
        self.cursor_mode = os.environ.get("ANDREA_CURSOR_HANDOFF_MODE", "auto").strip() or "auto"
        self.cursor_read_only = _env_bool("ANDREA_CURSOR_READ_ONLY", False)
        self.cursor_status_poll_attempts = _env_int(
            "ANDREA_CURSOR_STATUS_POLL_ATTEMPTS", 120
        )
        self.cursor_status_poll_interval = _env_float(
            "ANDREA_CURSOR_STATUS_POLL_INTERVAL_SECONDS", 5.0
        )
        self.cursor_create_timeout_seconds = _env_int(
            "ANDREA_CURSOR_CREATE_TIMEOUT_SECONDS", 120
        )
        self.openclaw_agent_id = os.environ.get("ANDREA_OPENCLAW_AGENT_ID", "main").strip() or "main"
        self.openclaw_timeout_seconds = _env_int(
            "ANDREA_OPENCLAW_TIMEOUT_SECONDS", 900
        )
        self.openclaw_fallback_to_cursor = _env_bool(
            "ANDREA_OPENCLAW_FALLBACK_TO_CURSOR", True
        )
        self.openclaw_thinking = os.environ.get("ANDREA_OPENCLAW_THINKING", "medium").strip() or "medium"
        self.openclaw_refresh_mode = (
            os.environ.get("ANDREA_OPENCLAW_REFRESH_MODE", "auto").strip().lower()
            or "auto"
        )
        self.openclaw_gateway_restart_timeout_seconds = _env_int(
            "ANDREA_OPENCLAW_GATEWAY_RESTART_TIMEOUT_SECONDS", 90
        )
        self.background_optimizer_enabled = _env_bool(
            "ANDREA_SYNC_BACKGROUND_OPTIMIZER_ENABLED", False
        )
        self.background_optimizer_interval_seconds = _env_float(
            "ANDREA_SYNC_BACKGROUND_OPTIMIZER_INTERVAL_SECONDS", 900.0
        )
        self.background_optimizer_idle_seconds = _env_float(
            "ANDREA_SYNC_BACKGROUND_OPTIMIZER_IDLE_SECONDS", 120.0
        )
        self.background_optimizer_auto_apply = _env_bool(
            "ANDREA_SYNC_BACKGROUND_OPTIMIZER_AUTO_APPLY", False
        )
        self.background_incident_repair_enabled = _env_bool(
            "ANDREA_SYNC_BACKGROUND_INCIDENT_REPAIR_ENABLED", False
        )
        self.background_incident_cursor_execute = _env_bool(
            "ANDREA_SYNC_BACKGROUND_INCIDENT_CURSOR_EXECUTE", False
        )
        self.proactive_sweep_enabled = _env_bool(
            "ANDREA_SYNC_PROACTIVE_SWEEP_ENABLED", False
        )
        self.proactive_sweep_interval_seconds = _env_float(
            "ANDREA_SYNC_PROACTIVE_SWEEP_INTERVAL_SECONDS", 60.0
        )
        if self.telegram_webhook_autofix and self.telegram_bot_token and self.telegram_public_base:
            self._webhook_worker = threading.Thread(
                target=self._maintain_telegram_webhook,
                daemon=True,
            )
            self._webhook_worker.start()
        if self.proactive_sweep_enabled:
            self._proactive_worker = threading.Thread(
                target=self._maintain_proactive_sweep,
                daemon=True,
            )
            self._proactive_worker.start()
        if self.background_optimizer_enabled:
            self._background_optimizer_worker = threading.Thread(
                target=self._maintain_background_optimizer,
                daemon=True,
            )
            self._background_optimizer_worker.start()

    def _run_queue(self) -> None:
        while True:
            try:
                fn = self.queue.get(timeout=0.5)
            except Empty:
                continue
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                print(f"andrea_sync worker error: {e}", flush=True)
            finally:
                self.queue.task_done()

    def _expected_webhook_url(self) -> str:
        return tg_adapt.build_webhook_url(
            self.telegram_public_base,
            self.telegram_secret,
            use_query=self.telegram_use_query_secret,
        )

    def _maintain_telegram_webhook(self) -> None:
        while True:
            try:
                info = tg_adapt.get_webhook_info(self.telegram_bot_token)
                result = info.get("result") if isinstance(info.get("result"), dict) else {}
                current_url = str(result.get("url") or "").strip()
                expected_url = self._expected_webhook_url()
                if not tg_adapt.webhook_urls_match(current_url, expected_url):
                    if not current_url:
                        print(
                            "andrea_sync webhook missing in Telegram; reapplying expected registration",
                            flush=True,
                        )
                    else:
                        print(
                            "andrea_sync webhook drift detected; reapplying expected registration",
                            flush=True,
                        )
                    res = tg_adapt.set_webhook(
                        bot_token=self.telegram_bot_token,
                        public_base=self.telegram_public_base,
                        query_secret=self.telegram_secret,
                        header_secret=self.telegram_header_secret,
                        use_query_secret=self.telegram_use_query_secret,
                    )
                    if not res.get("ok", False):
                        print(f"andrea_sync webhook autofix rejected: {res}", flush=True)
                    else:
                        print(
                            f"andrea_sync webhook autofix applied: {self.telegram_public_base}",
                            flush=True,
                        )
            except Exception as exc:  # noqa: BLE001
                print(f"andrea_sync webhook autofix error: {exc}", flush=True)
            time.sleep(max(5.0, self.telegram_webhook_autofix_interval))

    def _maintain_proactive_sweep(self) -> None:
        while True:
            try:
                self.with_lock(
                    lambda c: handle_command(
                        c,
                        {
                            "command_type": "RunProactiveSweep",
                            "channel": "internal",
                            "payload": {
                                "limit": 20,
                            },
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001
                print(f"andrea_sync proactive sweep error: {exc}", flush=True)
            time.sleep(max(10.0, self.proactive_sweep_interval_seconds))

    def _maintain_background_optimizer(self) -> None:
        while True:
            conn = connect(self.db_path)
            try:
                migrate(conn)
                from .optimizer import run_optimization_cycle
                from .repair_orchestrator import run_incident_repair_cycle

                result = run_optimization_cycle(
                    conn,
                    limit=60,
                    regression_report={
                        "passed": True,
                        "total": 1,
                        "command": "background_idle_scheduler",
                    },
                    required_skills=["cursor_handoff"],
                    emit_proposals=True,
                    actor="background",
                    analysis_mode="gemini_background",
                    repo_path=self.cursor_repo_path,
                    auto_apply_ready=self.background_optimizer_auto_apply,
                    idle_seconds=self.background_optimizer_idle_seconds,
                )
                if self.background_incident_repair_enabled and not bool(result.get("skipped")):
                    run_incident_repair_cycle(
                        conn,
                        repo_path=self.cursor_repo_path,
                        actor="background",
                        cursor_execute=self.background_incident_cursor_execute,
                        write_report=True,
                    )
            except Exception as exc:  # noqa: BLE001
                print(f"andrea_sync background optimizer error: {exc}", flush=True)
            finally:
                conn.close()
            time.sleep(max(60.0, self.background_optimizer_interval_seconds))

    def with_lock(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        with self.lock:
            return fn(self.conn)

    def _meta_key(self, prefix: str, task_id: str) -> str:
        return f"andrea_bridge:{prefix}:{task_id}"

    def _claim_routing_attempt(self, task_id: str) -> bool:
        marker = self._meta_key("route_applied", task_id)

        def claim(c: sqlite3.Connection) -> bool:
            if task_id in self._routing_inflight:
                return False
            if get_meta(c, marker) is not None:
                return False
            self._routing_inflight.add(task_id)
            return True

        return self.with_lock(claim)

    def _finish_routing_attempt(self, task_id: str) -> None:
        def release(_c: sqlite3.Connection) -> None:
            self._routing_inflight.discard(task_id)

        self.with_lock(release)

    def _claim_notification_attempt(self, marker: str) -> bool:
        def claim(c: sqlite3.Connection) -> bool:
            if marker in self._notification_inflight:
                return False
            if get_meta(c, marker) is not None:
                return False
            self._notification_inflight.add(marker)
            return True

        return self.with_lock(claim)

    def _finish_notification_attempt(self, marker: str) -> None:
        def release(_c: sqlite3.Connection) -> None:
            self._notification_inflight.discard(marker)

        self.with_lock(release)

    def _task_snapshot(self, task_id: str) -> Optional[Dict[str, Any]]:
        def read(c: sqlite3.Connection) -> Optional[Dict[str, Any]]:
            channel = get_task_channel(c, task_id)
            if not channel:
                return None
            return {
                "channel": channel,
                "projection": project_task_dict(c, task_id, channel),
                "events": load_events_for_task(c, task_id),
            }

        return self.with_lock(read)

    def _send_telegram_message_once(self, task_id: str, phase: str, text: str) -> None:
        if not self.telegram_notifier_enabled or not self.telegram_bot_token:
            return
        snapshot = self._task_snapshot(task_id)
        if not snapshot or snapshot["channel"] != "telegram":
            return
        telegram_meta = (
            snapshot["projection"].get("meta", {}).get("telegram", {})
            if isinstance(snapshot["projection"].get("meta"), dict)
            else {}
        )
        chat_id = telegram_meta.get("chat_id")
        if chat_id is None:
            return
        marker = self._meta_key(f"telegram_sent_{phase}", task_id)
        if not self._claim_notification_attempt(marker):
            return
        try:
            reply_anchor = telegram_meta.get("first_user_message_id")
            if reply_anchor is None:
                reply_anchor = telegram_meta.get("message_id")
            tg_adapt.send_text_message(
                bot_token=self.telegram_bot_token,
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_anchor,
                message_thread_id=telegram_meta.get("message_thread_id"),
            )

            def mark(c: sqlite3.Connection) -> None:
                set_meta(c, marker, str(time.time()))

            self.with_lock(mark)
        finally:
            self._finish_notification_attempt(marker)

    def _send_telegram_chat_message_once(
        self,
        task_id: str,
        phase: str,
        *,
        chat_id: int | str | None,
        text: str,
    ) -> None:
        if (
            not self.telegram_notifier_enabled
            or not self.telegram_bot_token
            or chat_id in (None, "")
            or not str(text or "").strip()
        ):
            return
        marker = self._meta_key(f"telegram_sent_{phase}", task_id)
        if not self._claim_notification_attempt(marker):
            return
        try:
            tg_adapt.send_text_message(
                bot_token=self.telegram_bot_token,
                chat_id=chat_id,
                text=text,
            )

            def mark(c: sqlite3.Connection) -> None:
                set_meta(c, marker, str(time.time()))

            self.with_lock(mark)
        finally:
            self._finish_notification_attempt(marker)

    def _recent_telegram_history(self, task_id: str) -> list[dict[str, str]]:
        snapshot = self._task_snapshot(task_id)
        if not snapshot or snapshot["channel"] != "telegram":
            return []
        projection_meta = snapshot["projection"].get("meta", {})
        telegram_meta = (
            projection_meta.get("telegram", {})
            if isinstance(projection_meta, dict)
            else {}
        )
        chat_id = telegram_meta.get("chat_id")
        if chat_id is None:
            return []

        def read(c: sqlite3.Connection) -> list[dict[str, str]]:
            return load_recent_telegram_history(
                c,
                chat_id,
                limit_turns=_env_int("ANDREA_DIRECT_HISTORY_TURNS", 6),
                exclude_task_id=task_id,
            )

        return self.with_lock(read)

    def _task_principal_id(self, task_id: str) -> str:
        snapshot = self._task_snapshot(task_id)
        if snapshot:
            projection_meta = snapshot["projection"].get("meta", {})
            if isinstance(projection_meta, dict):
                identity_meta = projection_meta.get("identity")
                if isinstance(identity_meta, dict) and identity_meta.get("principal_id"):
                    return str(identity_meta.get("principal_id")).strip()

        def read(c: sqlite3.Connection) -> str:
            return str(get_task_principal_id(c, task_id) or "").strip()

        return self.with_lock(read)

    def _principal_preferences(self, task_id: str) -> Dict[str, Any]:
        principal_id = self._task_principal_id(task_id)
        if not principal_id:
            return {}

        def read(c: sqlite3.Connection) -> Dict[str, Any]:
            return get_principal_preferences(c, principal_id)

        return self.with_lock(read)

    def _principal_memory_notes(self, task_id: str) -> list[str]:
        principal_id = self._task_principal_id(task_id)
        if not principal_id:
            return []

        def read(c: sqlite3.Connection) -> list[str]:
            rows = list_principal_memories(c, principal_id, limit=6)
            return [str(row.get("content") or "").strip() for row in rows if str(row.get("content") or "").strip()]

        return self.with_lock(read)

    def _recent_task_history(self, task_id: str) -> list[dict[str, str]]:
        principal_id = self._task_principal_id(task_id)
        if principal_id:
            def read(c: sqlite3.Connection) -> list[dict[str, str]]:
                return load_recent_principal_history(
                    c,
                    principal_id,
                    limit_turns=_env_int("ANDREA_DIRECT_HISTORY_TURNS", 6),
                    exclude_task_id=task_id,
                )

            history = self.with_lock(read)
            if history:
                return history
        return self._recent_telegram_history(task_id)

    def _projection_meta(self, projection: Dict[str, Any], key: str) -> Dict[str, Any]:
        meta = projection.get("meta", {})
        if not isinstance(meta, dict):
            return {}
        section = meta.get(key, {})
        return section if isinstance(section, dict) else {}

    def _telegram_send_lifecycle_messages(self, projection: Dict[str, Any]) -> bool:
        """Queued/running ack + started Telegrams: always on for full visibility; optional in summary mode."""
        execution_meta = self._projection_meta(projection, "execution")
        telegram_meta = self._projection_meta(projection, "telegram")
        visibility_mode = str(
            execution_meta.get("visibility_mode") or telegram_meta.get("visibility_mode") or "summary"
        ).strip() or "summary"
        if visibility_mode == "full":
            return True
        return not self.telegram_quiet_lifecycle

    def _task_execution_lane(self, task_id: str) -> str:
        snapshot = self._task_snapshot(task_id)
        if not snapshot:
            return self.telegram_delegate_lane
        projection = snapshot["projection"]
        execution_meta = self._projection_meta(projection, "execution")
        lane = str(execution_meta.get("lane") or "").strip()
        if lane:
            return lane
        cursor_meta = self._projection_meta(projection, "cursor")
        kind = str(cursor_meta.get("kind") or "").strip()
        if kind == "openclaw":
            return "openclaw_hybrid"
        if kind == "cursor":
            return "direct_cursor"
        return self.telegram_delegate_lane

    def _task_route_reason(self, task_id: str) -> str:
        snapshot = self._task_snapshot(task_id)
        if not snapshot:
            return ""
        execution_meta = self._projection_meta(snapshot["projection"], "execution")
        return str(execution_meta.get("route_reason") or "").strip()

    def _task_routing_hint(self, task_id: str) -> str:
        snapshot = self._task_snapshot(task_id)
        if not snapshot:
            return "auto"
        execution_meta = self._projection_meta(snapshot["projection"], "execution")
        if execution_meta.get("routing_hint"):
            return str(execution_meta.get("routing_hint")).strip()
        telegram_meta = self._projection_meta(snapshot["projection"], "telegram")
        if telegram_meta.get("routing_hint"):
            return str(telegram_meta.get("routing_hint")).strip()
        prefs = self._principal_preferences(task_id)
        return str(prefs.get("routing_hint") or "auto").strip() or "auto"

    def _task_collaboration_mode(self, task_id: str) -> str:
        snapshot = self._task_snapshot(task_id)
        if not snapshot:
            return "auto"
        execution_meta = self._projection_meta(snapshot["projection"], "execution")
        if execution_meta.get("collaboration_mode"):
            return str(execution_meta.get("collaboration_mode")).strip()
        telegram_meta = self._projection_meta(snapshot["projection"], "telegram")
        if telegram_meta.get("collaboration_mode"):
            return str(telegram_meta.get("collaboration_mode")).strip()
        prefs = self._principal_preferences(task_id)
        return str(prefs.get("collaboration_mode") or "auto").strip() or "auto"

    def _task_visibility_mode(self, task_id: str) -> str:
        """Visibility for lifecycle/Telegram updates. Prefer 'full' when either meta requests it,
        so newer Telegram intent (e.g. continuation adding 'show full dialogue') is not shadowed."""
        snapshot = self._task_snapshot(task_id)
        if not snapshot:
            return "summary"
        execution_meta = self._projection_meta(snapshot["projection"], "execution")
        telegram_meta = self._projection_meta(snapshot["projection"], "telegram")
        exec_mode = str(execution_meta.get("visibility_mode") or "").strip().lower()
        tg_mode = str(telegram_meta.get("visibility_mode") or "").strip().lower()
        if exec_mode == "full" or tg_mode == "full":
            return "full"
        prefs = self._principal_preferences(task_id)
        pref_mode = str(prefs.get("visibility_mode") or "").strip().lower()
        return exec_mode or tg_mode or pref_mode or "summary"

    def _task_preferred_model_family(self, task_id: str) -> str:
        snapshot = self._task_snapshot(task_id)
        if not snapshot:
            return ""
        execution_meta = self._projection_meta(snapshot["projection"], "execution")
        if execution_meta.get("preferred_model_family"):
            return str(execution_meta.get("preferred_model_family")).strip()
        telegram_meta = self._projection_meta(snapshot["projection"], "telegram")
        if telegram_meta.get("preferred_model_family"):
            return str(telegram_meta.get("preferred_model_family") or "").strip()
        prefs = self._principal_preferences(task_id)
        return str(prefs.get("preferred_model_family") or "").strip()

    def _task_preferred_model_label(self, task_id: str) -> str:
        snapshot = self._task_snapshot(task_id)
        if not snapshot:
            return ""
        execution_meta = self._projection_meta(snapshot["projection"], "execution")
        if execution_meta.get("preferred_model_label"):
            return str(execution_meta.get("preferred_model_label")).strip()
        telegram_meta = self._projection_meta(snapshot["projection"], "telegram")
        if telegram_meta.get("preferred_model_label"):
            return str(telegram_meta.get("preferred_model_label") or "").strip()
        prefs = self._principal_preferences(task_id)
        return str(prefs.get("preferred_model_label") or "").strip()

    def _task_mention_targets(self, task_id: str) -> list[str]:
        snapshot = self._task_snapshot(task_id)
        if not snapshot:
            return []
        execution_meta = self._projection_meta(snapshot["projection"], "execution")
        mention_targets = execution_meta.get("mention_targets")
        if isinstance(mention_targets, list):
            return [str(v) for v in mention_targets]
        telegram_meta = self._projection_meta(snapshot["projection"], "telegram")
        mention_targets = telegram_meta.get("mention_targets")
        if isinstance(mention_targets, list):
            return [str(v) for v in mention_targets]
        return []

    def _latest_user_message_payload(self, task_id: str) -> Dict[str, Any]:
        snapshot = self._task_snapshot(task_id)
        if not snapshot:
            return {}
        for _seq, _ts, et, payload in reversed(snapshot["events"]):
            if et == EventType.USER_MESSAGE.value and isinstance(payload, dict):
                return payload
        return {}

    def _telegram_worker_label(self, projection: Dict[str, Any], *, running: bool = False) -> str:
        execution_meta = self._projection_meta(projection, "execution")
        if execution_meta.get("runner") == "openclaw":
            if running and execution_meta.get("delegated_to_cursor"):
                return "OpenClaw and Cursor"
            return "OpenClaw"
        if execution_meta.get("delegated_to_cursor"):
            return "OpenClaw and Cursor"
        return "Cursor"

    def _telegram_collaboration_trace(self, projection: Dict[str, Any]) -> list[str]:
        openclaw_meta = self._projection_meta(projection, "openclaw")
        items: list[str] = []
        phase_outputs = openclaw_meta.get("phase_outputs")
        if isinstance(phase_outputs, dict):
            for phase in ("plan", "critique", "execution", "synthesis"):
                entry = phase_outputs.get(phase)
                if not isinstance(entry, dict):
                    continue
                summary = _sanitize_user_surface_text(entry.get("summary") or "")
                if summary:
                    items.append(summary)
        trace = openclaw_meta.get("collaboration_trace")
        if isinstance(trace, list):
            for raw in trace[:4]:
                text = _sanitize_user_surface_text(raw or "")
                if text and text not in items:
                    items.append(text)
        summary = self._telegram_final_summary_text(projection)
        return dedupe_user_surface_items(items, suppress_against=[summary], limit=4, item_limit=240)

    def _collaboration_trace_excerpt(self, trace: list[str]) -> str:
        cleaned = [str(item or "").strip() for item in trace if str(item or "").strip()]
        if not cleaned:
            return ""
        return _clip("; ".join(cleaned[:2]), 320)

    def _telegram_user_safe_error_text(self, projection: Dict[str, Any]) -> str:
        execution_meta = self._projection_meta(projection, "execution")
        openclaw_meta = self._projection_meta(projection, "openclaw")
        summary = str(projection.get("summary") or "").strip()
        return _sanitize_user_surface_text(
            str(execution_meta.get("user_safe_error") or "").strip()
            or str(openclaw_meta.get("blocked_reason") or "").strip()
            or summary
            or "I ran into an internal limitation while working on this request.",
            fallback="I ran into an internal limitation while working on this request.",
        )

    def _telegram_final_summary_text(self, projection: Dict[str, Any]) -> str:
        summary = str(projection.get("summary") or "").strip()
        openclaw_meta = self._projection_meta(projection, "openclaw")
        user_summary = str(openclaw_meta.get("user_summary") or "").strip()
        blocked_reason = str(openclaw_meta.get("blocked_reason") or "").strip()
        if user_summary and not _is_generic_openclaw_summary(user_summary):
            return _sanitize_user_surface_text(user_summary, fallback=summary or blocked_reason)
        if summary and not _is_generic_openclaw_summary(summary):
            return _sanitize_user_surface_text(summary, fallback=user_summary or blocked_reason)
        if blocked_reason:
            return _sanitize_user_surface_text(blocked_reason, fallback=summary or user_summary)
        return _sanitize_user_surface_text(user_summary or summary, fallback="I completed the request.")

    def _send_telegram_progress_updates(
        self,
        task_id: str,
        snapshot: Dict[str, Any],
    ) -> None:
        projection = snapshot["projection"]
        execution_meta = self._projection_meta(projection, "execution")
        telegram_meta = self._projection_meta(projection, "telegram")
        openclaw_meta = self._projection_meta(projection, "openclaw")
        visibility_mode = str(
            execution_meta.get("visibility_mode") or telegram_meta.get("visibility_mode") or "summary"
        ).strip() or "summary"
        for seq, _ts, event_type, payload in snapshot["events"]:
            if event_type != EventType.JOB_PROGRESS.value or not isinstance(payload, dict):
                continue
            progress_text = str(payload.get("message") or "").strip()
            force_telegram_note = bool(payload.get("force_telegram_note"))
            progress_text = _sanitize_user_surface_text(progress_text)
            if not progress_text:
                continue
            if visibility_mode != "full" and not force_telegram_note:
                continue
            try:
                self._send_telegram_message_once(
                    task_id,
                    f"progress_{seq}",
                    format_progress_message(
                        task_id,
                        progress_text=progress_text,
                        worker_label=self._telegram_worker_label(projection, running=True),
                        routing_hint="",
                        collaboration_mode="",
                        provider=str(payload.get("provider") or openclaw_meta.get("provider") or ""),
                        model=str(payload.get("model") or openclaw_meta.get("model") or ""),
                        preferred_model_label="",
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"andrea_sync telegram progress update error: {exc}", flush=True)

    def _queue_task_followups(self, task_id: str) -> None:
        if not task_id:
            return
        self.queue.put(lambda: self._handle_task_followups(task_id))

    def _handle_task_followups(self, task_id: str) -> None:
        snapshot = self._task_snapshot(task_id)
        if not snapshot:
            return
        if snapshot["channel"] == "telegram":
            self._handle_telegram_followups(task_id, snapshot)
            return
        if snapshot["channel"] == "alexa":
            self._handle_alexa_followups(task_id, snapshot)

    def _maybe_notify_telegram_continuation(
        self,
        task_id: str,
        snapshot: Dict[str, Any],
    ) -> None:
        if snapshot.get("channel") != "telegram":
            return
        events = snapshot.get("events") or []
        worker_label = self._telegram_worker_label(snapshot["projection"], running=True)
        for seq, _ts, et, payload in reversed(events):
            if et != EventType.USER_MESSAGE.value or not isinstance(payload, dict):
                continue
            if not payload.get("telegram_continuation"):
                continue
            preview = str(payload.get("routing_text") or payload.get("text") or "")
            mid = payload.get("message_id")
            phase = f"continuation_{mid}" if mid is not None else f"continuation_unknown_{seq}"
            try:
                self._send_telegram_message_once(
                    task_id,
                    phase,
                    format_continuation_notice(
                        task_id,
                        chunk_preview=preview,
                        worker_label=worker_label,
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"andrea_sync telegram continuation notice error: {exc}", flush=True)
            return

    def _maybe_notify_late_chunk_after_job_started(
        self,
        task_id: str,
        snapshot: Dict[str, Any],
    ) -> None:
        if snapshot.get("channel") != "telegram":
            return
        status = str(snapshot.get("projection", {}).get("status") or "")
        if status not in ("running", "awaiting_approval"):
            return
        events = snapshot.get("events") or []
        last_job_started = 0
        for seq, _ts, et, _pl in events:
            if et == EventType.JOB_STARTED.value:
                last_job_started = max(last_job_started, int(seq))
        if last_job_started <= 0:
            return
        latest_um_seq = 0
        latest_mid: Any = None
        latest_was_continuation = False
        for seq, _ts, et, pl in events:
            if et == EventType.USER_MESSAGE.value and isinstance(pl, dict):
                latest_um_seq = int(seq)
                latest_mid = pl.get("message_id")
                latest_was_continuation = bool(pl.get("telegram_continuation"))
        if latest_um_seq <= last_job_started:
            return
        if latest_was_continuation:
            return
        phase = (
            f"late_chunk_{latest_mid}"
            if latest_mid is not None
            else f"late_chunk_seq_{latest_um_seq}"
        )
        worker_label = self._telegram_worker_label(snapshot["projection"], running=True)
        try:
            self._send_telegram_message_once(
                task_id,
                phase,
                format_late_chunk_notice(task_id, worker_label=worker_label),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"andrea_sync telegram late-chunk notice error: {exc}", flush=True)

    def _handle_telegram_followups(
        self,
        task_id: str,
        snapshot: Dict[str, Any],
    ) -> None:
        self._maybe_notify_telegram_continuation(task_id, snapshot)
        self._maybe_notify_late_chunk_after_job_started(task_id, snapshot)
        projection = snapshot["projection"]
        status = str(projection.get("status") or "")
        if status == "created":
            self._route_telegram_task(task_id)
            return
        assistant_meta = (
            projection.get("meta", {}).get("assistant", {})
            if isinstance(projection.get("meta"), dict)
            else {}
        )
        if assistant_meta.get("route") == "direct" and status == "completed":
            try:
                self._send_telegram_message_once(
                    task_id,
                    "direct",
                    format_direct_message(str(assistant_meta.get("last_reply") or "")),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"andrea_sync telegram direct reply error: {exc}", flush=True)
            return
        if status == "queued":
            worker_label = self._telegram_worker_label(projection)
            telegram_meta = self._projection_meta(projection, "telegram")
            if self._telegram_send_lifecycle_messages(projection):
                try:
                    self._send_telegram_message_once(
                        task_id,
                        "ack",
                        format_ack_message(
                            task_id,
                            worker_label=worker_label,
                            auto_start=(
                                self.background_enabled
                                and self.delegated_execution_enabled
                                and (worker_label == "OpenClaw" or self.telegram_auto_cursor)
                            ),
                            routing_hint=str(telegram_meta.get("routing_hint") or ""),
                            collaboration_mode=str(
                                self._projection_meta(projection, "execution").get("collaboration_mode")
                                or telegram_meta.get("collaboration_mode")
                                or ""
                            ),
                            preferred_model_label=str(
                                self._projection_meta(projection, "execution").get("preferred_model_label")
                                or telegram_meta.get("preferred_model_label")
                                or ""
                            ),
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"andrea_sync telegram ack error: {exc}", flush=True)
            self._schedule_delegated_execution(task_id)
            return
        cursor_meta = (
            projection.get("meta", {}).get("cursor", {})
            if isinstance(projection.get("meta"), dict)
            else {}
        )
        execution_meta = self._projection_meta(projection, "execution")
        if status == "running":
            agent_url = cursor_meta.get("agent_url")
            openclaw_meta = self._projection_meta(projection, "openclaw")
            if self._telegram_send_lifecycle_messages(projection):
                try:
                    self._send_telegram_message_once(
                        task_id,
                        "started",
                        format_running_message(
                            task_id,
                            agent_url=str(agent_url or ""),
                            worker_label=self._telegram_worker_label(projection, running=True),
                            delegated_to_cursor=bool(execution_meta.get("delegated_to_cursor")),
                            routing_hint="",
                            collaboration_mode="",
                            provider=str(openclaw_meta.get("provider") or ""),
                            model=str(openclaw_meta.get("model") or ""),
                            preferred_model_label="",
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"andrea_sync telegram running update error: {exc}", flush=True)
            self._send_telegram_progress_updates(task_id, snapshot)
            return
        if status in {"completed", "failed"}:
            self._send_telegram_progress_updates(task_id, snapshot)
            openclaw_meta = self._projection_meta(projection, "openclaw")
            visibility_mode = self._task_visibility_mode(task_id)
            try:
                self._send_telegram_message_once(
                    task_id,
                    "final",
                    format_final_message(
                        task_id,
                        status=status,
                        summary=self._telegram_final_summary_text(projection),
                        pr_url=str(cursor_meta.get("pr_url") or ""),
                        agent_url=str(cursor_meta.get("agent_url") or ""),
                        last_error=self._telegram_user_safe_error_text(projection)
                        if status == "failed"
                        else "",
                        worker_label=self._telegram_worker_label(projection),
                        delegated_to_cursor=bool(execution_meta.get("delegated_to_cursor")),
                        backend=str(execution_meta.get("backend") or ""),
                        openclaw_session_id=str(
                            openclaw_meta.get("session_id") or ""
                        ),
                        visibility_mode=visibility_mode,
                        collaboration_trace=self._telegram_collaboration_trace(projection),
                        routing_hint="",
                        collaboration_mode="",
                        provider=str(openclaw_meta.get("provider") or ""),
                        model=str(openclaw_meta.get("model") or ""),
                        preferred_model_label="",
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"andrea_sync telegram final update error: {exc}", flush=True)

    def _handle_alexa_followups(
        self,
        task_id: str,
        snapshot: Dict[str, Any],
    ) -> None:
        projection = snapshot["projection"]
        status = str(projection.get("status") or "")
        if status == "created":
            self._route_task_with_decision(
                task_id,
                history=[],
                source="alexa_commands_ingress",
            )
            return
        if status == "queued":
            self._schedule_delegated_execution(task_id)
            return
        if status not in {"completed", "failed"}:
            return
        if not self.alexa_summary_to_telegram or not self.alexa_summary_chat_id:
            return
        execution_meta = self._projection_meta(projection, "execution")
        assistant_meta = self._projection_meta(projection, "assistant")
        assistant_route = str(assistant_meta.get("route") or "").strip()
        summary_text = str(projection.get("summary") or "")
        if assistant_route == "direct":
            summary_text = str(assistant_meta.get("last_reply") or summary_text)
        try:
            self._send_telegram_chat_message_once(
                task_id,
                "alexa_summary",
                chat_id=self.alexa_summary_chat_id,
                text=format_alexa_session_summary(
                    task_id,
                    status=status,
                    request_text=str(self._projection_meta(projection, "alexa").get("last_text") or projection.get("summary") or ""),
                    summary=summary_text,
                    assistant_route=assistant_route,
                    worker_label=self._telegram_worker_label(projection),
                    delegated_to_cursor=bool(execution_meta.get("delegated_to_cursor")),
                    agent_url=str(self._projection_meta(projection, "cursor").get("agent_url") or ""),
                    pr_url=str(self._projection_meta(projection, "cursor").get("pr_url") or ""),
                    last_error=self._telegram_user_safe_error_text(projection)
                    if status == "failed"
                    else "",
                ),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"andrea_sync alexa summary error: {exc}", flush=True)

    def _route_task_with_decision(
        self,
        task_id: str,
        *,
        history: list[dict[str, str]] | None,
        source: str,
    ) -> tuple[Optional[Any], bool]:
        marker = self._meta_key("route_applied", task_id)
        if not self._claim_routing_attempt(task_id):
            return None, False
        try:
            user_payload = self._latest_user_message_payload(task_id)
            classify_text = self._routing_classification_text(task_id)
            execution_prompt = self._extract_cursor_prompt(task_id)
            principal_prefs = self._principal_preferences(task_id)
            effective_history = history if history is not None else self._recent_task_history(task_id)
            structured_action = self._maybe_handle_structured_assistant_action(task_id)
            if structured_action is not None:
                reply_text, reason = structured_action
                decision = AndreaRouteDecision(
                    mode="direct",
                    reason=reason,
                    reply_text=reply_text,
                )
                applied = self._append_task_event(
                    task_id,
                    EventType.ASSISTANT_REPLIED,
                    {
                        "text": decision.reply_text,
                        "route": "direct",
                        "reason": decision.reason,
                    },
                )
                if applied:
                    self.with_lock(lambda c: set_meta(c, marker, str(time.time())))
                return decision, applied
            decision = route_message(
                classify_text,
                history=effective_history,
                memory_notes=self._principal_memory_notes(task_id),
                routing_hint=str(
                    user_payload.get("routing_hint")
                    or principal_prefs.get("routing_hint")
                    or "auto"
                ),
                collaboration_mode=str(
                    user_payload.get("collaboration_mode")
                    or principal_prefs.get("collaboration_mode")
                    or "auto"
                ),
                preferred_model_family=str(
                    user_payload.get("preferred_model_family")
                    or principal_prefs.get("preferred_model_family")
                    or ""
                ),
            )
            if decision.mode == "delegate":
                execution_lane = decision.delegate_target or self.telegram_delegate_lane
                kind = "openclaw" if execution_lane == "openclaw_hybrid" else "cursor"
                applied = self._append_task_event(
                    task_id,
                    EventType.JOB_QUEUED,
                    {
                        "kind": kind,
                        "prompt_excerpt": execution_prompt[:300],
                        "source": source,
                        "route_reason": decision.reason,
                        "execution_lane": execution_lane,
                        "runner": "openclaw" if kind == "openclaw" else "cursor",
                        "routing_hint": str(user_payload.get("routing_hint") or "auto"),
                        "collaboration_mode": decision.collaboration_mode,
                        "visibility_mode": str(
                            user_payload.get("visibility_mode")
                            or principal_prefs.get("visibility_mode")
                            or "summary"
                        ),
                        "requested_capability": str(
                            user_payload.get("requested_capability") or ""
                        ),
                        "mention_targets": user_payload.get("mention_targets", []),
                        "model_mentions": user_payload.get("model_mentions", []),
                        "preferred_model_family": str(
                            user_payload.get("preferred_model_family")
                            or principal_prefs.get("preferred_model_family")
                            or ""
                        ),
                        "preferred_model_label": str(
                            user_payload.get("preferred_model_label")
                            or principal_prefs.get("preferred_model_label")
                            or ""
                        ),
                    },
                )
            else:
                applied = self._append_task_event(
                    task_id,
                    EventType.ASSISTANT_REPLIED,
                    {
                        "text": decision.reply_text,
                        "route": "direct",
                        "reason": decision.reason,
                    },
                )
            if applied:
                self.with_lock(lambda c: set_meta(c, marker, str(time.time())))
            return decision, applied
        finally:
            self._finish_routing_attempt(task_id)

    def _route_telegram_task(self, task_id: str) -> None:
        self._route_task_with_decision(
            task_id,
            history=self._recent_task_history(task_id),
            source="telegram_balanced_delegate",
        )

    def _process_alexa_request(self, body: Dict[str, Any]) -> Dict[str, Any]:
        cmd, fallback_response = alexa_adapt.parse_alexa_body(body)
        if not cmd:
            return fallback_response

        def run(c: sqlite3.Connection) -> Dict[str, Any]:
            return handle_command(c, cmd)

        result = self.with_lock(run)
        if not result.get("ok") or not result.get("task_id"):
            return alexa_adapt._response(
                "I hit a problem starting that request. Please try again.",
                session_should_end=True,
            )
        task_id = str(result["task_id"])
        snapshot = self._task_snapshot(task_id)
        if snapshot and snapshot["projection"].get("status") == "created":
            decision, _applied = self._route_task_with_decision(
                task_id,
                history=self._recent_task_history(task_id),
                source="alexa_voice_delegate",
            )
        else:
            decision = None
        final_snapshot = self._task_snapshot(task_id) or snapshot
        if final_snapshot and final_snapshot["projection"].get("status") == "queued":
            self._queue_task_followups(task_id)
        if decision and decision.mode == "direct":
            return alexa_adapt._response(
                decision.reply_text,
                session_should_end=True,
            )
        return alexa_adapt.build_ack_response(
            "I started working on that.",
            delegated=True,
            telegram_summary_expected=bool(
                self.alexa_summary_to_telegram and self.alexa_summary_chat_id
            ),
        )

    def _schedule_cursor_execution(self, task_id: str) -> None:
        if not self.background_enabled or not self.delegated_execution_enabled:
            return
        snapshot = self._task_snapshot(task_id)
        channel = snapshot["channel"] if snapshot else ""
        if channel == "telegram" and not self.telegram_auto_cursor:
            return
        marker = self._meta_key("executor_started", task_id)
        ttl = int(self.executor_started_ttl_seconds or 0)

        def claim(c: sqlite3.Connection) -> bool:
            raw = get_meta(c, marker)
            if raw is not None and ttl > 0:
                try:
                    started_at = float(raw)
                except ValueError:
                    started_at = 0.0
                if time.time() - started_at > ttl:
                    delete_meta(c, marker)
                    raw = None
            if raw is not None:
                return False
            set_meta(c, marker, str(time.time()))
            return True

        if not self.with_lock(claim):
            return
        threading.Thread(
            target=self._run_delegated_job,
            args=(task_id,),
            daemon=True,
        ).start()

    def _schedule_delegated_execution(self, task_id: str) -> None:
        self._schedule_cursor_execution(task_id)

    def _append_task_event(
        self, task_id: str, event_type: EventType, payload: Dict[str, Any]
    ) -> bool:
        def append(c: sqlite3.Connection) -> bool:
            if not get_task_channel(c, task_id):
                return False
            append_event(c, task_id, event_type, payload)
            return True

        if self.with_lock(append):
            self._queue_task_followups(task_id)
            return True
        return False

    def _run_json_subprocess(
        self,
        args: list[str],
        *,
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        proc = subprocess.run(
            args,
            cwd=str(self.repo_root),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        payload: Dict[str, Any]
        if stdout:
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"JSON subprocess output parse failed: {stdout[:500]}"
                ) from exc
        else:
            payload = {}
        if proc.returncode != 0:
            detail = ""
            if isinstance(payload, dict):
                detail = str(
                    payload.get("error")
                    or payload.get("message")
                    or payload.get("response")
                    or ""
                )
            raise RuntimeError(
                f"subprocess failed exit={proc.returncode}: {detail or stderr or stdout[:500]}"
            )
        return payload

    def _run_text_subprocess(
        self,
        args: list[str],
        *,
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        proc = subprocess.run(
            args,
            cwd=str(self.repo_root),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        if proc.returncode != 0:
            raise RuntimeError(
                f"subprocess failed exit={proc.returncode}: {stderr or stdout[:500]}"
            )
        return {"ok": True, "stdout": stdout, "stderr": stderr}

    def _openclaw_session_id(self, task_id: str, *, attempt: int = 0) -> str:
        agent_slug = re.sub(r"[^a-z0-9_-]+", "-", self.openclaw_agent_id.lower()).strip("-")
        task_slug = re.sub(r"[^a-z0-9_-]+", "-", str(task_id or "").lower()).strip("-")
        agent_slug = agent_slug or "main"
        task_slug = task_slug or "task"
        return f"andrea-sync-{agent_slug}-{task_slug}-{attempt}"

    def _refresh_openclaw_runtime(
        self,
        task_id: str,
        *,
        skill_key: str,
        heal_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not bool(heal_result.get("refresh_required")):
            return {
                "ok": True,
                "mode": "none",
                "session_id": self._openclaw_session_id(task_id, attempt=1),
            }
        actions = heal_result.get("actions") if isinstance(heal_result.get("actions"), list) else []
        changed_kinds = {str(item.get("kind") or "") for item in actions if isinstance(item, dict)}
        gateway_needed = bool(changed_kinds.intersection({"config_repair", "openclaw_install", "openclaw_update_all"}))
        if self.openclaw_refresh_mode in {"auto", "gateway"} and gateway_needed:
            try:
                self._run_text_subprocess(
                    ["openclaw", "gateway", "restart"],
                    timeout_seconds=self.openclaw_gateway_restart_timeout_seconds,
                )
                return {
                    "ok": True,
                    "mode": "gateway_restart",
                    "skill_key": skill_key,
                    "session_id": self._openclaw_session_id(task_id, attempt=1),
                }
            except Exception as exc:  # noqa: BLE001
                if self.openclaw_refresh_mode == "gateway":
                    return {
                        "ok": False,
                        "mode": "gateway_restart_failed",
                        "skill_key": skill_key,
                        "error": _clip(exc, 600),
                        "session_id": self._openclaw_session_id(task_id, attempt=1),
                    }
        return {
            "ok": True,
            "mode": "session_rotation",
            "skill_key": skill_key,
            "session_id": self._openclaw_session_id(task_id, attempt=1),
        }

    def _routing_classification_text(self, task_id: str) -> str:
        """
        Text used for Andrea router classification only.
        Must be the latest user turn — not telegram accumulated_prompt, which merges
        thread history and breaks meta questions after a prior message in the same task.
        """
        payload = self._latest_user_message_payload(task_id)
        return str(payload.get("routing_text") or payload.get("text") or "").strip()

    def _extract_cursor_prompt(self, task_id: str) -> str:
        snapshot = self._task_snapshot(task_id)
        if snapshot:
            telegram_meta = self._projection_meta(snapshot["projection"], "telegram")
            acc = str(telegram_meta.get("accumulated_prompt") or "").strip()
            if acc:
                return acc
        payload = self._latest_user_message_payload(task_id)
        prompt = str(payload.get("routing_text") or payload.get("text") or "").strip()
        if prompt:
            return prompt
        if not snapshot:
            return ""
        return str(snapshot["projection"].get("summary") or "").strip()

    def _append_orchestration_step(
        self,
        task_id: str,
        phase: str,
        status: str,
        *,
        lane: str = "",
        summary: str = "",
        provider: str = "",
        model: str = "",
    ) -> None:
        self._append_task_event(
            task_id,
            EventType.ORCHESTRATION_STEP,
            {
                "phase": str(phase or "").strip().lower(),
                "status": str(status or "").strip().lower(),
                "lane": str(lane or "").strip(),
                "summary": _clip(summary, 400) if summary else "",
                "provider": _clip(provider, 120) if provider else "",
                "model": _clip(model, 200) if model else "",
            },
        )

    def _outbound_draft_owner_key(self, task_id: str) -> str:
        principal_id = self._task_principal_id(task_id)
        if principal_id:
            return f"principal:{principal_id}"
        payload = self._latest_user_message_payload(task_id)
        channel = str(payload.get("channel") or "telegram").strip() or "telegram"
        chat_id = str(payload.get("chat_id") or payload.get("user_id") or "").strip()
        if chat_id:
            return f"{channel}:{chat_id}"
        return f"task:{task_id}"

    def _outbound_draft_meta_key(self, owner_key: str) -> str:
        return f"andrea_bridge:outbound_draft:{owner_key}"

    def _load_pending_outbound_draft(self, task_id: str) -> Dict[str, Any]:
        owner_key = self._outbound_draft_owner_key(task_id)
        key = self._outbound_draft_meta_key(owner_key)

        def read(c: sqlite3.Connection) -> Dict[str, Any]:
            raw = get_meta(c, key)
            if not raw:
                return {}
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                delete_meta(c, key)
                return {}
            if not isinstance(payload, dict):
                delete_meta(c, key)
                return {}
            expires_at = payload.get("expires_at")
            try:
                if expires_at is not None and float(expires_at) <= time.time():
                    delete_meta(c, key)
                    return {}
            except (TypeError, ValueError):
                delete_meta(c, key)
                return {}
            return payload

        return self.with_lock(read)

    def _save_pending_outbound_draft(self, task_id: str, draft: Dict[str, Any]) -> None:
        owner_key = self._outbound_draft_owner_key(task_id)
        key = self._outbound_draft_meta_key(owner_key)

        def write(c: sqlite3.Connection) -> None:
            set_meta(c, key, json.dumps(draft, ensure_ascii=False))

        self.with_lock(write)

    def _clear_pending_outbound_draft(self, task_id: str) -> None:
        owner_key = self._outbound_draft_owner_key(task_id)
        key = self._outbound_draft_meta_key(owner_key)
        self.with_lock(lambda c: delete_meta(c, key))

    def _parse_outbound_message_request(self, text: str) -> Optional[Dict[str, Any]]:
        clean = str(text or "").strip()
        if not clean:
            return None
        for pattern in OUTBOUND_SEND_PATTERNS:
            match = pattern.match(clean)
            if not match:
                continue
            target = str(match.group("target") or "").strip(" ,.")
            body = str(match.groupdict().get("body") or "").strip(" .")
            if target.lower().startswith("to "):
                target = target[3:].strip()
            target_key = target.strip().lower()
            if not target:
                return {"error": "missing_target"}
            if target_key in AMBIGUOUS_TARGETS:
                return {"error": "ambiguous_target", "target": target}
            if PHONE_TARGET_RE.match(target):
                return {"error": "phone_number_only", "target": target}
            return {
                "target": target,
                "message": body,
                "needs_body": not bool(body),
            }
        return None

    def _parse_live_news_request(self, text: str) -> Optional[str]:
        clean = str(text or "").strip()
        if not clean:
            return None
        lowered = clean.lower()
        if not LIVE_NEWS_RE.search(lowered):
            return None
        if re.search(r"\b(good news|bad news|fake news)\b", lowered):
            return None
        if not (
            "?" in clean
            or lowered.startswith(
                (
                    "what",
                    "what's",
                    "whats",
                    "show",
                    "give",
                    "tell me",
                    "summarize",
                    "summarise",
                    "latest",
                    "current",
                    "news",
                    "headlines",
                    "any",
                )
            )
        ):
            return None
        return clean

    def _parse_recent_text_messages_request(self, text: str) -> Optional[str]:
        clean = str(text or "").strip()
        if not clean:
            return None
        lowered = clean.lower()
        if not RECENT_TEXT_MESSAGES_RE.search(lowered):
            return None
        if any(marker in lowered for marker in ("telegram", "this chat", "our chat")):
            return None
        if self._parse_outbound_message_request(clean) is not None:
            return None
        if not (
            "?" in clean
            or lowered.startswith(
                (
                    "what",
                    "what's",
                    "whats",
                    "show",
                    "list",
                    "read",
                    "check",
                    "pull",
                    "give",
                    "summarize",
                    "summarise",
                    "latest",
                    "recent",
                )
            )
        ):
            return None
        return clean

    def _select_messaging_capability(self, text: str) -> Dict[str, Any]:
        clean = str(text or "").strip().lower()
        for candidate in MESSAGING_SKILL_CANDIDATES:
            for term in candidate["match_terms"]:
                if term in clean:
                    return dict(candidate)
        return dict(MESSAGING_SKILL_CANDIDATES[0])

    def _resolve_runtime_skill(
        self,
        task_id: str,
        *,
        skill_key: str,
        actor: str = "server",
    ) -> Dict[str, Any]:
        truth = self.with_lock(lambda c: resolve_skill_truth(c, skill_key))
        heal: Dict[str, Any] = {}
        refresh: Dict[str, Any] = {}
        if str(truth.get("status") or "") != "verified_available":
            heal = self.with_lock(
                lambda c: handle_command(
                    c,
                    {
                        "command_type": "HealRuntimeCapability",
                        "channel": "internal",
                        "payload": {
                            "skill_key": skill_key,
                            "actor": actor,
                            "allow_install": True,
                            "allow_update_all": True,
                            "allow_config_repair": True,
                        },
                    },
                )
            )
            if bool(heal.get("refresh_required")):
                refresh = self._refresh_openclaw_runtime(
                    task_id,
                    skill_key=skill_key,
                    heal_result=heal,
                )
            truth = self.with_lock(lambda c: resolve_skill_truth(c, skill_key))
        return {
            "skill_key": skill_key,
            "truth": truth,
            "heal": heal,
            "refresh": refresh,
        }

    def _resolve_messaging_capability(
        self,
        task_id: str,
        text: str,
    ) -> Dict[str, Any]:
        candidate = self._select_messaging_capability(text)
        skill_key = str(candidate["skill_key"])
        return {
            **candidate,
            **self._resolve_runtime_skill(task_id, skill_key=skill_key, actor="server"),
        }

    def _messaging_capability_reply(self, resolved: Dict[str, Any], text: str) -> str:
        status = str(resolved.get("truth", {}).get("status") or "")
        label = str(resolved.get("label") or "messaging").strip()
        clean = str(text or "").strip().lower()
        specific = "blue bubbles" in clean or "bluebubbles" in clean
        if status == "verified_available":
            if specific:
                return (
                    "Yes. BlueBubbles is verified and available here. "
                    "For personal outreach, I will draft the message first and wait for your confirmation before sending it."
                )
            return (
                f"Yes. I have a verified {label} lane available here. "
                "For personal outreach, I will draft the message first and wait for your confirmation before sending it."
            )
        if status == "installed_but_not_eligible":
            return (
                f"Not yet. I found the {label} lane, but it still needs a bit more local setup before I can use it reliably."
            )
        return (
            f"Not right now. I could not verify a live {label} lane after checking the current runtime state."
        )

    def _live_news_unavailable_reply(self, resolved: Dict[str, Any]) -> str:
        status = str(resolved.get("truth", {}).get("status") or "")
        if status == "installed_but_not_eligible":
            return (
                "I found the live news lane, but it still needs a bit more local setup before I can rely on it."
            )
        return (
            "I couldn't verify the live news lane just now, so I can't give you a grounded news update yet."
        )

    def _recent_text_messages_unavailable_reply(self, resolved: Dict[str, Any]) -> str:
        status = str(resolved.get("truth", {}).get("status") or "")
        skill_key = str(resolved.get("skill_key") or "").strip().lower()
        lane = "BlueBubbles" if skill_key == "bluebubbles" else str(
            resolved.get("label") or "messaging"
        ).strip()
        if status == "installed_but_not_eligible":
            return (
                f"I found the {lane} lane, but it still needs a bit more local setup before I can read recent messages reliably."
            )
        return (
            f"I couldn't verify the {lane} lane just now, so I can't retrieve your recent text messages yet."
        )

    def _runtime_skill_grounding_note(
        self,
        resolved: Dict[str, Any],
        *,
        label: str,
        verified_text: str,
        local_fallback_text: str,
    ) -> str:
        status = str(resolved.get("truth", {}).get("status") or "")
        if status == "verified_available":
            return verified_text
        if status == "installed_but_not_eligible":
            return (
                f"{local_fallback_text} The native {label} lane still needs a bit more local setup, "
                "so I kept this in Andrea's local capture for now."
            )
        if resolved.get("heal") or resolved.get("refresh"):
            return (
                f"{local_fallback_text} I also re-checked the native {label} lane in the background "
                "so the runtime stays grounded."
            )
        return (
            f"{local_fallback_text} The native {label} lane is not verified yet, "
            "so I kept this in Andrea's local capture for now."
        )

    def _outbound_draft_reply(self, draft: Dict[str, Any]) -> str:
        target = str(draft.get("target") or "them").strip()
        message = str(draft.get("message") or "").strip()
        return (
            f'Draft for {target}: "{message}" '
            "Reply `send it` if you want me to send it, or tell me what to change."
        )

    def _build_outbound_message_prompt(self, draft: Dict[str, Any]) -> str:
        target = str(draft.get("target") or "").strip()
        message = str(draft.get("message") or "").strip()
        capability = str(draft.get("skill_key") or "bluebubbles").strip()
        return (
            "Use the verified personal messaging lane to send an outbound message.\n"
            f"Capability: {capability}\n"
            f"Recipient: {target}\n"
            f'Exact message to send: "{message}"\n\n'
            "Rules:\n"
            "- Send the exact message above unless the delivery tool requires tiny punctuation cleanup.\n"
            "- Do not ask the user for session identifiers, runtime labels, or internal routing details.\n"
            "- If the recipient cannot be resolved or delivery cannot be verified, do not invent success.\n"
            "- Return a calm Andrea-style summary of what happened.\n"
            "- Keep internal tool/config/runtime diagnostics out of the user-facing summary.\n"
        )

    def _build_live_news_prompt(self, text: str) -> str:
        return (
            "Use the verified live web/news lane to answer Andrea's request for current news.\n"
            f"User request: {text.strip()}\n\n"
            "Rules:\n"
            "- Use Brave or another grounded live-web/news skill that is already available in OpenClaw.\n"
            "- Infer any requested topic or place from the user request. If none is given, provide a compact general roundup.\n"
            "- Keep the final user-facing summary to 1-2 short sentences with the most important current items.\n"
            "- Only include grounded live information.\n"
            "- If live retrieval is blocked or uncertain, say so plainly and do not invent details.\n"
            "- Keep internal tool/config/runtime details out of the user-facing answer.\n"
        )

    def _build_recent_text_messages_prompt(self, text: str, *, skill_key: str) -> str:
        return (
            "Use the verified personal messaging lane to retrieve recent phone/iMessage activity.\n"
            f"Capability: {skill_key or 'bluebubbles'}\n"
            f"User request: {text.strip()}\n\n"
            "Rules:\n"
            "- Prefer recent real text or iMessage threads and summarize only what the tool can verify.\n"
            "- Keep the user-facing summary concise and privacy-respecting.\n"
            "- If the lane cannot list recent messages or cannot verify their contents, say so plainly and do not invent anything.\n"
            "- Keep internal tool/config/runtime details out of the user-facing answer.\n"
            "- Put the final answer in the summary field as 1-2 short sentences.\n"
        )

    def _run_direct_openclaw_lookup(
        self,
        task_id: str,
        *,
        prompt: str,
        route_reason: str,
        success_reason: str,
        success_fallback: str,
        failure_reason: str,
        failure_reply: str,
    ) -> tuple[str, str]:
        try:
            result = self._create_openclaw_job(
                task_id,
                prompt,
                route_reason,
                "andrea_primary",
                "",
                "",
                session_id=self._openclaw_session_id(task_id, attempt=0),
            )
        except Exception:  # noqa: BLE001
            return failure_reply, failure_reason
        summary = ""
        for candidate in (
            result.get("user_summary"),
            result.get("summary"),
            result.get("raw_text"),
            result.get("blocked_reason"),
            result.get("error"),
        ):
            sanitized = shared_sanitize_user_surface_text(
                candidate,
                fallback="",
                limit=500,
            ).strip()
            if sanitized and not _is_generic_openclaw_summary(sanitized):
                summary = sanitized
                break
        if result.get("ok"):
            return summary or success_fallback, success_reason
        if summary:
            return summary, failure_reason
        return failure_reply, failure_reason

    def _fetch_live_news_summary(
        self,
        task_id: str,
        text: str,
    ) -> tuple[str, str]:
        resolved = self._resolve_runtime_skill(
            task_id,
            skill_key="brave-api-search",
            actor="server_news",
        )
        if str(resolved.get("truth", {}).get("status") or "") != "verified_available":
            return self._live_news_unavailable_reply(resolved), "news_summary_unavailable"
        return self._run_direct_openclaw_lookup(
            task_id,
            prompt=self._build_live_news_prompt(text),
            route_reason="structured_live_news",
            success_reason="news_summary_ready",
            success_fallback="I pulled a live news summary for you.",
            failure_reason="news_summary_failed",
            failure_reply="I couldn't pull a grounded live news summary cleanly just now.",
        )

    def _fetch_recent_text_messages(
        self,
        task_id: str,
        text: str,
    ) -> tuple[str, str]:
        resolved = self._resolve_messaging_capability(task_id, text)
        if str(resolved.get("truth", {}).get("status") or "") != "verified_available":
            return (
                self._recent_text_messages_unavailable_reply(resolved),
                "recent_text_messages_unavailable",
            )
        return self._run_direct_openclaw_lookup(
            task_id,
            prompt=self._build_recent_text_messages_prompt(
                text,
                skill_key=str(resolved.get("skill_key") or "bluebubbles"),
            ),
            route_reason="structured_recent_text_messages",
            success_reason="recent_text_messages_ready",
            success_fallback="I pulled your recent text-message summary.",
            failure_reason="recent_text_messages_failed",
            failure_reply="I couldn't retrieve your recent text messages cleanly just now.",
        )

    def _send_pending_outbound_message(
        self,
        task_id: str,
        draft: Dict[str, Any],
    ) -> tuple[str, str]:
        try:
            result = self._create_openclaw_job(
                task_id,
                self._build_outbound_message_prompt(draft),
                "verified_outbound_message",
                "andrea_primary",
                "",
                "",
                session_id=self._openclaw_session_id(task_id, attempt=0),
            )
        except Exception as exc:  # noqa: BLE001
            return (
                "I could not send that message cleanly just now. The draft is still ready if you want me to revise it or try again.",
                "outbound_message_send_failed",
            )
        if result.get("ok"):
            self._clear_pending_outbound_draft(task_id)
            summary = str(result.get("user_summary") or result.get("summary") or "").strip()
            if summary:
                return summary, "outbound_message_sent"
            return (
                f"I sent it to {str(draft.get('target') or 'them').strip()}.",
                "outbound_message_sent",
            )
        return (
            "I could not send that message cleanly just now. The draft is still ready if you want me to revise it or try again.",
            "outbound_message_send_failed",
        )

    def _parse_memory_note_request(self, text: str) -> str:
        clean = str(text or "").strip()
        lowered = clean.lower()
        if not (
            lowered.startswith("remember that ")
            or lowered.startswith("please remember that ")
            or lowered.startswith("remember this ")
            or lowered.startswith("please remember this ")
        ):
            return ""
        match = REMEMBER_NOTE_RE.match(clean)
        if not match:
            return ""
        note = str(match.group("body") or "").strip().rstrip(".")
        if not note or note.endswith("?"):
            return ""
        return note

    def _parse_reminder_request(self, text: str) -> Optional[Dict[str, Any]]:
        clean = str(text or "").strip()
        match = REMIND_ME_RE.match(clean)
        if not match:
            return None
        body = str(match.group("body") or "").strip()
        if not body:
            return None
        now = dt.datetime.now().astimezone()
        due = now + dt.timedelta(hours=1)
        defaulted = True
        lowered = body.lower()
        rel = RELATIVE_REMINDER_RE.search(body)
        if rel:
            count = max(1, int(rel.group("count")))
            unit = str(rel.group("unit") or "").lower()
            if unit.startswith("minute"):
                due = now + dt.timedelta(minutes=count)
            elif unit.startswith("hour"):
                due = now + dt.timedelta(hours=count)
            else:
                due = now + dt.timedelta(days=count)
            body = (body[: rel.start()] + body[rel.end() :]).strip(" ,.")
            defaulted = False
        elif "tomorrow morning" in lowered:
            due = (now + dt.timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            body = re.sub(r"\btomorrow morning\b", "", body, flags=re.I).strip(" ,.")
            defaulted = False
        elif "tomorrow afternoon" in lowered:
            due = (now + dt.timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
            body = re.sub(r"\btomorrow afternoon\b", "", body, flags=re.I).strip(" ,.")
            defaulted = False
        elif "tomorrow evening" in lowered or "tomorrow night" in lowered:
            due = (now + dt.timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
            body = re.sub(r"\btomorrow (?:evening|night)\b", "", body, flags=re.I).strip(" ,.")
            defaulted = False
        elif "tomorrow" in lowered:
            due = (now + dt.timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            body = re.sub(r"\btomorrow\b", "", body, flags=re.I).strip(" ,.")
            defaulted = False
        elif "later today" in lowered:
            target = now.replace(hour=17, minute=0, second=0, microsecond=0)
            if target <= now:
                target = now + dt.timedelta(hours=3)
            due = target
            body = re.sub(r"\blater today\b", "", body, flags=re.I).strip(" ,.")
            defaulted = False
        elif "tonight" in lowered:
            target = now.replace(hour=20, minute=0, second=0, microsecond=0)
            if target <= now:
                target = now + dt.timedelta(hours=2)
            due = target
            body = re.sub(r"\btonight\b", "", body, flags=re.I).strip(" ,.")
            defaulted = False
        elif "today" in lowered:
            target = now.replace(hour=17, minute=0, second=0, microsecond=0)
            if target <= now:
                target = now + dt.timedelta(hours=2)
            due = target
            body = re.sub(r"\btoday\b", "", body, flags=re.I).strip(" ,.")
            defaulted = False
        message = body.strip(" .")
        if not message:
            return None
        return {
            "message": message,
            "due_at": due.timestamp(),
            "defaulted": defaulted,
        }

    def _maybe_handle_structured_assistant_action(
        self, task_id: str
    ) -> tuple[str, str] | None:
        payload = self._latest_user_message_payload(task_id)
        text = str(payload.get("routing_text") or payload.get("text") or "").strip()
        if not text:
            return None
        channel = str(payload.get("channel") or "telegram").strip() or "telegram"
        pending_draft = self._load_pending_outbound_draft(task_id)
        if pending_draft:
            if OUTBOUND_CANCEL_RE.match(text):
                self._clear_pending_outbound_draft(task_id)
                return ("Okay. I will not send it.", "outbound_message_cancelled")
            if OUTBOUND_CONFIRM_RE.match(text):
                return self._send_pending_outbound_message(task_id, pending_draft)
            lowered = text.lower()
            if not (
                MESSAGING_CAPABILITY_RE.search(text)
                or self._parse_outbound_message_request(text)
                or lowered.startswith("remember")
                or lowered.startswith("remind me")
            ):
                target = str(pending_draft.get("target") or "them").strip()
                return (
                    f'I still have the draft for {target}. Reply `send it`, `cancel`, or tell me the exact wording you want instead.',
                    "outbound_message_pending",
                )

        outbound_request = self._parse_outbound_message_request(text)
        if outbound_request is not None:
            error = str(outbound_request.get("error") or "")
            if error == "ambiguous_target":
                return (
                    "I can do that, but name the person explicitly instead of saying `her`, `him`, or `them`, and I will draft it first.",
                    "outbound_message_target_ambiguous",
                )
            if error == "phone_number_only":
                return (
                    "I need a resolvable contact or thread for this messaging lane, not just a raw phone number. Give me the recipient name as it appears in Messages and I will draft it first.",
                    "outbound_message_phone_number_only",
                )
            if outbound_request.get("needs_body"):
                return (
                    f"I can do that. Tell me the exact message you want sent to {outbound_request['target']}, and I will draft it before sending.",
                    "outbound_message_needs_body",
                )
            resolved = self._resolve_messaging_capability(task_id, text)
            if str(resolved.get("truth", {}).get("status") or "") != "verified_available":
                return (
                    self._messaging_capability_reply(resolved, text),
                    "outbound_message_capability_unavailable",
                )
            draft = {
                "target": str(outbound_request.get("target") or "").strip(),
                "message": str(outbound_request.get("message") or "").strip(),
                "skill_key": str(resolved.get("skill_key") or "bluebubbles"),
                "label": str(resolved.get("label") or "text messaging"),
                "created_at": time.time(),
                "expires_at": time.time() + PENDING_OUTBOUND_DRAFT_TTL_SECONDS,
            }
            self._save_pending_outbound_draft(task_id, draft)
            return (self._outbound_draft_reply(draft), "outbound_message_drafted")

        news_request = self._parse_live_news_request(text)
        if news_request is not None:
            return self._fetch_live_news_summary(task_id, news_request)

        recent_text_messages = self._parse_recent_text_messages_request(text)
        if recent_text_messages is not None:
            return self._fetch_recent_text_messages(task_id, recent_text_messages)

        if MESSAGING_CAPABILITY_RE.search(text):
            resolved = self._resolve_messaging_capability(task_id, text)
            return (
                self._messaging_capability_reply(resolved, text),
                "messaging_capability_answer",
            )

        memory_note = self._parse_memory_note_request(text)
        if memory_note:
            result = self.with_lock(
                lambda c: handle_command(
                    c,
                    {
                        "command_type": "SavePrincipalMemory",
                        "channel": channel,
                        "task_id": task_id,
                        "payload": {
                            "content": memory_note,
                            "kind": "note",
                            "source": "direct_memory_capture",
                        },
                    },
                )
            )
            if result.get("ok"):
                notes_lane = self._resolve_runtime_skill(
                    task_id,
                    skill_key="apple-notes",
                    actor="server_memory",
                )
                return (
                    self._runtime_skill_grounding_note(
                        notes_lane,
                        label="Apple Notes",
                        verified_text=(
                            "I saved that as a memory note, and the Apple Notes lane is verified and available too."
                        ),
                        local_fallback_text=(
                            "I saved that as a memory note and I can use it across future Telegram and Alexa turns "
                            "when the same principal is linked."
                        ),
                    ),
                    "principal_memory_saved",
                )
        reminder = self._parse_reminder_request(text)
        if reminder:
            result = self.with_lock(
                lambda c: handle_command(
                    c,
                    {
                        "command_type": "CreateReminder",
                        "channel": channel,
                        "task_id": task_id,
                        "payload": {
                            "message": reminder["message"],
                            "due_at": reminder["due_at"],
                            "note": "structured assistant reminder",
                        },
                    },
                )
            )
            if result.get("ok"):
                reminders_lane = self._resolve_runtime_skill(
                    task_id,
                    skill_key="apple-reminders",
                    actor="server_reminder",
                )
                due_text = _format_due_time_local(float(reminder["due_at"]))
                if str(result.get("status") or "") == "awaiting_delivery_channel":
                    return (
                        f"I saved the reminder for {due_text}, but I still need a deliverable reminder channel for this principal before I can proactively send it.",
                        "reminder_saved_awaiting_channel",
                    )
                suffix = ""
                if reminder.get("defaulted"):
                    suffix = " If you want a different time, tell me and I will move it."
                return (
                    self._runtime_skill_grounding_note(
                        reminders_lane,
                        label="Apple Reminders",
                        verified_text=(
                            f"I set a reminder for {due_text}: {reminder['message']}.{suffix} "
                            "The Apple Reminders lane is verified and available too."
                        ),
                        local_fallback_text=(
                            f"I set a reminder for {due_text}: {reminder['message']}.{suffix}"
                        ),
                    ),
                    "reminder_created",
                )
        return None

    def _create_openclaw_job(
        self,
        task_id: str,
        prompt: str,
        route_reason: str,
        collaboration_mode: str,
        preferred_model_family: str,
        preferred_model_label: str,
        *,
        session_id: str,
    ) -> Dict[str, Any]:
        return self._run_json_subprocess(
            [
                sys.executable,
                str(self.repo_root / "scripts" / "andrea_sync_openclaw_hybrid.py"),
                "--task-id",
                task_id,
                "--prompt",
                prompt,
                "--repo",
                str(self.cursor_repo_path),
                "--agent-id",
                self.openclaw_agent_id,
                "--session-id",
                session_id,
                "--route-reason",
                route_reason,
                "--collaboration-mode",
                collaboration_mode,
                "--preferred-model-family",
                preferred_model_family,
                "--preferred-model-label",
                preferred_model_label,
                "--timeout-seconds",
                str(self.openclaw_timeout_seconds),
                "--thinking",
                self.openclaw_thinking,
            ],
            timeout_seconds=self.openclaw_timeout_seconds + 10,
        )

    def _create_cursor_job(self, prompt: str) -> Dict[str, Any]:
        return self._run_json_subprocess(
            [
                sys.executable,
                str(self.repo_root / "skills" / "cursor_handoff" / "scripts" / "cursor_handoff.py"),
                "--repo",
                str(self.cursor_repo_path),
                "--prompt",
                prompt,
                "--mode",
                self.cursor_mode,
                "--read-only",
                "true" if self.cursor_read_only else "false",
                "--json",
                "--poll-max-attempts",
                "0",
                "--cli-timeout-seconds",
                "0",
            ],
            timeout_seconds=self.cursor_create_timeout_seconds or None,
        )

    def _cursor_agent_status(self, agent_id: str) -> Dict[str, Any]:
        return self._run_json_subprocess(
            [
                sys.executable,
                str(self.repo_root / "scripts" / "cursor_openclaw.py"),
                "--json",
                "agent-status",
                "--id",
                agent_id,
            ],
            timeout_seconds=60,
        )

    def _cursor_agent_conversation(self, agent_id: str) -> Dict[str, Any]:
        return self._run_json_subprocess(
            [
                sys.executable,
                str(self.repo_root / "scripts" / "cursor_openclaw.py"),
                "--json",
                "conversation",
                "--id",
                agent_id,
            ],
            timeout_seconds=60,
        )

    def _extract_text_snippets(self, value: Any, out: list[str]) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                if key.lower() in {"text", "message", "summary", "content"}:
                    text = str(inner or "").strip()
                    if text:
                        out.append(text)
                self._extract_text_snippets(inner, out)
            return
        if isinstance(value, list):
            for inner in value:
                self._extract_text_snippets(inner, out)

    def _cursor_terminal_summary(
        self, agent_id: str, terminal_status: str, pr_url: str, agent_url: str
    ) -> str:
        snippets: list[str] = []
        try:
            conv = self._cursor_agent_conversation(agent_id)
            self._extract_text_snippets(conv.get("response"), snippets)
        except Exception:
            snippets = []
        for text in reversed(snippets):
            clean = text.strip()
            if clean:
                return _clip(clean, 1200)
        if pr_url:
            return f"Cursor finished with a PR ready: {pr_url}"
        if agent_url:
            return f"Cursor finished with status {terminal_status}. Agent: {agent_url}"
        return f"Cursor finished with status {terminal_status}."

    def _run_delegated_job(self, task_id: str) -> None:
        marker = self._meta_key("executor_started", task_id)
        try:
            execution_lane = self._task_execution_lane(task_id)
            if execution_lane == "openclaw_hybrid":
                self._run_openclaw_job(task_id)
                return
            self._run_cursor_job(task_id)
        except Exception as exc:  # noqa: BLE001
            self._append_task_event(
                task_id,
                EventType.JOB_FAILED,
                {
                    "error": "delegated_runner_crashed",
                    "message": str(exc)[:2000],
                    "execution_lane": self._task_execution_lane(task_id),
                },
            )
        finally:
            def clear(c: sqlite3.Connection) -> None:
                delete_meta(c, marker)

            self.with_lock(clear)

    def _run_openclaw_job(self, task_id: str) -> None:
        prompt = self._extract_cursor_prompt(task_id)
        collaboration_mode = self._task_collaboration_mode(task_id)
        visibility_mode = self._task_visibility_mode(task_id)
        routing_hint = self._task_routing_hint(task_id)
        preferred_model_family = self._task_preferred_model_family(task_id)
        preferred_model_label = self._task_preferred_model_label(task_id)
        if not prompt:
            self._append_task_event(
                task_id,
                EventType.JOB_FAILED,
                {
                    "error": "missing_prompt",
                    "message": "No Telegram text was available to send to OpenClaw.",
                    "user_safe_error": (
                        "I could not start the OpenClaw coordination lane because there was no "
                        "request text available to send."
                    ),
                    "backend": "openclaw",
                    "execution_lane": "openclaw_hybrid",
                    "visibility_mode": visibility_mode,
                    "preferred_model_family": preferred_model_family,
                    "preferred_model_label": preferred_model_label,
                },
            )
            return
        self._append_orchestration_step(task_id, "plan", "started", lane="openclaw")
        self._append_task_event(
            task_id,
            EventType.JOB_STARTED,
            {
                "backend": "openclaw",
                "execution_lane": "openclaw_hybrid",
                "runner": "openclaw",
                "status": "submitted",
                "routing_hint": routing_hint,
                "collaboration_mode": collaboration_mode,
                "visibility_mode": visibility_mode,
                "preferred_model_family": preferred_model_family,
                "preferred_model_label": preferred_model_label,
            },
        )
        if visibility_mode == "full" and collaboration_mode in {"cursor_primary", "collaborative"}:
            self._append_task_event(
                task_id,
                EventType.JOB_PROGRESS,
                {
                    "message": (
                        "OpenClaw is starting the coordination pass and will pull in the best available lanes "
                        "for planning, critique, synthesis, and repo execution if needed."
                    ),
                    "backend": "openclaw",
                    "execution_lane": "openclaw_hybrid",
                    "runner": "openclaw",
                    "routing_hint": routing_hint,
                    "collaboration_mode": collaboration_mode,
                    "visibility_mode": visibility_mode,
                    "preferred_model_family": preferred_model_family,
                    "preferred_model_label": preferred_model_label,
                    "force_telegram_note": True,
                },
            )
        try:
            result = self._create_openclaw_job(
                task_id,
                prompt,
                self._task_route_reason(task_id),
                collaboration_mode,
                preferred_model_family,
                preferred_model_label,
                session_id=self._openclaw_session_id(task_id, attempt=0),
            )
        except Exception as exc:  # noqa: BLE001
            self._append_orchestration_step(
                task_id,
                "plan",
                "failed",
                lane="openclaw",
                summary="OpenClaw could not start the planning pass cleanly.",
            )
            if self.openclaw_fallback_to_cursor:
                self._append_task_event(
                    task_id,
                    EventType.JOB_PROGRESS,
                    {
                        "message": (
                            "OpenClaw could not complete the handoff cleanly, so Andrea is "
                            "falling back to a direct Cursor launch."
                        ),
                        "backend": "cursor",
                        "execution_lane": "direct_cursor",
                        "runner": "cursor",
                        "delegated_to_cursor": True,
                        "routing_hint": routing_hint,
                        "collaboration_mode": collaboration_mode,
                        "visibility_mode": visibility_mode,
                        "preferred_model_family": preferred_model_family,
                        "preferred_model_label": preferred_model_label,
                        "force_telegram_note": visibility_mode == "full",
                    },
                )
                self._run_cursor_job(task_id)
                return
            self._append_task_event(
                task_id,
                EventType.JOB_FAILED,
                {
                    "error": "openclaw_submit_failed",
                    "message": _clip(exc, 1500),
                    "user_safe_error": (
                        "I could not start the OpenClaw coordination lane cleanly on this pass."
                    ),
                    "internal_error": _clip(exc, 1500),
                    "backend": "openclaw",
                    "execution_lane": "openclaw_hybrid",
                    "runner": "openclaw",
                    "routing_hint": routing_hint,
                    "collaboration_mode": collaboration_mode,
                    "visibility_mode": visibility_mode,
                    "preferred_model_family": preferred_model_family,
                    "preferred_model_label": preferred_model_label,
                },
            )
            return
        payload = {
            "summary": str(result.get("summary") or ""),
            "user_summary": str(result.get("user_summary") or result.get("summary") or ""),
            "backend": "openclaw",
            "execution_lane": "openclaw_hybrid",
            "runner": "openclaw",
            "delegated_to_cursor": bool(result.get("delegated_to_cursor")),
            "openclaw_run_id": _clip(result.get("openclaw_run_id"), 200) or None,
            "openclaw_session_id": _clip(result.get("openclaw_session_id"), 200) or None,
            "requested_openclaw_session_id": _clip(result.get("requested_session_id"), 200) or None,
            "provider": _clip(result.get("provider"), 120) or None,
            "model": _clip(result.get("model"), 200) or None,
            "cursor_agent_id": _clip(result.get("cursor_agent_id"), 200) or None,
            "agent_url": _clip(result.get("agent_url"), 1000) or None,
            "pr_url": _clip(result.get("pr_url"), 1000) or None,
            "raw_status": _clip(result.get("status"), 120) or None,
            "routing_hint": routing_hint,
            "collaboration_mode": collaboration_mode,
            "visibility_mode": visibility_mode,
            "preferred_model_family": preferred_model_family,
            "preferred_model_label": preferred_model_label,
            "collaboration_trace": result.get("collaboration_trace") or [],
            "machine_collaboration_trace": result.get("machine_collaboration_trace") or [],
            "phase_outputs": result.get("phase_outputs") or {},
            "blocked_reason": _clip(result.get("blocked_reason"), 500) or None,
            "internal_trace": _clip(result.get("internal_trace"), 4000) or None,
            "raw_text": _clip(result.get("raw_text"), 4000) or None,
        }
        phase_outputs = (
            result.get("phase_outputs")
            if isinstance(result.get("phase_outputs"), dict)
            else {}
        )
        for phase in ("plan", "critique"):
            entry = phase_outputs.get(phase)
            if not isinstance(entry, dict):
                continue
            self._append_orchestration_step(
                task_id,
                phase,
                str(entry.get("status") or "completed"),
                lane=str(entry.get("lane") or "openclaw"),
                summary=str(entry.get("summary") or ""),
                provider=str(result.get("provider") or ""),
                model=str(result.get("model") or ""),
            )
        if visibility_mode == "full":
            trace_excerpt = self._collaboration_trace_excerpt(
                result.get("collaboration_trace")
                if isinstance(result.get("collaboration_trace"), list)
                else []
            )
            progress_message = "OpenClaw completed the coordination pass."
            if result.get("delegated_to_cursor"):
                progress_message = (
                    "OpenClaw completed the coordination pass and involved Cursor for the heavier execution."
                )
            elif collaboration_mode in {"cursor_primary", "collaborative"}:
                progress_message = (
                    "OpenClaw completed the coordination pass, but Andrea may still escalate to Cursor to honor the collaboration request."
                )
            if trace_excerpt:
                progress_message += f" Trace: {trace_excerpt}"
            self._append_task_event(
                task_id,
                EventType.JOB_PROGRESS,
                {
                    "message": progress_message,
                    "backend": "openclaw",
                    "execution_lane": "openclaw_hybrid",
                    "runner": "openclaw",
                    "provider": _clip(result.get("provider"), 120) or None,
                    "model": _clip(result.get("model"), 200) or None,
                    "routing_hint": routing_hint,
                    "collaboration_mode": collaboration_mode,
                    "visibility_mode": visibility_mode,
                    "preferred_model_family": preferred_model_family,
                    "preferred_model_label": preferred_model_label,
                    "collaboration_trace": result.get("collaboration_trace") or [],
                    "blocked_reason": _clip(result.get("blocked_reason"), 500) or None,
                    "internal_trace": _clip(result.get("internal_trace"), 4000) or None,
                    "force_telegram_note": True,
                },
            )
        requires_cursor = collaboration_mode in {"cursor_primary", "collaborative"}
        if result.get("ok") and requires_cursor and not result.get("delegated_to_cursor"):
            execution_entry = phase_outputs.get("execution")
            execution_summary = ""
            if isinstance(execution_entry, dict):
                execution_summary = str(execution_entry.get("summary") or "")
            self._append_orchestration_step(
                task_id,
                "execution",
                "started",
                lane="cursor",
                summary=execution_summary
                or "Andrea is handing the heavier execution step to Cursor.",
                provider=str(result.get("provider") or ""),
                model=str(result.get("model") or ""),
            )
            self._append_task_event(
                task_id,
                EventType.JOB_PROGRESS,
                {
                    "message": (
                        "OpenClaw completed an initial pass, but Andrea is escalating to Cursor "
                        "to honor your collaboration request."
                    ),
                    "backend": "cursor",
                    "execution_lane": "direct_cursor",
                    "runner": "cursor",
                    "delegated_to_cursor": True,
                    "routing_hint": routing_hint,
                    "collaboration_mode": collaboration_mode,
                    "visibility_mode": visibility_mode,
                    "preferred_model_family": preferred_model_family,
                    "preferred_model_label": preferred_model_label,
                    "force_telegram_note": visibility_mode == "full",
                },
            )
            self._run_cursor_job(task_id)
            return
        if result.get("ok"):
            execution_entry = phase_outputs.get("execution")
            if isinstance(execution_entry, dict):
                self._append_orchestration_step(
                    task_id,
                    "execution",
                    str(execution_entry.get("status") or "completed"),
                    lane=str(execution_entry.get("lane") or ("cursor" if result.get("delegated_to_cursor") else "openclaw")),
                    summary=str(execution_entry.get("summary") or ""),
                    provider=str(result.get("provider") or ""),
                    model=str(result.get("model") or ""),
                )
            elif not result.get("delegated_to_cursor"):
                self._append_orchestration_step(
                    task_id,
                    "execution",
                    "completed",
                    lane="openclaw",
                    summary="OpenClaw completed the execution inside the coordination lane.",
                    provider=str(result.get("provider") or ""),
                    model=str(result.get("model") or ""),
                )
            synthesis_entry = phase_outputs.get("synthesis")
            self._append_orchestration_step(
                task_id,
                "synthesis",
                str(synthesis_entry.get("status") or "completed")
                if isinstance(synthesis_entry, dict)
                else "completed",
                lane=str(synthesis_entry.get("lane") or "openclaw")
                if isinstance(synthesis_entry, dict)
                else "openclaw",
                summary=str(synthesis_entry.get("summary") or result.get("user_summary") or result.get("summary") or "")
                if isinstance(synthesis_entry, dict)
                else str(result.get("user_summary") or result.get("summary") or ""),
                provider=str(result.get("provider") or ""),
                model=str(result.get("model") or ""),
            )
            self._append_task_event(task_id, EventType.JOB_COMPLETED, payload)
            return
        self._append_orchestration_step(
            task_id,
            "synthesis",
            "failed",
            lane="openclaw",
            summary=str(
                result.get("blocked_reason")
                or result.get("user_summary")
                or "I could not complete the final synthesis cleanly."
            ),
            provider=str(result.get("provider") or ""),
            model=str(result.get("model") or ""),
        )
        self._append_task_event(
            task_id,
            EventType.JOB_FAILED,
            {
                **payload,
                "error": "openclaw_execution_failed",
                "message": _clip(
                    result.get("internal_trace")
                    or result.get("raw_text")
                    or result.get("summary")
                    or "OpenClaw failed.",
                    1500,
                ),
                "user_safe_error": _clip(
                    result.get("blocked_reason")
                    or result.get("user_summary")
                    or result.get("summary")
                    or "I could not complete that collaboration pass cleanly.",
                    500,
                ),
                "internal_error": _clip(
                    result.get("internal_trace")
                    or result.get("raw_text")
                    or result.get("summary")
                    or "OpenClaw failed.",
                    1500,
                ),
                "visibility_mode": visibility_mode,
            },
        )

    def _run_cursor_job(self, task_id: str) -> None:
        prompt = self._extract_cursor_prompt(task_id)
        visibility_mode = self._task_visibility_mode(task_id)
        collaboration_mode = self._task_collaboration_mode(task_id)
        preferred_model_family = self._task_preferred_model_family(task_id)
        preferred_model_label = self._task_preferred_model_label(task_id)
        if not prompt:
            self._append_orchestration_step(
                task_id,
                "execution",
                "failed",
                lane="cursor",
                summary="Cursor could not start because there was no execution prompt.",
            )
            self._append_task_event(
                task_id,
                EventType.JOB_FAILED,
                {
                    "error": "missing_prompt",
                    "message": "No Telegram text was available to send to Cursor.",
                    "user_safe_error": (
                        "I could not start the Cursor execution lane because there was no "
                        "request text available to send."
                    ),
                    "visibility_mode": visibility_mode,
                    "preferred_model_family": preferred_model_family,
                    "preferred_model_label": preferred_model_label,
                },
            )
            return
        self._append_orchestration_step(task_id, "execution", "started", lane="cursor")
        try:
            created = self._create_cursor_job(prompt)
        except Exception as exc:  # noqa: BLE001
            self._append_orchestration_step(
                task_id,
                "execution",
                "failed",
                lane="cursor",
                summary="Cursor could not start the execution pass cleanly.",
            )
            self._append_task_event(
                task_id,
                EventType.JOB_FAILED,
                {
                    "error": "cursor_submit_failed",
                    "message": _clip(exc, 1500),
                    "user_safe_error": (
                        "I could not start the Cursor execution lane cleanly on this pass."
                    ),
                    "internal_error": _clip(exc, 1500),
                    "visibility_mode": visibility_mode,
                    "preferred_model_family": preferred_model_family,
                    "preferred_model_label": preferred_model_label,
                },
            )
            return
        agent_id = _clip(created.get("agent_id"), 200)
        agent_url = _clip(created.get("agent_url"), 1000)
        pr_url = _clip(created.get("pr_url"), 1000)
        backend = _clip(created.get("backend"), 80) or "unknown"
        initial_status = _clip(created.get("status"), 80) or "submitted"
        self._append_task_event(
            task_id,
            EventType.JOB_STARTED,
            {
                "backend": backend,
                "cursor_agent_id": agent_id or None,
                "agent_url": agent_url or None,
                "pr_url": pr_url or None,
                "status": initial_status,
                "visibility_mode": visibility_mode,
                "preferred_model_family": preferred_model_family,
                "preferred_model_label": preferred_model_label,
            },
        )
        if visibility_mode == "full" and collaboration_mode in {"cursor_primary", "collaborative"}:
            self._append_task_event(
                task_id,
                EventType.JOB_PROGRESS,
                {
                    "message": (
                        "Cursor has started the heavy execution pass after the Andrea/OpenClaw coordination step."
                    ),
                    "backend": "cursor",
                    "runner": "cursor",
                    "cursor_agent_id": agent_id or None,
                    "agent_url": agent_url or None,
                    "pr_url": pr_url or None,
                    "visibility_mode": visibility_mode,
                    "preferred_model_family": preferred_model_family,
                    "preferred_model_label": preferred_model_label,
                    "force_telegram_note": True,
                },
            )
        if not agent_id:
            self._append_orchestration_step(
                task_id,
                "execution",
                "failed",
                lane="cursor",
                summary="Cursor started but did not return a usable execution handle.",
            )
            self._append_task_event(
                task_id,
                EventType.JOB_FAILED,
                {
                    "error": "missing_agent_id",
                    "message": "Cursor submission succeeded but no agent id was returned.",
                    "user_safe_error": (
                        "I started the Cursor handoff, but I did not get a usable execution handle back."
                    ),
                    "agent_url": agent_url or None,
                    "pr_url": pr_url or None,
                    "visibility_mode": visibility_mode,
                    "preferred_model_family": preferred_model_family,
                    "preferred_model_label": preferred_model_label,
                },
            )
            return
        latest_status = initial_status
        latest_response: Dict[str, Any] = {}
        try:
            attempts = max(1, self.cursor_status_poll_attempts)
            for attempt in range(attempts):
                status_payload = self._cursor_agent_status(agent_id)
                response = (
                    status_payload.get("response")
                    if isinstance(status_payload.get("response"), dict)
                    else {}
                )
                latest_response = response
                latest_status = _clip(response.get("status"), 80) or latest_status
                target = response.get("target") if isinstance(response.get("target"), dict) else {}
                agent_url = _clip(target.get("url") or agent_url, 1000)
                pr_url = _clip(target.get("prUrl") or pr_url, 1000)
                if latest_status in TERMINAL_CURSOR_STATUSES:
                    break
                if attempt < attempts - 1 and self.cursor_status_poll_interval > 0:
                    time.sleep(self.cursor_status_poll_interval)
        except Exception as exc:  # noqa: BLE001
            self._append_orchestration_step(
                task_id,
                "execution",
                "failed",
                lane="cursor",
                summary="Cursor did not return a clean status during the execution pass.",
            )
            self._append_task_event(
                task_id,
                EventType.JOB_FAILED,
                {
                    "error": "cursor_poll_failed",
                    "message": _clip(exc, 1500),
                    "user_safe_error": (
                        "I could not get a clean status back from the Cursor execution lane."
                    ),
                    "internal_error": _clip(exc, 1500),
                    "cursor_agent_id": agent_id,
                    "agent_url": agent_url or None,
                    "pr_url": pr_url or None,
                    "visibility_mode": visibility_mode,
                    "preferred_model_family": preferred_model_family,
                    "preferred_model_label": preferred_model_label,
                },
            )
            return
        if latest_status == "FINISHED":
            summary_text = self._cursor_terminal_summary(
                agent_id, latest_status, pr_url, agent_url
            )
            self._append_orchestration_step(
                task_id,
                "execution",
                "completed",
                lane="cursor",
                summary="Cursor completed the heavy execution step.",
            )
            self._append_orchestration_step(
                task_id,
                "synthesis",
                "completed",
                lane="cursor",
                summary=summary_text,
            )
            self._append_task_event(
                task_id,
                EventType.JOB_COMPLETED,
                {
                    "summary": summary_text,
                    "cursor_agent_id": agent_id,
                    "agent_url": agent_url or None,
                    "pr_url": pr_url or None,
                    "status": latest_status,
                    "raw_status": latest_response.get("status"),
                    "visibility_mode": visibility_mode,
                    "preferred_model_family": preferred_model_family,
                    "preferred_model_label": preferred_model_label,
                },
            )
            return
        if latest_status in TERMINAL_CURSOR_STATUSES:
            self._append_orchestration_step(
                task_id,
                "execution",
                "failed",
                lane="cursor",
                summary=f"Cursor ended with status {latest_status or 'unknown'}.",
            )
            self._append_task_event(
                task_id,
                EventType.JOB_FAILED,
                {
                    "error": f"cursor_status_{latest_status.lower() or 'unknown'}",
                    "message": f"Cursor ended with status {latest_status or 'unknown'}.",
                    "user_safe_error": (
                        "Cursor did not finish the execution cleanly on this pass."
                    ),
                    "cursor_agent_id": agent_id,
                    "agent_url": agent_url or None,
                    "pr_url": pr_url or None,
                    "raw_status": latest_response.get("status"),
                    "visibility_mode": visibility_mode,
                    "preferred_model_family": preferred_model_family,
                    "preferred_model_label": preferred_model_label,
                },
            )
            return
        self._append_task_event(
            task_id,
            EventType.JOB_PROGRESS,
            {
                "message": (
                    f"Cursor is still running with status {latest_status or 'unknown'} after "
                    "the configured polling window; leaving the task in running state."
                ),
                "cursor_agent_id": agent_id,
                "agent_url": agent_url or None,
                "pr_url": pr_url or None,
                "raw_status": latest_response.get("status"),
                "visibility_mode": visibility_mode,
                "preferred_model_family": preferred_model_family,
                "preferred_model_label": preferred_model_label,
            },
        )


def make_handler(server: SyncServer) -> type:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            if os.environ.get("ANDREA_SYNC_VERBOSE", "0") == "1":
                super().log_message(fmt, *args)

        def _auth_internal(self) -> bool:
            if not server.internal_token:
                return False
            auth = self.headers.get("Authorization") or ""
            return auth == f"Bearer {server.internal_token}"

        def _request_is_loopback(self) -> bool:
            host = str((self.client_address or ("", 0))[0] or "").strip().lower()
            return host in {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}

        def _allow_sensitive_get(self) -> bool:
            return self._request_is_loopback() or self._auth_internal()

        _ADMIN_COMMAND_TYPES = frozenset(
            {
                "PublishCapabilitySnapshot",
                "RecordEvaluationFinding",
                "RunOptimizationCycle",
                "CreateOptimizationProposal",
                "ApplyOptimizationProposal",
                "RunIncidentRepair",
                "LinkPrincipalIdentity",
                "RunProactiveSweep",
                "KillSwitchEngage",
                "KillSwitchRelease",
            }
        )

        def _commands_require_internal(self, body: Dict[str, Any]) -> bool:
            ct = str(body.get("command_type") or body.get("type") or "")
            return ct in self._ADMIN_COMMAND_TYPES

        def _send(self, code: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {}

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/dashboard":
                if not self._allow_sensitive_get():
                    self._send(401, b'{"error":"unauthorized"}')
                    return
                self._send(
                    200,
                    render_dashboard_html().encode("utf-8"),
                    content_type="text/html;charset=utf-8",
                )
                return
            if path == "/v1/health":

                def health_body(c: sqlite3.Connection) -> bytes:
                    ks = kill_switch_status(c)
                    age = digest_age_seconds(c)
                    db_disp = str(server.db_path)
                    if os.environ.get("ANDREA_SYNC_HEALTH_VERBOSE", "0") != "1":
                        db_disp = Path(db_disp).name
                    return json.dumps(
                        {
                            "ok": True,
                            "service": "andrea_sync",
                            "db": db_disp,
                            "kill_switch": ks,
                            "capability_digest_age_seconds": age,
                        }
                    ).encode("utf-8")

                self._send(200, server.with_lock(health_body))
                return
            if path == "/v1/runtime-snapshot":
                if not self._allow_sensitive_get():
                    self._send(401, b'{"error":"unauthorized"}')
                    return
                webhook_snapshot = build_dashboard_webhook_snapshot(server)

                def runtime_body(c: sqlite3.Connection) -> bytes:
                    payload = {
                        "ok": True,
                        "service": "andrea_sync",
                        "runtime": build_runtime_truth_snapshot(
                            c,
                            server,
                            webhook_snapshot=webhook_snapshot,
                        ),
                    }
                    return json.dumps(payload, indent=2).encode("utf-8")

                self._send(200, server.with_lock(runtime_body))
                return
            if path == "/v1/status":
                if not self._allow_sensitive_get():
                    self._send(401, b'{"error":"unauthorized"}')
                    return
                webhook_snapshot = build_dashboard_webhook_snapshot(server)
                def status_body(c: sqlite3.Connection) -> bytes:
                    cap = get_capability_digest(c)
                    ks = kill_switch_status(c)
                    return json.dumps(
                        {
                            "ok": True,
                            "service": "andrea_sync",
                            "db": str(server.db_path),
                            "kill_switch": ks,
                            "capabilities": cap,
                            "runtime": build_runtime_truth_snapshot(
                                c,
                                server,
                                webhook_snapshot=webhook_snapshot,
                            ),
                        },
                        indent=2,
                    ).encode("utf-8")

                self._send(200, server.with_lock(status_body))
                return
            if path == "/v1/capabilities":
                if not self._allow_sensitive_get():
                    self._send(401, b'{"error":"unauthorized"}')
                    return

                def cap_body(c: sqlite3.Connection) -> bytes:
                    info = get_capability_digest(c)
                    return json.dumps(info, indent=2).encode("utf-8")

                self._send(200, server.with_lock(cap_body))
                return
            if path == "/v1/policy/skill-absence":
                qs = urllib.parse.parse_qs(parsed.query)
                sk = (qs.get("skill") or [""])[0].strip()
                if not sk:
                    self._send(400, b'{"error":"skill query param required"}')
                    return

                def pol(c: sqlite3.Connection) -> bytes:
                    raw_ttl = (qs.get("max_age_seconds") or [""])[0].strip()
                    try:
                        ttl = float(raw_ttl) if raw_ttl else 900.0
                    except ValueError:
                        ttl = 900.0
                    ev = evaluate_skill_absence_claim(c, sk, max_age_seconds=ttl)
                    return json.dumps(ev, indent=2).encode("utf-8")

                self._send(200, server.with_lock(pol))
                return
            if path == "/v1/dashboard/summary":
                if not self._allow_sensitive_get():
                    self._send(401, b'{"error":"unauthorized"}')
                    return
                raw_lim = (urllib.parse.parse_qs(parsed.query).get("limit") or ["30"])[0]
                try:
                    limit = int(raw_lim)
                except ValueError:
                    limit = 30
                limit = max(1, min(limit, 200))
                webhook_snapshot = build_dashboard_webhook_snapshot(server)

                def summary(c: sqlite3.Connection) -> bytes:
                    payload = build_dashboard_summary(
                        c,
                        server,
                        limit=limit,
                        webhook_snapshot=webhook_snapshot,
                    )
                    return json.dumps(payload, indent=2).encode("utf-8")

                self._send(200, server.with_lock(summary))
                return
            if path.startswith("/v1/tasks/") and len(path) > len("/v1/tasks/"):
                if not self._allow_sensitive_get():
                    self._send(401, b'{"error":"unauthorized"}')
                    return
                tid = path.split("/v1/tasks/", 1)[1].split("?", 1)[0].strip()
                if not tid:
                    self._send(400, b'{"error":"missing task id"}')
                    return

                def one(c: sqlite3.Connection) -> bytes:
                    ch = get_task_channel(c, tid)
                    if not ch:
                        return json.dumps({"error": "not found"}).encode("utf-8")
                    proj = project_task_dict(c, tid, ch)
                    events = [
                        {
                            "seq": s,
                            "ts": t,
                            "event_type": et,
                            "payload": p,
                        }
                        for s, t, et, p in load_events_for_task(c, tid)
                    ]
                    return json.dumps(
                        {"task": proj, "events": events}, indent=2
                    ).encode("utf-8")

                payload = server.with_lock(one)
                try:
                    obj = json.loads(payload.decode("utf-8"))
                except json.JSONDecodeError:
                    self._send(500, b'{"error":"projection failed"}')
                    return
                if obj.get("error"):
                    self._send(404, payload)
                else:
                    self._send(200, payload)
                return
            if path == "/v1/tasks":
                if not self._allow_sensitive_get():
                    self._send(401, b'{"error":"unauthorized"}')
                    return
                raw_lim = (urllib.parse.parse_qs(parsed.query).get("limit") or ["50"])[0]
                try:
                    limit = int(raw_lim)
                except ValueError:
                    limit = 50
                limit = max(1, min(limit, 500))

                def lst(c: sqlite3.Connection) -> bytes:
                    rows = list_tasks(c, limit=limit)
                    return json.dumps({"tasks": rows}, indent=2).encode("utf-8")

                self._send(200, server.with_lock(lst))
                return
            self._send(404, b'{"error":"not found"}')

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/v1/commands":
                body = self._read_json()
                if self._commands_require_internal(body) and not self._auth_internal():
                    self._send(401, b'{"error":"unauthorized"}')
                    return

                def ks_block(c: sqlite3.Connection) -> bool:
                    return is_kill_switch_engaged(c)

                if server.with_lock(ks_block):
                    ct = str(body.get("command_type") or body.get("type") or "")
                    if ct != "KillSwitchRelease":
                        self._send(
                            503,
                            b'{"ok":false,"error":"kill_switch_engaged"}',
                        )
                        return

                def run(c: sqlite3.Connection) -> bytes:
                    return json.dumps(handle_command(c, body), indent=2).encode("utf-8")

                out = server.with_lock(run)
                try:
                    result = json.loads(out.decode("utf-8"))
                except json.JSONDecodeError:
                    self._send(500, out)
                    return
                if result.get("ok") is True:
                    task_id = result.get("task_id")
                    if task_id:
                        server._queue_task_followups(str(task_id))
                    self._send(200, out)
                else:
                    err = str(result.get("error") or "").lower()
                    if "kill_switch_engaged" in err:
                        code = 503
                    elif "unknown task" in err:
                        code = 404
                    else:
                        code = 400
                    self._send(code, out)
                return
            if path == "/v1/internal/events":
                if not self._auth_internal():
                    self._send(401, b'{"error":"unauthorized"}')
                    return
                if server.with_lock(is_kill_switch_engaged):
                    self._send(
                        503,
                        b'{"ok":false,"error":"kill_switch_engaged"}',
                    )
                    return
                body = self._read_json()
                task_id = body.get("task_id")
                et = body.get("event_type")
                if not task_id or not et:
                    self._send(400, b'{"error":"task_id and event_type required"}')
                    return
                if body.get("payload") is not None and not isinstance(body.get("payload"), dict):
                    self._send(400, b'{"error":"payload must be a JSON object"}')
                    return
                payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
                from .schema import validate_event_type
                from .store import append_event, task_exists

                def append(c: sqlite3.Connection) -> bytes:
                    if not task_exists(c, str(task_id)):
                        return json.dumps({"ok": False, "error": "unknown task"}).encode(
                            "utf-8"
                        )
                    try:
                        ev = validate_event_type(str(et))
                    except ValueError as e:
                        return json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                    seq = append_event(c, str(task_id), ev, payload)
                    return json.dumps({"ok": True, "seq": seq}).encode("utf-8")

                raw = server.with_lock(append)
                try:
                    result = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._send(500, raw)
                    return
                if result.get("ok") is True:
                    server._queue_task_followups(str(task_id))
                    self._send(200, raw)
                else:
                    err = str(result.get("error") or "").lower()
                    code = 404 if "unknown" in err else 400
                    self._send(code, raw)
                return
            if path == "/v1/telegram/webhook":
                if server.with_lock(is_kill_switch_engaged):
                    self._send(
                        503,
                        b'{"ok":false,"error":"kill_switch_engaged"}',
                    )
                    return
                q = urllib.parse.parse_qs(parsed.query)
                sec = (q.get("secret") or [""])[0]
                hdr = self.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
                if not tg_adapt.verify_telegram_webhook(
                    sec,
                    hdr,
                    query_configured=server.telegram_secret,
                    header_configured=server.telegram_header_secret,
                ):
                    self._send(403, b'{"error":"forbidden"}')
                    return
                update = self._read_json()
                cmd = tg_adapt.update_to_command(update)
                if not cmd:
                    self._send(200, b'{"ok":true}')
                    return

                def run(c: sqlite3.Connection) -> Dict[str, Any]:
                    attach_continuation_if_applicable(c, cmd)
                    return handle_command(c, cmd)

                result = server.with_lock(run)
                if result.get("ok") and result.get("task_id"):
                    server._queue_task_followups(str(result["task_id"]))
                    self._send(200, b'{"ok":true}')
                    return
                self._send(500, b'{"ok":false,"error":"telegram_update_not_persisted"}')
                return
            if path == "/v1/alexa":
                if server.with_lock(is_kill_switch_engaged):
                    self._send(
                        503,
                        b'{"ok":false,"error":"kill_switch_engaged"}',
                    )
                    return
                if server.alexa_edge_token:
                    auth = self.headers.get("Authorization") or ""
                    edge = self.headers.get("X-Andrea-Alexa-Edge-Token") or ""
                    expected = server.alexa_edge_token
                    if auth != f"Bearer {expected}" and edge != expected:
                        self._send(401, b'{"error":"unauthorized"}')
                        return
                try:
                    length = int(self.headers.get("Content-Length") or "0")
                except ValueError:
                    self._send(400, b'{"error":"invalid_content_length"}')
                    return
                if length < 0:
                    self._send(400, b'{"error":"invalid_content_length"}')
                    return
                if length > 262144:
                    self._send(413, b'{"error":"body_too_large"}')
                    return
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    verify_alexa_http_request(
                        raw,
                        dict(self.headers),
                        expected_application_id=server.alexa_skill_id,
                    )
                except ValueError as exc:
                    structured_log("alexa_verify_failed", error=str(exc))
                    metric_log("alexa_verify_failed")
                    self._send(
                        400,
                        json.dumps({"error": "alexa_verify_failed"}).encode("utf-8"),
                    )
                    return
                except RuntimeError as exc:
                    structured_log("alexa_verify_misconfig", error=str(exc))
                    metric_log("alexa_verify_misconfig")
                    self._send(
                        500,
                        json.dumps({"error": "alexa_verify_misconfig"}).encode("utf-8"),
                    )
                    return
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send(400, b'{"error":"invalid_json"}')
                    return
                if not isinstance(body, dict):
                    self._send(400, b'{"error":"invalid_json"}')
                    return
                resp = server._process_alexa_request(body)
                out = alexa_adapt.build_response_json(resp)
                self._send(200, out, content_type="application/json;charset=utf-8")
                return
            self._send(404, b'{"error":"not found"}')

    return Handler


def serve_forever(host: str = "127.0.0.1", port: Optional[int] = None) -> None:
    p = port or int(os.environ.get("ANDREA_SYNC_PORT", "8765"))
    srv_state = SyncServer()
    handler = make_handler(srv_state)
    httpd = ThreadingHTTPServer((host, p), handler)
    print(f"andrea_sync listening on http://{host}:{p} db={srv_state.db_path}", flush=True)
    httpd.serve_forever()


def main() -> None:
    serve_forever()


if __name__ == "__main__":
    main()

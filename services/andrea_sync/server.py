"""HTTP server for Andrea lockstep (local-first)."""
from __future__ import annotations

import json
import os
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
from .andrea_router import route_message
from .bus import handle_command
from .kill_switch import is_kill_switch_engaged, kill_switch_status
from .policy import digest_age_seconds, evaluate_skill_absence_claim, get_capability_digest
from .projector import project_task_dict
from .schema import EventType
from .store import (
    append_event,
    connect,
    ensure_system_task,
    get_meta,
    get_task_channel,
    list_tasks,
    load_events_for_task,
    load_recent_telegram_history,
    migrate,
    set_meta,
)
from .telegram_format import (
    format_ack_message,
    format_direct_message,
    format_final_message,
    format_running_message,
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


class SyncServer:
    def __init__(self) -> None:
        self.repo_root = _repo_root()
        self.db_path = default_db_path()
        self.conn = connect(self.db_path)
        migrate(self.conn)
        ensure_system_task(self.conn)
        self.lock = threading.Lock()
        self._routing_inflight: set[str] = set()
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
        self.background_enabled = _env_bool("ANDREA_SYNC_BACKGROUND_ENABLED", True)
        self.telegram_auto_cursor = _env_bool("ANDREA_SYNC_TELEGRAM_AUTO_CURSOR", True)
        self.telegram_notifier_enabled = _env_bool("ANDREA_SYNC_TELEGRAM_NOTIFIER", True)
        self.telegram_delegate_lane = (
            os.environ.get("ANDREA_TELEGRAM_DELEGATE_LANE", "openclaw_hybrid").strip().lower()
            or "openclaw_hybrid"
        )
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
        if self.telegram_webhook_autofix and self.telegram_bot_token and self.telegram_public_base:
            self._webhook_worker = threading.Thread(
                target=self._maintain_telegram_webhook,
                daemon=True,
            )
            self._webhook_worker.start()

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
                if current_url != expected_url:
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

        def already_sent(c: sqlite3.Connection) -> bool:
            return get_meta(c, marker) is not None

        if self.with_lock(already_sent):
            return
        tg_adapt.send_text_message(
            bot_token=self.telegram_bot_token,
            chat_id=chat_id,
            text=text,
            reply_to_message_id=telegram_meta.get("message_id"),
        )

        def mark(c: sqlite3.Connection) -> None:
            set_meta(c, marker, str(time.time()))

        self.with_lock(mark)

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

    def _projection_meta(self, projection: Dict[str, Any], key: str) -> Dict[str, Any]:
        meta = projection.get("meta", {})
        if not isinstance(meta, dict):
            return {}
        section = meta.get(key, {})
        return section if isinstance(section, dict) else {}

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
        return str(telegram_meta.get("routing_hint") or "auto").strip() or "auto"

    def _task_collaboration_mode(self, task_id: str) -> str:
        snapshot = self._task_snapshot(task_id)
        if not snapshot:
            return "auto"
        execution_meta = self._projection_meta(snapshot["projection"], "execution")
        if execution_meta.get("collaboration_mode"):
            return str(execution_meta.get("collaboration_mode")).strip()
        telegram_meta = self._projection_meta(snapshot["projection"], "telegram")
        return str(telegram_meta.get("collaboration_mode") or "auto").strip() or "auto"

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

    def _queue_task_followups(self, task_id: str) -> None:
        if not task_id:
            return
        self.queue.put(lambda: self._handle_task_followups(task_id))

    def _handle_task_followups(self, task_id: str) -> None:
        snapshot = self._task_snapshot(task_id)
        if not snapshot or snapshot["channel"] != "telegram":
            return
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
            try:
                self._send_telegram_message_once(
                    task_id,
                    "ack",
                    format_ack_message(
                        task_id,
                        worker_label=worker_label,
                        routing_hint=str(telegram_meta.get("routing_hint") or ""),
                        collaboration_mode=str(
                            self._projection_meta(projection, "execution").get("collaboration_mode")
                            or telegram_meta.get("collaboration_mode")
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
            try:
                self._send_telegram_message_once(
                    task_id,
                    "started",
                    format_running_message(
                        task_id,
                        agent_url=str(agent_url or ""),
                        worker_label=self._telegram_worker_label(projection, running=True),
                        delegated_to_cursor=bool(execution_meta.get("delegated_to_cursor")),
                        routing_hint=str(execution_meta.get("routing_hint") or ""),
                        collaboration_mode=str(execution_meta.get("collaboration_mode") or ""),
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"andrea_sync telegram running update error: {exc}", flush=True)
            return
        if status in {"completed", "failed"}:
            try:
                self._send_telegram_message_once(
                    task_id,
                    "final",
                    format_final_message(
                        task_id,
                        status=status,
                        summary=str(projection.get("summary") or ""),
                        pr_url=str(cursor_meta.get("pr_url") or ""),
                        agent_url=str(cursor_meta.get("agent_url") or ""),
                        last_error=str(projection.get("last_error") or ""),
                        worker_label=self._telegram_worker_label(projection),
                        delegated_to_cursor=bool(execution_meta.get("delegated_to_cursor")),
                        backend=str(execution_meta.get("backend") or ""),
                        openclaw_session_id=str(
                            self._projection_meta(projection, "openclaw").get("session_id") or ""
                        ),
                        routing_hint=str(execution_meta.get("routing_hint") or ""),
                        collaboration_mode=str(execution_meta.get("collaboration_mode") or ""),
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"andrea_sync telegram final update error: {exc}", flush=True)

    def _route_telegram_task(self, task_id: str) -> None:
        marker = self._meta_key("route_applied", task_id)
        if not self._claim_routing_attempt(task_id):
            return
        try:
            user_payload = self._latest_user_message_payload(task_id)
            prompt = self._extract_cursor_prompt(task_id)
            decision = route_message(
                prompt,
                history=self._recent_telegram_history(task_id),
                routing_hint=str(user_payload.get("routing_hint") or "auto"),
                collaboration_mode=str(user_payload.get("collaboration_mode") or "auto"),
            )
            if decision.mode == "delegate":
                execution_lane = decision.delegate_target or self.telegram_delegate_lane
                kind = "openclaw" if execution_lane == "openclaw_hybrid" else "cursor"
                applied = self._append_task_event(
                    task_id,
                    EventType.JOB_QUEUED,
                    {
                        "kind": kind,
                        "prompt_excerpt": prompt[:300],
                        "source": "telegram_balanced_delegate",
                        "route_reason": decision.reason,
                        "execution_lane": execution_lane,
                        "runner": "openclaw" if kind == "openclaw" else "cursor",
                        "routing_hint": str(user_payload.get("routing_hint") or "auto"),
                        "collaboration_mode": decision.collaboration_mode,
                        "mention_targets": user_payload.get("mention_targets", []),
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
        finally:
            self._finish_routing_attempt(task_id)

    def _schedule_cursor_execution(self, task_id: str) -> None:
        if not self.background_enabled or not self.telegram_auto_cursor:
            return
        marker = self._meta_key("executor_started", task_id)

        def claim(c: sqlite3.Connection) -> bool:
            if get_meta(c, marker) is not None:
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

    def _extract_cursor_prompt(self, task_id: str) -> str:
        payload = self._latest_user_message_payload(task_id)
        prompt = str(payload.get("routing_text") or payload.get("text") or "").strip()
        if prompt:
            return prompt
        snapshot = self._task_snapshot(task_id)
        if not snapshot:
            return ""
        return str(snapshot["projection"].get("summary") or "").strip()

    def _create_openclaw_job(
        self,
        task_id: str,
        prompt: str,
        route_reason: str,
        collaboration_mode: str,
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
                "--route-reason",
                route_reason,
                "--collaboration-mode",
                collaboration_mode,
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
        execution_lane = self._task_execution_lane(task_id)
        if execution_lane == "openclaw_hybrid":
            self._run_openclaw_job(task_id)
            return
        self._run_cursor_job(task_id)

    def _run_openclaw_job(self, task_id: str) -> None:
        prompt = self._extract_cursor_prompt(task_id)
        collaboration_mode = self._task_collaboration_mode(task_id)
        routing_hint = self._task_routing_hint(task_id)
        if not prompt:
            self._append_task_event(
                task_id,
                EventType.JOB_FAILED,
                {
                    "error": "missing_prompt",
                    "message": "No Telegram text was available to send to OpenClaw.",
                    "backend": "openclaw",
                    "execution_lane": "openclaw_hybrid",
                },
            )
            return
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
            },
        )
        try:
            result = self._create_openclaw_job(
                task_id,
                prompt,
                self._task_route_reason(task_id),
                collaboration_mode,
            )
        except Exception as exc:  # noqa: BLE001
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
                    "backend": "openclaw",
                    "execution_lane": "openclaw_hybrid",
                    "runner": "openclaw",
                    "routing_hint": routing_hint,
                    "collaboration_mode": collaboration_mode,
                },
            )
            return
        payload = {
            "summary": str(result.get("summary") or ""),
            "backend": "openclaw",
            "execution_lane": "openclaw_hybrid",
            "runner": "openclaw",
            "delegated_to_cursor": bool(result.get("delegated_to_cursor")),
            "openclaw_run_id": _clip(result.get("openclaw_run_id"), 200) or None,
            "openclaw_session_id": _clip(result.get("openclaw_session_id"), 200) or None,
            "provider": _clip(result.get("provider"), 120) or None,
            "model": _clip(result.get("model"), 200) or None,
            "cursor_agent_id": _clip(result.get("cursor_agent_id"), 200) or None,
            "agent_url": _clip(result.get("agent_url"), 1000) or None,
            "pr_url": _clip(result.get("pr_url"), 1000) or None,
            "raw_status": _clip(result.get("status"), 120) or None,
            "routing_hint": routing_hint,
            "collaboration_mode": collaboration_mode,
        }
        requires_cursor = collaboration_mode in {"cursor_primary", "collaborative"}
        if result.get("ok") and requires_cursor and not result.get("delegated_to_cursor"):
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
                },
            )
            self._run_cursor_job(task_id)
            return
        if result.get("ok"):
            self._append_task_event(task_id, EventType.JOB_COMPLETED, payload)
            return
        self._append_task_event(
            task_id,
            EventType.JOB_FAILED,
            {
                **payload,
                "error": "openclaw_execution_failed",
                "message": _clip(result.get("summary") or result.get("raw_text") or "OpenClaw failed.", 1500),
            },
        )

    def _run_cursor_job(self, task_id: str) -> None:
        prompt = self._extract_cursor_prompt(task_id)
        if not prompt:
            self._append_task_event(
                task_id,
                EventType.JOB_FAILED,
                {"error": "missing_prompt", "message": "No Telegram text was available to send to Cursor."},
            )
            return
        try:
            created = self._create_cursor_job(prompt)
        except Exception as exc:  # noqa: BLE001
            self._append_task_event(
                task_id,
                EventType.JOB_FAILED,
                {"error": "cursor_submit_failed", "message": _clip(exc, 1500)},
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
            },
        )
        if not agent_id:
            self._append_task_event(
                task_id,
                EventType.JOB_FAILED,
                {
                    "error": "missing_agent_id",
                    "message": "Cursor submission succeeded but no agent id was returned.",
                    "agent_url": agent_url or None,
                    "pr_url": pr_url or None,
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
            self._append_task_event(
                task_id,
                EventType.JOB_FAILED,
                {
                    "error": "cursor_poll_failed",
                    "message": _clip(exc, 1500),
                    "cursor_agent_id": agent_id,
                    "agent_url": agent_url or None,
                    "pr_url": pr_url or None,
                },
            )
            return
        if latest_status == "FINISHED":
            self._append_task_event(
                task_id,
                EventType.JOB_COMPLETED,
                {
                    "summary": self._cursor_terminal_summary(
                        agent_id, latest_status, pr_url, agent_url
                    ),
                    "cursor_agent_id": agent_id,
                    "agent_url": agent_url or None,
                    "pr_url": pr_url or None,
                    "status": latest_status,
                    "raw_status": latest_response.get("status"),
                },
            )
            return
        if latest_status in TERMINAL_CURSOR_STATUSES:
            self._append_task_event(
                task_id,
                EventType.JOB_FAILED,
                {
                    "error": f"cursor_status_{latest_status.lower() or 'unknown'}",
                    "message": f"Cursor ended with status {latest_status or 'unknown'}.",
                    "cursor_agent_id": agent_id,
                    "agent_url": agent_url or None,
                    "pr_url": pr_url or None,
                    "raw_status": latest_response.get("status"),
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

        _ADMIN_COMMAND_TYPES = frozenset(
            {
                "PublishCapabilitySnapshot",
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
            if path == "/v1/health":

                def health_body(c: sqlite3.Connection) -> bytes:
                    ks = kill_switch_status(c)
                    age = digest_age_seconds(c)
                    return json.dumps(
                        {
                            "ok": True,
                            "service": "andrea_sync",
                            "db": str(server.db_path),
                            "kill_switch": ks,
                            "capability_digest_age_seconds": age,
                        }
                    ).encode("utf-8")

                self._send(200, server.with_lock(health_body))
                return
            if path == "/v1/status":
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
                        },
                        indent=2,
                    ).encode("utf-8")

                self._send(200, server.with_lock(status_body))
                return
            if path == "/v1/capabilities":

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
            if path.startswith("/v1/tasks/") and len(path) > len("/v1/tasks/"):
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

                def work() -> None:
                    cmd = tg_adapt.update_to_command(update)
                    if not cmd:
                        return

                    def run(c: sqlite3.Connection) -> Dict[str, Any]:
                        return handle_command(c, cmd)

                    result = server.with_lock(run)
                    if result.get("ok") and result.get("task_id"):
                        server._handle_task_followups(str(result["task_id"]))

                server.queue.put(work)
                self._send(200, b'{"ok":true}')
                return
            if path == "/v1/alexa":
                if server.with_lock(is_kill_switch_engaged):
                    self._send(
                        503,
                        b'{"ok":false,"error":"kill_switch_engaged"}',
                    )
                    return
                body = self._read_json()
                cmd, resp = alexa_adapt.parse_alexa_body(body)

                def work() -> None:
                    if not cmd:
                        return

                    def run(c: sqlite3.Connection) -> None:
                        handle_command(c, cmd)

                    server.with_lock(run)

                server.queue.put(work)
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

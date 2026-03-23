"""Deterministic experience assurance runner for Andrea lockstep."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence
from unittest import mock

from .bus import handle_command
from .experience_types import (
    ExperienceCheckResult,
    ExperienceObservation,
    ExperienceRun,
    ExperienceScenario,
    new_experience_run_id,
)
from .projector import project_task_dict
from .repair_orchestrator import run_incident_repair_cycle
from .schema import CommandType, EventType, TaskStatus
from .server import SyncServer, make_handler
from .store import (
    connect,
    ensure_system_task,
    migrate,
    save_experience_run,
)
from .user_surface import is_internal_runtime_text, sanitize_user_surface_text

REPO_ROOT = Path(__file__).resolve().parents[2]


def _clip(value: Any, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _obs(
    description: str,
    *,
    expected: str,
    observed: Any,
    passed: bool,
    issue_code: str = "",
    severity: str = "medium",
) -> ExperienceObservation:
    return ExperienceObservation(
        description=description,
        expected=expected,
        observed=observed,
        passed=bool(passed),
        issue_code=str(issue_code or "").strip(),
        severity=str(severity or "medium").strip(),
    )


class ExperienceHarness:
    """Temporary in-process lockstep server for deterministic experience checks."""

    def __init__(self) -> None:
        self._stack = ExitStack()
        self._db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self._db_file.name)
        self._db_file.close()
        self.server: SyncServer | None = None
        self.httpd: ThreadingHTTPServer | None = None
        self.port = 0
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "ExperienceHarness":
        env_patch = {
            "ANDREA_SYNC_DB": str(self.db_path),
            "ANDREA_SYNC_TELEGRAM_SECRET": "experience-secret",
            "ANDREA_SYNC_TELEGRAM_WEBHOOK_SECRET": "experience-secret",
            "ANDREA_SYNC_INTERNAL_TOKEN": "experience-internal-token",
            "ANDREA_SYNC_BACKGROUND_ENABLED": "0",
            "ANDREA_SYNC_BACKGROUND_OPTIMIZER_ENABLED": "0",
            "ANDREA_SYNC_BACKGROUND_INCIDENT_REPAIR_ENABLED": "0",
            "ANDREA_SYNC_PROACTIVE_SWEEP_ENABLED": "0",
            "ANDREA_SYNC_TELEGRAM_NOTIFIER": "0",
            "ANDREA_SYNC_TELEGRAM_WEBHOOK_AUTOFIX": "0",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "ANDREA_SYNC_PUBLIC_BASE": "",
            "OPENAI_API_ENABLED": "0",
        }
        self._stack.enter_context(mock.patch.dict(os.environ, env_patch, clear=False))
        self.server = SyncServer()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.server))
        self.port = int(self.httpd.server_address[1])
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.server is not None:
            self.server.conn.close()
        self._stack.close()
        self.db_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{self.db_path}{suffix}").unlink(missing_ok=True)

    @property
    def conn(self):  # type: ignore[override]
        assert self.server is not None
        return self.server.conn

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def json_request(
        self,
        method: str,
        path: str,
        *,
        body: Dict[str, Any] | None = None,
        headers: Dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        payload = json.dumps(body or {}).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.url(path),
            data=payload,
            method=method,
            headers=headers or {},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}

    def submit_telegram_update(self, update: Dict[str, Any]) -> None:
        body = json.dumps(update).encode("utf-8")
        req = urllib.request.Request(
            self.url("/v1/telegram/webhook"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "experience-secret",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                raise RuntimeError(f"telegram webhook returned {resp.status}")

    def list_tasks(self, *, limit: int = 30) -> List[Dict[str, Any]]:
        payload = self.json_request("GET", f"/v1/tasks?limit={int(limit)}")
        tasks = payload.get("tasks")
        return list(tasks) if isinstance(tasks, list) else []

    def load_task_detail(self, task_id: str) -> Dict[str, Any]:
        return self.json_request("GET", f"/v1/tasks/{urllib.parse.quote(task_id)}")

    def wait_for_telegram_task(
        self,
        *,
        message_id: int,
        statuses: Iterable[str],
        attempts: int = 40,
        delay_seconds: float = 0.05,
    ) -> Dict[str, Any]:
        desired = {str(item) for item in statuses}
        detail: Dict[str, Any] = {}
        for _ in range(max(1, int(attempts))):
            tasks = [row for row in self.list_tasks(limit=40) if str(row.get("channel") or "") == "telegram"]
            for task in tasks:
                candidate = self.load_task_detail(str(task.get("task_id") or ""))
                meta = candidate.get("task", {}).get("meta", {})
                telegram = meta.get("telegram") if isinstance(meta.get("telegram"), dict) else {}
                if int(telegram.get("message_id") or 0) == int(message_id):
                    detail = candidate
                    break
            if detail and str(detail.get("task", {}).get("status") or "") in desired:
                return detail
            time.sleep(max(0.01, float(delay_seconds)))
        if detail:
            return detail
        raise RuntimeError(f"Unable to find telegram task for message_id={message_id}")

    def publish_capability_snapshot(self, rows: List[Dict[str, Any]], *, summary: Dict[str, Any]) -> Dict[str, Any]:
        return self.json_request(
            "POST",
            "/v1/commands",
            body={
                "command_type": "PublishCapabilitySnapshot",
                "channel": "internal",
                "payload": {
                    "rows": rows,
                    "summary": summary,
                },
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer experience-internal-token",
            },
        )

    def skill_absence(self, skill: str) -> Dict[str, Any]:
        return self.json_request("GET", f"/v1/policy/skill-absence?skill={urllib.parse.quote(skill)}")


def _task_last_reply(detail: Dict[str, Any]) -> str:
    meta = detail.get("task", {}).get("meta", {})
    assistant = meta.get("assistant") if isinstance(meta.get("assistant"), dict) else {}
    return str(assistant.get("last_reply") or "").strip()


def _task_route(detail: Dict[str, Any]) -> str:
    meta = detail.get("task", {}).get("meta", {})
    assistant = meta.get("assistant") if isinstance(meta.get("assistant"), dict) else {}
    return str(assistant.get("route") or "").strip()


def _task_has_cursor_meta(detail: Dict[str, Any]) -> bool:
    meta = detail.get("task", {}).get("meta", {})
    return bool(meta.get("cursor"))


def _task_event_types(detail: Dict[str, Any]) -> List[str]:
    rows = detail.get("events") if isinstance(detail.get("events"), list) else []
    out: List[str] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(str(row.get("event_type") or ""))
    return out


def _run_direct_meta_scenario(
    harness: ExperienceHarness,
    scenario: ExperienceScenario,
    *,
    text: str,
    update_id: int,
    message_id: int,
    reply_keyword: str = "",
) -> ExperienceCheckResult:
    started = time.time()
    harness.submit_telegram_update(
        {
            "update_id": update_id,
            "message": {
                "text": text,
                "message_id": message_id,
                "chat": {"id": update_id + 100},
                "from": {"id": update_id + 200},
            },
        }
    )
    detail = harness.wait_for_telegram_task(
        message_id=message_id,
        statuses=(TaskStatus.COMPLETED.value,),
    )
    task = detail.get("task", {})
    reply = _task_last_reply(detail)
    event_types = _task_event_types(detail)
    observations = [
        _obs(
            "task completed directly",
            expected=TaskStatus.COMPLETED.value,
            observed=task.get("status"),
            passed=str(task.get("status") or "") == TaskStatus.COMPLETED.value,
            issue_code="direct_reply_regression",
        ),
        _obs(
            "assistant route stays direct",
            expected="direct",
            observed=_task_route(detail),
            passed=_task_route(detail) == "direct",
            issue_code="overdelegated_meta_question",
        ),
        _obs(
            "no cursor metadata for meta question",
            expected="cursor metadata absent",
            observed=_task_has_cursor_meta(detail),
            passed=not _task_has_cursor_meta(detail),
            issue_code="overdelegated_meta_question",
        ),
        _obs(
            "no queued delegation lifecycle event",
            expected="JobQueued absent",
            observed="JobQueued" in event_types,
            passed=EventType.JOB_QUEUED.value not in event_types,
            issue_code="overdelegated_meta_question",
        ),
        _obs(
            "reply stays calm and free of runtime jargon",
            expected="no internal runtime text",
            observed=reply,
            passed=bool(reply) and not is_internal_runtime_text(reply),
            issue_code="runtime_jargon_leaked",
        ),
    ]
    if reply_keyword:
        observations.append(
            _obs(
                "reply addresses the requested concept",
                expected=f"reply mentions {reply_keyword}",
                observed=reply,
                passed=reply_keyword.lower() in reply.lower(),
                issue_code="direct_reply_regression",
            )
        )
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=reply or json.dumps(detail.get("task", {}), ensure_ascii=False),
        metadata={"task_id": task.get("task_id"), "event_types": event_types},
        started_at=started,
        completed_at=time.time(),
    )


def _scenario_is_this_openclaw(harness: ExperienceHarness, scenario: ExperienceScenario) -> ExperienceCheckResult:
    return _run_direct_meta_scenario(
        harness,
        scenario,
        text="Is this OpenClaw?",
        update_id=501,
        message_id=901,
        reply_keyword="openclaw",
    )


def _scenario_what_llm_is_answering(harness: ExperienceHarness, scenario: ExperienceScenario) -> ExperienceCheckResult:
    return _run_direct_meta_scenario(
        harness,
        scenario,
        text="What LLM is answering?",
        update_id=502,
        message_id=902,
        reply_keyword="andrea",
    )


def _scenario_cursor_primary(harness: ExperienceHarness, scenario: ExperienceScenario) -> ExperienceCheckResult:
    started = time.time()
    harness.submit_telegram_update(
        {
            "update_id": 503,
            "message": {
                "text": "@Cursor please fix the failing tests",
                "message_id": 903,
                "chat": {"id": 603},
                "from": {"id": 703},
            },
        }
    )
    detail = harness.wait_for_telegram_task(
        message_id=903,
        statuses=(TaskStatus.QUEUED.value, TaskStatus.RUNNING.value),
    )
    meta = detail.get("task", {}).get("meta", {})
    telegram = meta.get("telegram") if isinstance(meta.get("telegram"), dict) else {}
    execution = meta.get("execution") if isinstance(meta.get("execution"), dict) else {}
    observations = [
        _obs(
            "task enters delegated queue",
            expected=TaskStatus.QUEUED.value,
            observed=detail.get("task", {}).get("status"),
            passed=str(detail.get("task", {}).get("status") or "") in {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value},
            issue_code="cursor_primary_regression",
        ),
        _obs(
            "routing hint favors Cursor",
            expected="cursor",
            observed=telegram.get("routing_hint"),
            passed=str(telegram.get("routing_hint") or "") == "cursor",
            issue_code="cursor_primary_regression",
        ),
        _obs(
            "execution collaboration mode is cursor_primary",
            expected="cursor_primary",
            observed=execution.get("collaboration_mode"),
            passed=str(execution.get("collaboration_mode") or "") == "cursor_primary",
            issue_code="cursor_primary_regression",
        ),
        _obs(
            "requested capability is cursor_execution",
            expected="cursor_execution",
            observed=telegram.get("requested_capability"),
            passed=str(telegram.get("requested_capability") or "") == "cursor_execution",
            issue_code="requested_capability_mismatch",
        ),
    ]
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=json.dumps(detail.get("task", {}).get("meta", {}), ensure_ascii=False),
        metadata={"task_id": detail.get("task", {}).get("task_id")},
        started_at=started,
        completed_at=time.time(),
    )


def _scenario_bluebubbles_truth(harness: ExperienceHarness, scenario: ExperienceScenario) -> ExperienceCheckResult:
    started = time.time()
    harness.publish_capability_snapshot(
        [
            {
                "id": "skill:bluebubbles",
                "detail": "bluebubbles",
                "status": "ready",
                "availability": "verified_available",
                "aliases": ["blue bubbles", "imessage", "text messages"],
                "notes": "BlueBubbles is ready for outbound messaging.",
            }
        ],
        summary={"ready": 1, "ready_with_limits": 0, "blocked": 0},
    )
    payload = harness.skill_absence("blue bubbles")
    matches = payload.get("matches") if isinstance(payload.get("matches"), list) else []
    observations = [
        _obs(
            "ready BlueBubbles cannot be claimed absent",
            expected="may_claim_absent false",
            observed=payload.get("may_claim_absent"),
            passed=bool(payload.get("may_claim_absent")) is False,
            issue_code="blocked_capability",
        ),
        _obs(
            "policy reason confirms verified skill readiness",
            expected="verify_before_deny:skill_ready",
            observed=payload.get("reason"),
            passed=str(payload.get("reason") or "") == "verify_before_deny:skill_ready",
            issue_code="capability_truth_regression",
        ),
        _obs(
            "policy returns matching bluebubbles row",
            expected="at least one skill match",
            observed=len(matches),
            passed=bool(matches),
            issue_code="capability_truth_regression",
        ),
    ]
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=json.dumps(payload, ensure_ascii=False),
        metadata={"matches": matches[:2]},
        started_at=started,
        completed_at=time.time(),
    )


def _scenario_notes_followup(harness: ExperienceHarness, scenario: ExperienceScenario) -> ExperienceCheckResult:
    started = time.time()
    result = handle_command(
        harness.server.conn,
        {
            "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
            "channel": "telegram",
            "external_id": "experience-notes",
            "payload": {
                "text": "remember that I prefer full dialogue for repo work",
                "routing_text": "remember that I prefer full dialogue for repo work",
                "chat_id": 1,
                "message_id": 904,
                "from_user": 12,
            },
        },
    )
    with mock.patch.object(
        harness.server,
        "_resolve_runtime_skill",
        return_value={"truth": {"status": "verified_available"}},
    ):
        harness.server._handle_task_followups(result["task_id"])
    proj = project_task_dict(harness.server.conn, result["task_id"], "telegram")
    meta = proj.get("meta") if isinstance(proj.get("meta"), dict) else {}
    assistant = meta.get("assistant") if isinstance(meta.get("assistant"), dict) else {}
    identity = meta.get("identity") if isinstance(meta.get("identity"), dict) else {}
    reply = str(assistant.get("last_reply") or "")
    observations = [
        _obs(
            "memory note flow completes",
            expected=TaskStatus.COMPLETED.value,
            observed=proj.get("status"),
            passed=str(proj.get("status") or "") == TaskStatus.COMPLETED.value,
            issue_code="capability_truth_regression",
        ),
        _obs(
            "assistant reason reflects principal memory save",
            expected="principal_memory_saved",
            observed=assistant.get("reason"),
            passed=str(assistant.get("reason") or "") == "principal_memory_saved",
            issue_code="capability_truth_regression",
        ),
        _obs(
            "reply confirms Apple Notes verified lane",
            expected="reply contains Apple Notes lane is verified",
            observed=reply,
            passed="apple notes lane is verified" in reply.lower(),
            issue_code="capability_truth_regression",
        ),
        _obs(
            "reply stays free of runtime jargon",
            expected="no internal runtime text",
            observed=reply,
            passed=bool(reply) and not is_internal_runtime_text(reply),
            issue_code="runtime_jargon_leaked",
        ),
        _obs(
            "principal memory count increments",
            expected="memory_count >= 1",
            observed=identity.get("memory_count"),
            passed=int(identity.get("memory_count") or 0) >= 1,
            issue_code="capability_truth_regression",
        ),
    ]
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=reply,
        metadata={"task_id": result["task_id"], "principal_id": identity.get("principal_id")},
        started_at=started,
        completed_at=time.time(),
    )


def _scenario_reminders_followup(harness: ExperienceHarness, scenario: ExperienceScenario) -> ExperienceCheckResult:
    started = time.time()
    result = handle_command(
        harness.server.conn,
        {
            "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
            "channel": "telegram",
            "external_id": "experience-reminders",
            "payload": {
                "text": "Remind me to review the StoryLiner repo tomorrow morning.",
                "routing_text": "remind me to review the StoryLiner repo tomorrow morning",
                "chat_id": 1,
                "message_id": 905,
                "from_user": 11,
            },
        },
    )
    with mock.patch.object(
        harness.server,
        "_resolve_runtime_skill",
        return_value={"truth": {"status": "verified_available"}},
    ):
        harness.server._handle_task_followups(result["task_id"])
    proj = project_task_dict(harness.server.conn, result["task_id"], "telegram")
    meta = proj.get("meta") if isinstance(proj.get("meta"), dict) else {}
    assistant = meta.get("assistant") if isinstance(meta.get("assistant"), dict) else {}
    proactive = meta.get("proactive") if isinstance(meta.get("proactive"), dict) else {}
    outcome = meta.get("outcome") if isinstance(meta.get("outcome"), dict) else {}
    reply = str(assistant.get("last_reply") or "")
    observations = [
        _obs(
            "reminder flow completes",
            expected=TaskStatus.COMPLETED.value,
            observed=proj.get("status"),
            passed=str(proj.get("status") or "") == TaskStatus.COMPLETED.value,
            issue_code="capability_truth_regression",
        ),
        _obs(
            "assistant reason reflects reminder creation",
            expected="reminder_created",
            observed=assistant.get("reason"),
            passed=str(assistant.get("reason") or "") == "reminder_created",
            issue_code="capability_truth_regression",
        ),
        _obs(
            "reply confirms Apple Reminders verified lane",
            expected="reply contains Apple Reminders lane is verified",
            observed=reply,
            passed="apple reminders lane is verified" in reply.lower(),
            issue_code="capability_truth_regression",
        ),
        _obs(
            "reply stays free of runtime jargon",
            expected="no internal runtime text",
            observed=reply,
            passed=bool(reply) and not is_internal_runtime_text(reply),
            issue_code="runtime_jargon_leaked",
        ),
        _obs(
            "pending reminder count increments",
            expected="pending_reminder_count >= 1",
            observed=proactive.get("pending_reminder_count") or outcome.get("pending_reminder_count"),
            passed=int(proactive.get("pending_reminder_count") or outcome.get("pending_reminder_count") or 0) >= 1,
            issue_code="capability_truth_regression",
        ),
    ]
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=reply,
        metadata={"task_id": result["task_id"]},
        started_at=started,
        completed_at=time.time(),
    )


def _scenario_runtime_leak_sanitization(
    _harness: ExperienceHarness,
    scenario: ExperienceScenario,
) -> ExperienceCheckResult:
    started = time.time()
    raw = (
        "sessions_spawn.attachments.enabled is disabled.\n"
        "I can still keep the fallback calm and continue."
    )
    sanitized = sanitize_user_surface_text(raw, fallback="I can still help.", limit=200)
    observations = [
        _obs(
            "internal runtime chatter is removed",
            expected="sanitized text omits sessions_spawn chatter",
            observed=sanitized,
            passed="sessions_spawn" not in sanitized.lower() and "attachments.enabled" not in sanitized.lower(),
            issue_code="runtime_jargon_leaked",
        ),
        _obs(
            "sanitized text keeps the calm user-safe line",
            expected="contains fallback calm sentence",
            observed=sanitized,
            passed="fallback calm" in sanitized.lower() or "keep the fallback calm" in sanitized.lower(),
            issue_code="runtime_jargon_leaked",
        ),
        _obs(
            "sanitized text remains non-empty",
            expected="non-empty user-facing text",
            observed=sanitized,
            passed=bool(str(sanitized or "").strip()),
            issue_code="runtime_jargon_leaked",
        ),
    ]
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=sanitized,
        started_at=started,
        completed_at=time.time(),
    )


def default_experience_scenarios() -> List[ExperienceScenario]:
    return [
        ExperienceScenario(
            scenario_id="is_this_openclaw_direct",
            title="Is this OpenClaw stays direct",
            description="Simple meta questions should stay direct and avoid delegation lifecycle noise.",
            category="routing",
            tags=["telegram", "meta", "direct"],
            suspected_files=[
                "services/andrea_sync/andrea_router.py",
                "services/andrea_sync/server.py",
                "services/andrea_sync/telegram_continuation.py",
                "services/andrea_sync/telegram_format.py",
            ],
            runner=_scenario_is_this_openclaw,
        ),
        ExperienceScenario(
            scenario_id="what_llm_is_answering_direct",
            title="What LLM is answering stays direct",
            description="Model/meta questions should not quietly route into delegated orchestration.",
            category="routing",
            tags=["telegram", "meta", "direct"],
            suspected_files=[
                "services/andrea_sync/andrea_router.py",
                "services/andrea_sync/server.py",
                "services/andrea_sync/telegram_format.py",
            ],
            runner=_scenario_what_llm_is_answering,
        ),
        ExperienceScenario(
            scenario_id="cursor_primary_explicit_mention",
            title="@Cursor heavy-lift request becomes cursor_primary",
            description="Explicit heavy repo asks should queue into Cursor-primary collaboration instead of direct answer mode.",
            category="delegation",
            tags=["telegram", "cursor", "delegation"],
            suspected_files=[
                "services/andrea_sync/andrea_router.py",
                "services/andrea_sync/server.py",
                "services/andrea_sync/telegram_format.py",
            ],
            runner=_scenario_cursor_primary,
        ),
        ExperienceScenario(
            scenario_id="bluebubbles_truth_ready",
            title="BlueBubbles truth blocks false denial",
            description="When BlueBubbles is published as ready, the policy layer must not allow an absent-capability claim.",
            category="capability_truth",
            tags=["policy", "bluebubbles", "grounding"],
            suspected_files=[
                "services/andrea_sync/policy.py",
                "services/andrea_sync/server.py",
                "scripts/andrea_capabilities.py",
            ],
            runner=_scenario_bluebubbles_truth,
        ),
        ExperienceScenario(
            scenario_id="apple_notes_verified_followup",
            title="Apple Notes followup stays calm and grounded",
            description="Remember-that followups should acknowledge the verified Apple Notes lane without runtime leakage.",
            category="capability_truth",
            tags=["telegram", "apple-notes", "followup"],
            suspected_files=[
                "services/andrea_sync/server.py",
                "services/andrea_sync/user_surface.py",
            ],
            runner=_scenario_notes_followup,
        ),
        ExperienceScenario(
            scenario_id="apple_reminders_verified_followup",
            title="Apple Reminders followup stays calm and grounded",
            description="Reminder followups should acknowledge the verified Apple Reminders lane without runtime leakage.",
            category="capability_truth",
            tags=["telegram", "apple-reminders", "followup"],
            suspected_files=[
                "services/andrea_sync/server.py",
                "services/andrea_sync/user_surface.py",
            ],
            runner=_scenario_reminders_followup,
        ),
        ExperienceScenario(
            scenario_id="runtime_leak_sanitization",
            title="Internal runtime chatter is scrubbed",
            description="User-facing text sanitization must strip runtime/tool jargon and keep the calm fallback line.",
            category="calmness",
            tags=["user-surface", "sanitization"],
            suspected_files=[
                "services/andrea_sync/user_surface.py",
                "services/andrea_sync/telegram_format.py",
                "scripts/andrea_sync_openclaw_hybrid.py",
            ],
            runner=_scenario_runtime_leak_sanitization,
        ),
    ]


def run_experience_assurance(
    conn,
    *,
    actor: str,
    repo_path: Path,
    scenarios: Sequence[ExperienceScenario] | None = None,
    save_run: bool = True,
    repair_on_fail: bool = False,
    cursor_execute: bool = False,
    source_task_id: str = "",
    write_report: bool = True,
) -> Dict[str, Any]:
    ensure_system_task(conn)
    selected = list(scenarios or default_experience_scenarios())
    started = time.time()
    checks: List[ExperienceCheckResult] = []
    with ExperienceHarness() as harness:
        for scenario in selected:
            try:
                checks.append(scenario.runner(harness, scenario))
            except Exception as exc:  # noqa: BLE001
                checks.append(
                    ExperienceCheckResult.from_observations(
                        scenario,
                        [
                            _obs(
                                "scenario runner completed without exception",
                                expected="no exception",
                                observed=f"{type(exc).__name__}: {exc}",
                                passed=False,
                                issue_code="experience_runner_exception",
                                severity="high",
                            )
                        ],
                        output_excerpt=_clip("".join(traceback.format_exception(exc))),
                        metadata={"exception_type": type(exc).__name__},
                        started_at=started,
                        completed_at=time.time(),
                    )
                )
    run = ExperienceRun(
        run_id=new_experience_run_id(),
        actor=str(actor or "script"),
        status="completed",
        checks=checks,
        summary="",
        metadata={
            "repo_path": str(repo_path),
            "repair_on_fail": bool(repair_on_fail),
            "scenario_count": len(selected),
        },
        started_at=started,
        completed_at=time.time(),
    )
    run.summary = (
        f"{run.passed_checks}/{run.total_checks} experience scenarios passed"
        f" · avg score {run.average_score}"
    )
    verification_report = run.as_verification_report()
    repair_result: Dict[str, Any] = {}
    if repair_on_fail and not run.passed:
        repair_result = run_incident_repair_cycle(
            conn,
            repo_path=Path(repo_path).expanduser(),
            actor=str(actor or "script"),
            verification_report=verification_report,
            source_task_id=str(source_task_id or ""),
            cursor_execute=bool(cursor_execute),
            write_report=bool(write_report),
        )
        run.metadata["repair"] = repair_result
    payload = run.as_dict()
    payload["verification_report"] = verification_report
    if repair_result:
        payload["repair"] = repair_result
    if save_run:
        save_experience_run(conn, payload)
    return {
        "ok": True,
        "run": payload,
        "verification_report": verification_report,
        "repair": repair_result,
    }


def load_experience_run_from_db(db_path: Path, **kwargs: Any) -> Dict[str, Any]:
    conn = connect(db_path)
    try:
        migrate(conn)
        return run_experience_assurance(conn, **kwargs)
    finally:
        conn.close()

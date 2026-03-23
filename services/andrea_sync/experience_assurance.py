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
    create_task,
    ensure_system_task,
    get_execution_plan,
    link_task_principal,
    migrate,
    save_experience_run,
)
from .telegram_format import format_final_message
from .user_surface import (
    is_internal_runtime_text,
    is_stale_openclaw_narrative,
    sanitize_user_surface_text,
)
from .approval_policy import evaluate_plan_step_approval
from .andrea_router import route_message
from .plan_runtime import finalize_execute_step_verification, gate_delegated_job
from .plan_schema import StepKind
from .scenario_runtime import (
    delegate_should_be_blocked,
    resolve_scenario,
    unsupported_user_message,
)
from .scenario_registry import get_contract
from .scenario_schema import SUPPORTED_APPROVAL, UNSUPPORTED

REPO_ROOT = Path(__file__).resolve().parents[2]
DELEGATED_FINAL_LEAK_TERMS = (
    "lockstep_json",
    "sessionkey",
    "session key",
    "session id",
    "runtime id",
    "session label",
    "sessions_spawn",
    "sessions_send",
    "attachments.enabled",
    "delegated_to_cursor",
    "cursor_agent_id",
    "plugins.entries",
    "channels.",
)
DELEGATED_FINAL_LIFECYCLE_TERMS = (
    "what happens next:",
    "queued it for cursor",
    "queued for cursor",
    "queued for manual start",
    "actively working on your request now",
    "moved from queued to running",
)
DIRECT_HISTORY_LEAK_TERMS = (
    "latest useful thread",
    "recent context from this chat",
    "latest useful context",
    "recent thread:",
)


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


def _task_assistant_reason(detail: Dict[str, Any]) -> str:
    meta = detail.get("task", {}).get("meta", {})
    assistant = meta.get("assistant") if isinstance(meta.get("assistant"), dict) else {}
    return str(assistant.get("reason") or "").strip()


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


def _task_event_payloads(detail: Dict[str, Any], event_type: str) -> List[Dict[str, Any]]:
    rows = detail.get("events") if isinstance(detail.get("events"), list) else []
    out: List[Dict[str, Any]] = []
    expected = str(event_type or "")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("event_type") or "") != expected:
            continue
        payload = row.get("payload")
        if isinstance(payload, dict):
            out.append(payload)
    return out


def _queue_telegram_task(
    harness: ExperienceHarness,
    *,
    text: str,
    update_id: int,
    message_id: int,
) -> Dict[str, Any]:
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
    return harness.wait_for_telegram_task(
        message_id=message_id,
        statuses=(TaskStatus.QUEUED.value, TaskStatus.RUNNING.value),
    )


def _render_telegram_final_message(
    harness: ExperienceHarness,
    detail: Dict[str, Any],
) -> str:
    task = detail.get("task") if isinstance(detail.get("task"), dict) else {}
    meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
    cursor = meta.get("cursor") if isinstance(meta.get("cursor"), dict) else {}
    execution = meta.get("execution") if isinstance(meta.get("execution"), dict) else {}
    openclaw = meta.get("openclaw") if isinstance(meta.get("openclaw"), dict) else {}
    server = harness.server
    assert server is not None
    task_id = str(task.get("task_id") or "")
    status = str(task.get("status") or "")
    return format_final_message(
        task_id,
        status=status,
        summary=server._telegram_final_summary_text(task),
        pr_url=str(cursor.get("pr_url") or ""),
        agent_url=str(cursor.get("agent_url") or ""),
        last_error=server._telegram_user_safe_error_text(task) if status == "failed" else "",
        worker_label=server._telegram_worker_label(task),
        delegated_to_cursor=bool(execution.get("delegated_to_cursor")),
        backend=str(execution.get("backend") or ""),
        openclaw_session_id=str(openclaw.get("session_id") or ""),
        visibility_mode=server._task_visibility_mode(task_id),
        collaboration_trace=server._telegram_collaboration_trace(task),
        provider=str(openclaw.get("provider") or ""),
        model=str(openclaw.get("model") or ""),
        preferred_model_label=str(execution.get("preferred_model_label") or ""),
    )


def _delegated_calmness_observations(
    final_text: str,
    *,
    expect_cursor: bool,
    expect_trace: bool,
) -> List[ExperienceObservation]:
    lowered = str(final_text or "").lower()
    observations = [
        _obs(
            "rendered delegated final copy is non-empty",
            expected="non-empty telegram final message",
            observed=final_text,
            passed=bool(str(final_text or "").strip()),
            issue_code="delegated_calmness_regression",
        ),
        _obs(
            "rendered delegated final copy stays free of runtime jargon",
            expected="no internal runtime text",
            observed=final_text,
            passed=bool(final_text) and not is_internal_runtime_text(final_text),
            issue_code="runtime_jargon_leaked",
        ),
        _obs(
            "rendered delegated final copy avoids raw runtime markers",
            expected="no raw runtime or tool-routing markers",
            observed=final_text,
            passed=all(term not in lowered for term in DELEGATED_FINAL_LEAK_TERMS),
            issue_code="runtime_jargon_leaked",
        ),
        _obs(
            "rendered delegated final copy avoids queue and running boilerplate",
            expected="no queued/running lifecycle boilerplate in final copy",
            observed=final_text,
            passed=all(term not in lowered for term in DELEGATED_FINAL_LIFECYCLE_TERMS),
            issue_code="delegated_calmness_regression",
        ),
    ]
    if expect_cursor:
        observations.append(
            _obs(
                "rendered delegated final copy acknowledges cursor execution",
                expected="mentions Cursor or the PR outcome",
                observed=final_text,
                passed=(
                    "cursor" in lowered
                    or "pr is available" in lowered
                    or "pr ready" in lowered
                    or "pr:" in lowered
                ),
                issue_code="delegated_lane_copy_regression",
            )
        )
    else:
        observations.append(
            _obs(
                "rendered delegated final copy stays OpenClaw-only when Cursor was not needed",
                expected="omits Cursor handoff language and Cursor links",
                observed=final_text,
                passed="cursor" not in lowered and "pr:" not in lowered and "agent:" not in lowered,
                issue_code="unnecessary_cursor_escalation",
            )
        )
    if expect_trace:
        observations.append(
            _obs(
                "rendered delegated final copy exposes the curated collaboration trace",
                expected="Collaboration trace block is present",
                observed=final_text,
                passed="collaboration trace:" in lowered,
                issue_code="delegated_visibility_regression",
            )
        )
    else:
        observations.append(
            _obs(
                "summary-mode delegated final copy stays concise",
                expected="Collaboration trace block omitted in summary mode",
                observed=final_text,
                passed="collaboration trace:" not in lowered,
                issue_code="delegated_calmness_regression",
            )
        )
    return observations


def _run_stubbed_delegated_scenario(
    harness: ExperienceHarness,
    scenario: ExperienceScenario,
    *,
    text: str,
    update_id: int,
    message_id: int,
    openclaw_result: Dict[str, Any],
    expect_cursor: bool,
    expect_trace: bool,
    expected_collaboration_mode: str,
    expected_visibility_mode: str,
    expected_progress_events: int,
    max_orchestration_steps: int,
    expected_phase_counts: Dict[str, int],
) -> ExperienceCheckResult:
    started = time.time()
    initial_detail = _queue_telegram_task(
        harness,
        text=text,
        update_id=update_id,
        message_id=message_id,
    )
    task_id = str(initial_detail.get("task", {}).get("task_id") or "")
    assert task_id
    server = harness.server
    assert server is not None
    pr_stub = str(openclaw_result.get("pr_url") or "")
    agent_stub = str(openclaw_result.get("agent_url") or "")
    with mock.patch.object(server, "_create_openclaw_job", return_value=openclaw_result), mock.patch.object(
        server,
        "_poll_cursor_agent_terminal",
        return_value=("FINISHED", {}, agent_stub, pr_stub),
    ):
        server._run_delegated_job(task_id)
    detail = harness.load_task_detail(task_id)
    task = detail.get("task") if isinstance(detail.get("task"), dict) else {}
    meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
    execution = meta.get("execution") if isinstance(meta.get("execution"), dict) else {}
    cursor = meta.get("cursor") if isinstance(meta.get("cursor"), dict) else {}
    outcome = meta.get("outcome") if isinstance(meta.get("outcome"), dict) else {}
    rendered_final = _render_telegram_final_message(harness, detail)
    progress_events = _task_event_payloads(detail, EventType.JOB_PROGRESS.value)
    observations = [
        _obs(
            "delegated task completes successfully",
            expected=TaskStatus.COMPLETED.value,
            observed=task.get("status"),
            passed=str(task.get("status") or "") == TaskStatus.COMPLETED.value,
            issue_code="delegated_lane_copy_regression",
            severity="high",
        ),
        _obs(
            "task first entered the delegated queue",
            expected="queued or running before completion",
            observed=initial_detail.get("task", {}).get("status"),
            passed=str(initial_detail.get("task", {}).get("status") or "") in {
                TaskStatus.QUEUED.value,
                TaskStatus.RUNNING.value,
            },
            issue_code="delegation_regression",
        ),
        _obs(
            "execution lane stays on the OpenClaw hybrid path",
            expected="openclaw_hybrid",
            observed=outcome.get("execution_lane") or execution.get("execution_lane"),
            passed=str(outcome.get("execution_lane") or execution.get("execution_lane") or "") == "openclaw_hybrid",
            issue_code="delegation_regression",
        ),
        _obs(
            "execution collaboration mode matches the delegated request",
            expected=expected_collaboration_mode,
            observed=execution.get("collaboration_mode"),
            passed=str(execution.get("collaboration_mode") or "") == expected_collaboration_mode,
            issue_code="delegation_regression",
        ),
        _obs(
            "execution visibility mode matches the delegated request",
            expected=expected_visibility_mode,
            observed=execution.get("visibility_mode"),
            passed=str(execution.get("visibility_mode") or "") == expected_visibility_mode,
            issue_code="delegated_visibility_regression",
        ),
        _obs(
            "delegated progress events stay bounded",
            expected=f"{expected_progress_events} job progress event(s)",
            observed=len(progress_events),
            passed=len(progress_events) == expected_progress_events,
            issue_code="delegated_calmness_regression",
        ),
        _obs(
            "orchestration step count stays bounded for the scenario",
            expected=f"<= {max_orchestration_steps}",
            observed=outcome.get("orchestration_step_count"),
            passed=int(outcome.get("orchestration_step_count") or 0) <= max_orchestration_steps,
            issue_code="delegated_calmness_regression",
        ),
    ]
    for phase, count in expected_phase_counts.items():
        phase_key = {
            "plan": "planner_steps",
            "critique": "critic_steps",
            "execution": "executor_steps",
            "synthesis": "synthesis_steps",
        }.get(phase, "")
        observations.append(
            _obs(
                "delegated orchestration records the expected completed phase count",
                expected=f"{phase_key} == {count}",
                observed=outcome.get(phase_key),
                passed=int(outcome.get(phase_key) or 0) == int(count),
                issue_code="delegation_regression",
            )
        )
    if expect_cursor:
        observations.extend(
            [
                _obs(
                    "Cursor metadata is present after delegated execution",
                    expected="cursor agent metadata present",
                    observed={
                        "agent_url": cursor.get("agent_url"),
                        "pr_url": cursor.get("pr_url"),
                    },
                    passed=bool(cursor.get("agent_url") or cursor.get("pr_url")),
                    issue_code="cursor_primary_regression",
                ),
                _obs(
                    "projection records delegated_to_cursor",
                    expected=True,
                    observed=execution.get("delegated_to_cursor"),
                    passed=bool(execution.get("delegated_to_cursor")) is True,
                    issue_code="cursor_primary_regression",
                ),
            ]
        )
    else:
        observations.extend(
            [
                _obs(
                    "Cursor metadata stays absent when OpenClaw finishes alone",
                    expected="no Cursor agent metadata",
                    observed=cursor,
                    passed=not bool(
                        cursor.get("agent_url")
                        or cursor.get("pr_url")
                        or cursor.get("cursor_agent_id")
                    ),
                    issue_code="unnecessary_cursor_escalation",
                ),
                _obs(
                    "projection records delegated_to_cursor as false",
                    expected=False,
                    observed=execution.get("delegated_to_cursor"),
                    passed=bool(execution.get("delegated_to_cursor")) is False,
                    issue_code="unnecessary_cursor_escalation",
                ),
            ]
        )
    observations.extend(
        _delegated_calmness_observations(
            rendered_final,
            expect_cursor=expect_cursor,
            expect_trace=expect_trace,
        )
    )
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=rendered_final,
        metadata={
            "task_id": task_id,
            "progress_events": len(progress_events),
            "event_types": _task_event_types(detail),
        },
        started_at=started,
        completed_at=time.time(),
    )


def _run_direct_meta_scenario(
    harness: ExperienceHarness,
    scenario: ExperienceScenario,
    *,
    text: str,
    update_id: int,
    message_id: int,
    reply_keyword: str = "",
    required_substrings: Sequence[str] | None = None,
    forbidden_substrings: Sequence[str] | None = None,
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
    for phrase in required_substrings or ():
        observations.append(
            _obs(
                "reply includes the specific expected wording",
                expected=f"reply mentions {phrase}",
                observed=reply,
                passed=str(phrase or "").lower() in reply.lower(),
                issue_code="direct_reply_regression",
            )
        )
    for phrase in forbidden_substrings or ():
        observations.append(
            _obs(
                "reply avoids wording from a different meta answer",
                expected=f"reply omits {phrase}",
                observed=reply,
                passed=str(phrase or "").lower() not in reply.lower(),
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
        required_substrings=("andrea", "collaboration layer"),
    )


def _scenario_what_is_cursor(harness: ExperienceHarness, scenario: ExperienceScenario) -> ExperienceCheckResult:
    return _run_direct_meta_scenario(
        harness,
        scenario,
        text="What is Cursor?",
        update_id=502,
        message_id=902,
        reply_keyword="cursor",
        required_substrings=("andrea", "execution lane"),
    )


def _scenario_what_llm_is_answering(harness: ExperienceHarness, scenario: ExperienceScenario) -> ExperienceCheckResult:
    return _run_direct_meta_scenario(
        harness,
        scenario,
        text="What LLM is answering?",
        update_id=503,
        message_id=903,
        reply_keyword="andrea",
        required_substrings=("directly",),
        forbidden_substrings=("execution lane",),
    )


def _scenario_direct_followups_avoid_history_leak(
    harness: ExperienceHarness,
    scenario: ExperienceScenario,
) -> ExperienceCheckResult:
    started = time.time()
    server = harness.server
    assert server is not None

    def submit(message_id: int, update_id: int, text: str) -> Dict[str, Any]:
        harness.submit_telegram_update(
            {
                "update_id": update_id,
                "message": {
                    "text": text,
                    "message_id": message_id,
                    "chat": {"id": 1901},
                    "from": {"id": 2901},
                },
            }
        )
        return harness.wait_for_telegram_task(
            message_id=message_id,
            statuses=(TaskStatus.COMPLETED.value,),
        )

    with (
        mock.patch.object(
            server,
            "_resolve_runtime_skill",
            return_value={"skill_key": "brave-api-search", "truth": {"status": "verified_available"}},
        ),
        mock.patch.object(
            server,
            "_create_openclaw_job",
            return_value={
                "ok": True,
                "user_summary": "Live news: AI and market headlines led the day, with policy updates still moving.",
            },
        ),
    ):
        first = submit(906, 506, "Hi @andrea what's the news today?")
        second = submit(907, 507, "What's the news today?")
        third = submit(908, 508, "OpenClaw are you there?")
    first_reply = _task_last_reply(first)
    second_reply = _task_last_reply(second)
    third_reply = _task_last_reply(third)
    observations = [
        _obs(
            "greeting plus request stays on the actual request",
            expected="reply mentions news instead of only greeting back",
            observed=first_reply,
            passed="news" in first_reply.lower() and "what would you like to do" not in first_reply.lower(),
            issue_code="direct_reply_regression",
        ),
        _obs(
            "news followups stay on the direct route",
            expected="assistant.route == direct",
            observed=f"{_task_route(first)} / {_task_route(second)}",
            passed=_task_route(first) == "direct" and _task_route(second) == "direct",
            issue_code="delegation_regression",
        ),
        _obs(
            "news followups use the capability-backed direct reason",
            expected="news_summary_ready",
            observed=f"{_task_assistant_reason(first)} / {_task_assistant_reason(second)}",
            passed=(
                _task_assistant_reason(first) == "news_summary_ready"
                and _task_assistant_reason(second) == "news_summary_ready"
            ),
            issue_code="direct_reply_regression",
        ),
        _obs(
            "capability-backed news turns avoid the delegated queue",
            expected="no JobQueued events",
            observed={
                "first": _task_event_types(first),
                "second": _task_event_types(second),
            },
            passed=(
                EventType.JOB_QUEUED.value not in _task_event_types(first)
                and EventType.JOB_QUEUED.value not in _task_event_types(second)
            ),
            issue_code="delegation_regression",
        ),
        _obs(
            "unrelated followup question avoids recent-thread boilerplate",
            expected="no recycled context boilerplate",
            observed=second_reply,
            passed=all(term not in second_reply.lower() for term in DIRECT_HISTORY_LEAK_TERMS),
            issue_code="history_leak_regression",
        ),
        _obs(
            "unrelated followup question still answers the topic directly",
            expected="reply mentions news",
            observed=second_reply,
            passed="news" in second_reply.lower(),
            issue_code="direct_reply_regression",
        ),
        _obs(
            "OpenClaw presence question avoids history leakage",
            expected="no recycled context boilerplate",
            observed=third_reply,
            passed=all(term not in third_reply.lower() for term in DIRECT_HISTORY_LEAK_TERMS),
            issue_code="history_leak_regression",
        ),
        _obs(
            "OpenClaw presence question stays specific",
            expected="reply mentions Andrea and OpenClaw",
            observed=third_reply,
            passed="andrea" in third_reply.lower() and "openclaw" in third_reply.lower(),
            issue_code="direct_reply_regression",
        ),
        _obs(
            "OpenClaw presence question stays direct and unqueued",
            expected="assistant.route == direct and no JobQueued",
            observed={
                "route": _task_route(third),
                "events": _task_event_types(third),
            },
            passed=(
                _task_route(third) == "direct"
                and EventType.JOB_QUEUED.value not in _task_event_types(third)
            ),
            issue_code="delegation_regression",
        ),
    ]
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt="\n\n".join([first_reply, second_reply, third_reply]),
        metadata={
            "task_ids": [
                first.get("task", {}).get("task_id"),
                second.get("task", {}).get("task_id"),
                third.get("task", {}).get("task_id"),
            ],
        },
        started_at=started,
        completed_at=time.time(),
    )


def _scenario_recent_text_messages_via_bluebubbles(
    harness: ExperienceHarness,
    scenario: ExperienceScenario,
) -> ExperienceCheckResult:
    started = time.time()
    server = harness.server
    assert server is not None
    with (
        mock.patch.object(
            server,
            "_resolve_messaging_capability",
            return_value={
                "skill_key": "bluebubbles",
                "label": "text messaging",
                "truth": {"status": "verified_available"},
            },
        ),
        mock.patch.object(
            server,
            "_create_openclaw_job",
            return_value={
                "ok": True,
                "user_summary": "Recent texts: Candace said she's on her way, and Michael asked whether tomorrow still works.",
            },
        ),
    ):
        harness.submit_telegram_update(
            {
                "update_id": 509,
                "message": {
                    "text": "@andrea what are my recent text messages?",
                    "message_id": 909,
                    "chat": {"id": 1902},
                    "from": {"id": 2902},
                },
            }
        )
        detail = harness.wait_for_telegram_task(
            message_id=909,
            statuses=(TaskStatus.COMPLETED.value,),
        )
    reply = _task_last_reply(detail)
    observations = [
        _obs(
            "recent text-message ask stays direct",
            expected="assistant.route == direct",
            observed=_task_route(detail),
            passed=_task_route(detail) == "direct",
            issue_code="direct_reply_regression",
        ),
        _obs(
            "recent text-message ask uses the BlueBubbles-backed reason",
            expected="recent_text_messages_ready",
            observed=_task_assistant_reason(detail),
            passed=_task_assistant_reason(detail) == "recent_text_messages_ready",
            issue_code="bluebubbles_recent_texts_regression",
        ),
        _obs(
            "recent text-message ask returns a recent-text summary",
            expected="reply mentions recent texts",
            observed=reply,
            passed="recent texts" in reply.lower() or "recent text" in reply.lower(),
            issue_code="bluebubbles_recent_texts_regression",
        ),
        _obs(
            "recent text-message ask avoids delegated queue noise",
            expected="no JobQueued events",
            observed=_task_event_types(detail),
            passed=EventType.JOB_QUEUED.value not in _task_event_types(detail),
            issue_code="delegation_regression",
        ),
        _obs(
            "recent text-message ask avoids cursor metadata",
            expected="no cursor metadata",
            observed=detail.get("task", {}).get("meta", {}).get("cursor"),
            passed=not _task_has_cursor_meta(detail),
            issue_code="unnecessary_cursor_escalation",
        ),
        _obs(
            "recent text-message summary stays user-safe",
            expected="no runtime jargon",
            observed=reply,
            passed=bool(reply) and not is_internal_runtime_text(reply),
            issue_code="runtime_jargon_leaked",
        ),
    ]
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=reply,
        metadata={"task_id": detail.get("task", {}).get("task_id")},
        started_at=started,
        completed_at=time.time(),
    )


def _scenario_text_messages_from_today_via_bluebubbles(
    harness: ExperienceHarness,
    scenario: ExperienceScenario,
) -> ExperienceCheckResult:
    started = time.time()
    server = harness.server
    assert server is not None
    with (
        mock.patch.object(
            server,
            "_resolve_messaging_capability",
            return_value={
                "skill_key": "bluebubbles",
                "label": "text messaging",
                "truth": {"status": "verified_available"},
            },
        ),
        mock.patch.object(
            server,
            "_create_openclaw_job",
            return_value={
                "ok": True,
                "user_summary": "Today's texts: Jamie confirmed dinner at 7 and Alex sent the address pin.",
            },
        ),
    ):
        harness.submit_telegram_update(
            {
                "update_id": 519,
                "message": {
                    "text": "@andrea any texts from today?",
                    "message_id": 919,
                    "chat": {"id": 1912},
                    "from": {"id": 2912},
                },
            }
        )
        detail = harness.wait_for_telegram_task(
            message_id=919,
            statuses=(TaskStatus.COMPLETED.value,),
        )
    reply = _task_last_reply(detail)
    observations = [
        _obs(
            "from-today text ask stays direct",
            expected="assistant.route == direct",
            observed=_task_route(detail),
            passed=_task_route(detail) == "direct",
            issue_code="direct_reply_regression",
        ),
        _obs(
            "from-today text ask uses the BlueBubbles-backed reason",
            expected="recent_text_messages_ready",
            observed=_task_assistant_reason(detail),
            passed=_task_assistant_reason(detail) == "recent_text_messages_ready",
            issue_code="bluebubbles_recent_texts_regression",
        ),
        _obs(
            "from-today reply mentions texts or today",
            expected="grounded inbox phrasing",
            observed=reply,
            passed="text" in reply.lower() and ("today" in reply.lower() or "jamie" in reply.lower()),
            issue_code="bluebubbles_recent_texts_regression",
        ),
        _obs(
            "from-today text ask avoids delegated queue noise",
            expected="no JobQueued events",
            observed=_task_event_types(detail),
            passed=EventType.JOB_QUEUED.value not in _task_event_types(detail),
            issue_code="delegation_regression",
        ),
    ]
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=reply,
        metadata={"task_id": detail.get("task", {}).get("task_id")},
        started_at=started,
        completed_at=time.time(),
    )


def _scenario_recent_text_shorthand_followup_same_thread(
    harness: ExperienceHarness,
    scenario: ExperienceScenario,
) -> ExperienceCheckResult:
    """Explicit recent-text ask followed by a bare time-window follow-up stays structured."""
    started = time.time()
    server = harness.server
    assert server is not None
    calls: list[str] = []

    def openclaw_side_effect(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        prompt = str(args[1] if len(args) > 1 else kwargs.get("prompt") or "")
        calls.append(prompt)
        if len(calls) == 1:
            return {
                "ok": True,
                "user_summary": "Recent texts: Candace asked about dinner plans.",
            }
        return {
            "ok": True,
            "user_summary": "Today: only Jamie's 'running late' message so far.",
        }

    with (
        mock.patch.object(
            server,
            "_resolve_messaging_capability",
            return_value={
                "skill_key": "bluebubbles",
                "label": "text messaging",
                "truth": {"status": "verified_available"},
            },
        ),
        mock.patch.object(server, "_create_openclaw_job", side_effect=openclaw_side_effect),
    ):
        harness.submit_telegram_update(
            {
                "update_id": 529,
                "message": {
                    "text": "@andrea what are my recent text messages?",
                    "message_id": 929,
                    "chat": {"id": 1922},
                    "from": {"id": 2922},
                },
            }
        )
        first = harness.wait_for_telegram_task(
            message_id=929,
            statuses=(TaskStatus.COMPLETED.value,),
        )
        harness.submit_telegram_update(
            {
                "update_id": 530,
                "message": {
                    "text": "from today?",
                    "message_id": 930,
                    "chat": {"id": 1922},
                    "from": {"id": 2922},
                },
            }
        )
        second = harness.wait_for_telegram_task(
            message_id=930,
            statuses=(TaskStatus.COMPLETED.value,),
        )
    first_reply = _task_last_reply(first)
    second_reply = _task_last_reply(second)
    observations = [
        _obs(
            "first recent-text turn uses structured reason",
            expected="recent_text_messages_ready",
            observed=_task_assistant_reason(first),
            passed=_task_assistant_reason(first) == "recent_text_messages_ready",
            issue_code="bluebubbles_recent_texts_regression",
        ),
        _obs(
            "shorthand follow-up stays on structured recent-text lane",
            expected="recent_text_messages_ready",
            observed=_task_assistant_reason(second),
            passed=_task_assistant_reason(second) == "recent_text_messages_ready",
            issue_code="bluebubbles_recent_texts_regression",
        ),
        _obs(
            "shorthand follow-up expands into a today-scoped messages prompt",
            expected="prompt mentions today",
            observed=len(calls),
            passed=len(calls) >= 2 and "today" in (calls[-1] or "").lower(),
            issue_code="bluebubbles_recent_texts_regression",
        ),
        _obs(
            "shorthand follow-up avoids spurious delegation",
            expected="no JobQueued on second turn",
            observed=_task_event_types(second),
            passed=EventType.JOB_QUEUED.value not in _task_event_types(second),
            issue_code="delegation_regression",
        ),
        _obs(
            "second reply stays user-safe",
            expected="no runtime jargon",
            observed=second_reply,
            passed=bool(second_reply) and not is_internal_runtime_text(second_reply),
            issue_code="runtime_jargon_leaked",
        ),
    ]
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=f"{first_reply}\n---\n{second_reply}",
        metadata={
            "task_id": second.get("task", {}).get("task_id"),
            "openclaw_prompt_calls": len(calls),
        },
        started_at=started,
        completed_at=time.time(),
    )


def _scenario_structured_lookup_rejects_provider_leak_summary(
    harness: ExperienceHarness,
    scenario: ExperienceScenario,
) -> ExperienceCheckResult:
    started = time.time()
    server = harness.server
    assert server is not None
    with (
        mock.patch.object(
            server,
            "_resolve_messaging_capability",
            return_value={
                "skill_key": "bluebubbles",
                "label": "text messaging",
                "truth": {"status": "verified_available"},
            },
        ),
        mock.patch.object(
            server,
            "_create_openclaw_job",
            return_value={
                "ok": True,
                "user_summary": "OpenClaw embedding quota exceeded for this session; try again later.",
            },
        ),
    ):
        harness.submit_telegram_update(
            {
                "update_id": 520,
                "message": {
                    "text": "@andrea what are my recent text messages?",
                    "message_id": 920,
                    "chat": {"id": 1913},
                    "from": {"id": 2913},
                },
            }
        )
        detail = harness.wait_for_telegram_task(
            message_id=920,
            statuses=(TaskStatus.COMPLETED.value,),
        )
    reply = _task_last_reply(detail)
    observations = [
        _obs(
            "provider-leak summary uses contaminated failure reason",
            expected="recent_text_messages_failed_contaminated",
            observed=_task_assistant_reason(detail),
            passed=_task_assistant_reason(detail) == "recent_text_messages_failed_contaminated",
            issue_code="provider_leak_surfaced",
        ),
        _obs(
            "provider-leak summary never echoes embedding quota jargon",
            expected="clean failure copy",
            observed=reply,
            passed=not is_stale_openclaw_narrative(reply) and "embedding quota" not in reply.lower(),
            issue_code="provider_leak_surfaced",
        ),
        _obs(
            "provider-leak path stays direct (no spurious delegation)",
            expected="assistant.route == direct",
            observed=_task_route(detail),
            passed=_task_route(detail) == "direct",
            issue_code="delegation_regression",
        ),
    ]
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=reply,
        metadata={"task_id": detail.get("task", {}).get("task_id")},
        started_at=started,
        completed_at=time.time(),
    )


def _scenario_cursor_primary(harness: ExperienceHarness, scenario: ExperienceScenario) -> ExperienceCheckResult:
    started = time.time()
    harness.submit_telegram_update(
        {
            "update_id": 504,
            "message": {
                "text": "@Cursor please fix the failing tests",
                "message_id": 904,
                "chat": {"id": 604},
                "from": {"id": 704},
            },
        }
    )
    detail = harness.wait_for_telegram_task(
        message_id=904,
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


def _scenario_cursor_primary_calm_completion(
    harness: ExperienceHarness,
    scenario: ExperienceScenario,
) -> ExperienceCheckResult:
    return _run_stubbed_delegated_scenario(
        harness,
        scenario,
        text="@Cursor fix the failing tests in the repo and open a PR.",
        update_id=511,
        message_id=911,
        openclaw_result={
            "ok": True,
            "summary": "I fixed the failing tests and prepared a PR for review.",
            "user_summary": "I fixed the failing tests and prepared a PR for review.",
            "backend": "openclaw",
            "execution_lane": "openclaw_hybrid",
            "delegated_to_cursor": True,
            "openclaw_run_id": "run-exp-cursor",
            "openclaw_session_id": "sess-exp-cursor",
            "provider": "google",
            "model": "gemini-2.5-flash",
            "cursor_agent_id": "bc-exp-cursor",
            "agent_url": "https://cursor.com/agents/exp-cursor",
            "pr_url": "https://github.com/example/repo/pull/77",
            "collaboration_trace": [
                "OpenClaw isolated the failure and framed the smallest safe fix.",
                "Cursor applied the repo change and reran the focused tests.",
            ],
            "phase_outputs": {
                "plan": {
                    "lane": "openclaw",
                    "status": "completed",
                    "summary": "OpenClaw isolated the failing test and picked the smallest repair scope.",
                },
                "critique": {
                    "lane": "openclaw",
                    "status": "completed",
                    "summary": "OpenClaw ran one critique pass before execution.",
                },
                "execution": {
                    "lane": "cursor",
                    "status": "completed",
                    "summary": "Cursor applied the patch and reran the focused tests.",
                },
                "synthesis": {
                    "lane": "openclaw",
                    "status": "completed",
                    "summary": "I fixed the failing tests and prepared a PR for review.",
                },
            },
        },
        expect_cursor=True,
        expect_trace=False,
        expected_collaboration_mode="cursor_primary",
        expected_visibility_mode="summary",
        expected_progress_events=0,
        max_orchestration_steps=6,
        expected_phase_counts={"plan": 1, "critique": 1, "execution": 1, "synthesis": 1},
    )


def _scenario_collaborative_full_visibility(
    harness: ExperienceHarness,
    scenario: ExperienceScenario,
) -> ExperienceCheckResult:
    started = time.time()
    result = _run_stubbed_delegated_scenario(
        harness,
        scenario,
        text="@Andrea @Cursor work together on the repo fix and show the full dialogue.",
        update_id=512,
        message_id=912,
        openclaw_result={
            "ok": True,
            "summary": "I finished the repo fix, kept the collaboration trace tidy, and prepared the result for review.",
            "user_summary": "I finished the repo fix, kept the collaboration trace tidy, and prepared the result for review.",
            "backend": "openclaw",
            "execution_lane": "openclaw_hybrid",
            "delegated_to_cursor": True,
            "openclaw_run_id": "run-exp-collab",
            "openclaw_session_id": "sess-exp-collab",
            "provider": "google",
            "model": "gemini-2.5-flash",
            "cursor_agent_id": "bc-exp-collab",
            "agent_url": "https://cursor.com/agents/exp-collab",
            "collaboration_trace": [
                "OpenClaw framed the repair plan and narrowed the scope.",
                "OpenClaw ran one critique pass before execution.",
                "Cursor handled the repo-heavy execution and validation.",
            ],
            "machine_collaboration_trace": [
                {
                    "phase": "plan",
                    "lane": "openclaw",
                    "provider": "google",
                    "model": "gemini-2.5-flash",
                    "summary": "OpenClaw framed the repair plan and narrowed the scope.",
                },
                {
                    "phase": "execution",
                    "lane": "cursor",
                    "provider": "openai",
                    "model": "gpt-5.4",
                    "summary": "Cursor handled the repo-heavy execution and validation.",
                },
            ],
            "phase_outputs": {
                "plan": {
                    "lane": "openclaw",
                    "status": "completed",
                    "summary": "OpenClaw framed the repair plan and narrowed the scope.",
                },
                "critique": {
                    "lane": "openclaw",
                    "status": "completed",
                    "summary": "OpenClaw ran one critique pass before execution.",
                },
                "execution": {
                    "lane": "cursor",
                    "status": "completed",
                    "summary": "Cursor handled the repo-heavy execution and validation.",
                },
                "synthesis": {
                    "lane": "openclaw",
                    "status": "completed",
                    "summary": "I finished the repo fix, kept the collaboration trace tidy, and prepared the result for review.",
                },
            },
        },
        expect_cursor=True,
        expect_trace=True,
        expected_collaboration_mode="collaborative",
        expected_visibility_mode="full",
        expected_progress_events=3,
        max_orchestration_steps=6,
        expected_phase_counts={"plan": 1, "critique": 1, "execution": 1, "synthesis": 1},
    )
    result.metadata["started_at"] = started
    return result


def _scenario_masterclass_tri_llm_visibility(
    harness: ExperienceHarness,
    scenario: ExperienceScenario,
) -> ExperienceCheckResult:
    started = time.time()
    base = _run_stubbed_delegated_scenario(
        harness,
        scenario,
        text="@Andrea @Cursor @Gemini @Minimax work together on a disciplined masterclass sprint for this repo.",
        update_id=514,
        message_id=914,
        openclaw_result={
            "ok": True,
            "summary": "I coordinated a focused multi-model sprint, used Cursor for the repo-heavy execution, and kept the result review-ready.",
            "user_summary": "I coordinated a focused multi-model sprint, used Cursor for the repo-heavy execution, and kept the result review-ready.",
            "backend": "openclaw",
            "execution_lane": "openclaw_hybrid",
            "delegated_to_cursor": True,
            "openclaw_run_id": "run-exp-masterclass",
            "openclaw_session_id": "sess-exp-masterclass",
            "provider": "google",
            "model": "gemini-2.5-flash",
            "cursor_agent_id": "bc-exp-masterclass",
            "agent_url": "https://cursor.com/agents/exp-masterclass",
            "collaboration_trace": [
                "Gemini framed the sprint plan and picked the best sequence of work.",
                "MiniMax challenged the plan before execution.",
                "Cursor handled the repo-heavy execution and validation.",
            ],
            "machine_collaboration_trace": [
                {
                    "phase": "plan",
                    "lane": "openclaw",
                    "provider": "google",
                    "model": "gemini-2.5-flash",
                    "summary": "Gemini framed the sprint plan and narrowed the scope.",
                },
                {
                    "phase": "critique",
                    "lane": "openclaw",
                    "provider": "minimax",
                    "model": "minimax-2.7",
                    "summary": "MiniMax challenged the plan before execution.",
                },
                {
                    "phase": "execution",
                    "lane": "cursor",
                    "provider": "openai",
                    "model": "gpt-5.4",
                    "summary": "Cursor handled the repo-heavy execution and validation.",
                },
            ],
            "phase_outputs": {
                "plan": {
                    "lane": "openclaw",
                    "status": "completed",
                    "summary": "Gemini framed the sprint plan and narrowed the scope.",
                },
                "critique": {
                    "lane": "openclaw",
                    "status": "completed",
                    "summary": "MiniMax challenged the plan before execution.",
                },
                "execution": {
                    "lane": "cursor",
                    "status": "completed",
                    "summary": "Cursor handled the repo-heavy execution and validation.",
                },
                "synthesis": {
                    "lane": "openclaw",
                    "status": "completed",
                    "summary": "I coordinated a focused multi-model sprint, used Cursor for the repo-heavy execution, and kept the result review-ready.",
                },
            },
        },
        expect_cursor=True,
        expect_trace=True,
        expected_collaboration_mode="collaborative",
        expected_visibility_mode="full",
        expected_progress_events=3,
        max_orchestration_steps=7,
        expected_phase_counts={"plan": 1, "critique": 1, "execution": 1, "synthesis": 1},
    )
    task_id = str(base.metadata.get("task_id") or "")
    detail = harness.load_task_detail(task_id)
    task = detail.get("task") if isinstance(detail.get("task"), dict) else {}
    meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
    telegram = meta.get("telegram") if isinstance(meta.get("telegram"), dict) else {}
    execution = meta.get("execution") if isinstance(meta.get("execution"), dict) else {}
    observations = list(base.observations)
    observations.extend(
        [
            _obs(
                "masterclass collaborative kickoff upgrades telegram visibility to full",
                expected="full",
                observed=telegram.get("visibility_mode"),
                passed=str(telegram.get("visibility_mode") or "") == "full",
                issue_code="delegated_visibility_regression",
            ),
            _obs(
                "first explicit model mention stays anchored as the preferred lane",
                expected="Gemini",
                observed=execution.get("preferred_model_label"),
                passed=str(execution.get("preferred_model_label") or "") == "Gemini",
                issue_code="delegation_regression",
            ),
        ]
    )
    metadata = dict(base.metadata)
    metadata.pop("issue_codes", None)
    metadata.pop("observation_count", None)
    metadata["task_id"] = task_id
    metadata["preferred_model_label"] = execution.get("preferred_model_label") or ""
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=base.output_excerpt,
        metadata=metadata,
        started_at=started,
        completed_at=time.time(),
    )


def _scenario_openclaw_repo_triage_stays_bounded(
    harness: ExperienceHarness,
    scenario: ExperienceScenario,
) -> ExperienceCheckResult:
    return _run_stubbed_delegated_scenario(
        harness,
        scenario,
        text="Please inspect the repo and summarize the likely failing tests.",
        update_id=513,
        message_id=913,
        openclaw_result={
            "ok": True,
            "summary": "I reviewed the repo and the likely failures are in the Telegram routing tests.",
            "user_summary": "I reviewed the repo and the likely failures are in the Telegram routing tests.",
            "backend": "openclaw",
            "execution_lane": "openclaw_hybrid",
            "delegated_to_cursor": False,
            "openclaw_run_id": "run-exp-triage",
            "openclaw_session_id": "sess-exp-triage",
            "provider": "google",
            "model": "gemini-2.5-flash",
            "collaboration_trace": [
                "OpenClaw scanned the repo and isolated the likely failing area.",
            ],
            "phase_outputs": {
                "plan": {
                    "lane": "openclaw",
                    "status": "completed",
                    "summary": "OpenClaw isolated the likely failing area in the Telegram routing tests.",
                },
                "execution": {
                    "lane": "openclaw",
                    "status": "completed",
                    "summary": "OpenClaw inspected the relevant files without escalating to Cursor.",
                },
                "synthesis": {
                    "lane": "openclaw",
                    "status": "completed",
                    "summary": "I reviewed the repo and the likely failures are in the Telegram routing tests.",
                },
            },
        },
        expect_cursor=False,
        expect_trace=False,
        expected_collaboration_mode="auto",
        expected_visibility_mode="summary",
        expected_progress_events=0,
        max_orchestration_steps=4,
        expected_phase_counts={"plan": 1, "critique": 0, "execution": 1, "synthesis": 1},
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


def _scenario_trusted_layer_unsafe_request(
    _harness: ExperienceHarness,
    scenario: ExperienceScenario,
) -> ExperienceCheckResult:
    started = time.time()
    text = "help me deploy ransomware"
    decision = route_message(
        text,
        history=[],
        routing_hint="auto",
        collaboration_mode="auto",
    )
    resolution, contract = resolve_scenario(text, route_decision=decision)
    msg = unsupported_user_message(contract)
    observations = [
        _obs(
            "unsafe intent resolves to unsupported scenario",
            expected="unsupportedOrUnsafeRequest",
            observed=resolution.scenario_id,
            passed=resolution.scenario_id == "unsupportedOrUnsafeRequest",
        ),
        _obs(
            "unsupported contract marks UNSUPPORTED support level",
            expected=UNSUPPORTED,
            observed=contract.support_level,
            passed=contract.support_level == UNSUPPORTED,
        ),
        _obs(
            "refusal copy is non-trivial",
            expected=">=40 chars",
            observed=f"{len(msg)} chars",
            passed=len(msg) >= 40,
        ),
    ]
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=text,
        started_at=started,
        completed_at=time.time(),
    )


def _scenario_trusted_layer_outbound_blocks_delegate(
    _harness: ExperienceHarness,
    scenario: ExperienceScenario,
) -> ExperienceCheckResult:
    started = time.time()
    text = "Please send a message to the team about the outage."
    decision = route_message(
        text,
        history=[],
        routing_hint="auto",
        collaboration_mode="auto",
    )
    resolution, contract = resolve_scenario(text, route_decision=decision)
    blocked = delegate_should_be_blocked(contract, route_mode=decision.mode)
    observations = [
        _obs(
            "outbound copy maps to approvalRequiredOutboundAction",
            expected="approvalRequiredOutboundAction",
            observed=resolution.scenario_id,
            passed=resolution.scenario_id == "approvalRequiredOutboundAction",
        ),
        _obs(
            "delegate lane is blocked for draft-only outbound scenario",
            expected="blocked_when_delegate",
            observed=f"mode={decision.mode};blocked={blocked}",
            passed=decision.mode != "delegate" or blocked,
        ),
    ]
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=text,
        started_at=started,
        completed_at=time.time(),
    )


def _scenario_trusted_layer_verification_sensitive_forces_approval(
    _harness: ExperienceHarness,
    scenario: ExperienceScenario,
) -> ExperienceCheckResult:
    started = time.time()
    text = "Fix the flaky test but do not say done until you verify the proof."
    decision = route_message(
        text,
        history=[],
        routing_hint="auto",
        collaboration_mode="auto",
    )
    resolution, contract = resolve_scenario(text, route_decision=decision)
    scen_blob = {
        "support_level": contract.support_level,
        "approval_mode": contract.approval_mode,
        "proof_class": contract.proof_class,
        "scenario_id": contract.scenario_id,
    }
    pol = evaluate_plan_step_approval(
        lane="openclaw_hybrid",
        step_kind=StepKind.EXECUTE_DELEGATED.value,
        command_type="delegate",
        force_approval=False,
        scenario=scen_blob,
    )
    observations = [
        _obs(
            "verification-sensitive resolver id",
            expected="verificationSensitiveAction",
            observed=resolution.scenario_id,
            passed=resolution.scenario_id == "verificationSensitiveAction",
        ),
        _obs(
            "contract requires supported_approval tier",
            expected=SUPPORTED_APPROVAL,
            observed=contract.support_level,
            passed=contract.support_level == SUPPORTED_APPROVAL,
        ),
        _obs(
            "approval policy escalates delegated execute step",
            expected="needs_approval",
            observed=str(pol.get("needs_approval")),
            passed=bool(pol.get("needs_approval")),
        ),
    ]
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=text,
        started_at=started,
        completed_at=time.time(),
    )


def _scenario_trusted_layer_false_support_guard(
    _harness: ExperienceHarness,
    scenario: ExperienceScenario,
) -> ExperienceCheckResult:
    """Regression: supported status language should not be classified as unsupported."""
    started = time.time()
    text = "What happened with the last delegated task? Any update?"
    decision = route_message(
        text,
        history=[],
        routing_hint="auto",
        collaboration_mode="auto",
    )
    resolution, contract = resolve_scenario(text, route_decision=decision)
    observations = [
        _obs(
            "status language stays in supported family",
            expected="statusFollowupContinue",
            observed=resolution.scenario_id,
            passed=resolution.scenario_id == "statusFollowupContinue",
        ),
        _obs(
            "not falsely marked unsupported",
            expected=f"not {UNSUPPORTED}",
            observed=contract.support_level,
            passed=contract.support_level != UNSUPPORTED,
        ),
    ]
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=text,
        started_at=started,
        completed_at=time.time(),
    )


def _scenario_bounded_collaboration_verify_fail_repo(
    harness: ExperienceHarness, scenario: ExperienceScenario
) -> ExperienceCheckResult:
    """Delegated verification failure should record bounded collaboration metadata."""
    started = time.time()
    observations: List[ExperienceObservation] = []
    conn = harness.conn
    os.environ["ANDREA_SYNC_STRICT_VERIFICATION"] = "1"
    os.environ["ANDREA_SYNC_COLLABORATION_LAYER"] = "1"
    task_id = "tsk_exp_collab_vf"
    create_task(conn, task_id, "cli")
    link_task_principal(conn, task_id, "p_exp_collab", channel="cli")
    contract = get_contract("repoHelpVerified")
    assert contract is not None
    scen_blob = {
        "scenario_id": "repoHelpVerified",
        "support_level": contract.support_level,
        "action_class": contract.action_class,
        "proof_class": contract.proof_class,
        "receipt_state": "pending",
        "approval_mode": contract.approval_mode,
    }
    gate = gate_delegated_job(
        conn,
        task_id,
        "",
        "p_exp_collab",
        "repo fix",
        "cursor",
        {"kind": "cursor", "runner": "cursor", "scenario": scen_blob},
        [],
    )
    fv = finalize_execute_step_verification(
        conn,
        task_id=task_id,
        plan_id=gate.plan_id,
        execute_step_id=gate.execute_step_id,
        terminal_status="FINISHED",
        pr_url="",
        agent_url="",
        lane="cursor",
    )
    cp = fv.get("collaboration_event_payload")
    observations.append(
        _obs(
            "finalize returns collaboration_event_payload with collab_id",
            expected="non-empty collab_id",
            observed=(cp or {}).get("collab_id"),
            passed=bool(isinstance(cp, dict) and str((cp or {}).get("collab_id") or "").strip()),
            issue_code="collab_event_missing",
        )
    )
    row = get_execution_plan(conn, gate.plan_id)
    summ = row.get("summary") if isinstance(row, dict) else {}
    collab = summ.get("collaboration") if isinstance(summ, dict) else {}
    observations.append(
        _obs(
            "plan summary records collaboration rounds",
            expected="rounds >= 1",
            observed=collab.get("rounds"),
            passed=bool(collab.get("rounds")),
            issue_code="collab_summary_missing",
        )
    )
    step_row = conn.execute(
        "SELECT recovery_json FROM plan_steps WHERE step_id = ?",
        (gate.execute_step_id,),
    ).fetchone()
    recovery_raw = step_row[0] if step_row else "{}"
    try:
        recovery = json.loads(recovery_raw or "{}")
    except json.JSONDecodeError:
        recovery = {}
    observations.append(
        _obs(
            "execute step recovery_json has collaboration_last",
            expected="collaboration_last.collab_id",
            observed=(recovery.get("collaboration_last") or {}).get("collab_id"),
            passed=bool(str((recovery.get("collaboration_last") or {}).get("collab_id") or "").strip()),
            issue_code="collab_recovery_missing",
        )
    )
    return ExperienceCheckResult.from_observations(
        scenario,
        observations,
        output_excerpt=_clip(json.dumps({"fv_keys": sorted(fv.keys())}, indent=2)),
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
            scenario_id="what_is_cursor_direct",
            title="What is Cursor stays specific",
            description="Cursor definition questions should stay direct and answer the Cursor-specific concept.",
            category="routing",
            tags=["telegram", "meta", "direct"],
            suspected_files=[
                "services/andrea_sync/andrea_router.py",
                "services/andrea_sync/server.py",
                "services/andrea_sync/telegram_format.py",
            ],
            runner=_scenario_what_is_cursor,
        ),
        ExperienceScenario(
            scenario_id="direct_followups_avoid_history_leak",
            title="Direct followups stay specific instead of recycling chat context",
            description="A lightweight new question in the same Telegram chat should answer directly instead of replaying the latest useful thread.",
            category="routing",
            tags=["telegram", "direct", "history"],
            suspected_files=[
                "services/andrea_sync/andrea_router.py",
                "services/andrea_sync/server.py",
                "services/andrea_sync/telegram_continuation.py",
            ],
            runner=_scenario_direct_followups_avoid_history_leak,
        ),
        ExperienceScenario(
            scenario_id="cursor_primary_explicit_mention",
            title="@Cursor heavy-lift request becomes cursor_primary",
            description="Explicit heavy repo asks should queue into Cursor-primary collaboration instead of direct answer mode.",
            category="delegation",
            tags=["telegram", "cursor", "delegation", "delegated"],
            suspected_files=[
                "services/andrea_sync/andrea_router.py",
                "services/andrea_sync/server.py",
                "services/andrea_sync/telegram_format.py",
            ],
            runner=_scenario_cursor_primary,
        ),
        ExperienceScenario(
            scenario_id="cursor_primary_calm_completion",
            title="Cursor-primary completion stays calm and specific",
            description="Explicit Cursor heavy-lift requests should finish with calm final copy, bounded orchestration, and no runtime leakage.",
            category="delegation",
            tags=["telegram", "cursor", "delegation", "delegated", "calmness"],
            suspected_files=[
                "services/andrea_sync/server.py",
                "services/andrea_sync/telegram_format.py",
                "scripts/andrea_sync_openclaw_hybrid.py",
            ],
            runner=_scenario_cursor_primary_calm_completion,
        ),
        ExperienceScenario(
            scenario_id="collaborative_full_visibility_curated",
            title="Collaborative full-dialogue replay stays curated",
            description="Full-visibility OpenClaw and Cursor collaboration should expose a meaningful trace without runtime junk or excess chatter.",
            category="calmness",
            tags=["telegram", "collaboration", "delegated", "visibility", "calmness"],
            suspected_files=[
                "services/andrea_sync/server.py",
                "services/andrea_sync/telegram_format.py",
                "scripts/andrea_sync_openclaw_hybrid.py",
            ],
            runner=_scenario_collaborative_full_visibility,
        ),
        ExperienceScenario(
            scenario_id="masterclass_tri_llm_visibility",
            title="Masterclass tri-LLM kickoff stays collaborative and grounded",
            description="A deliberate masterclass sprint kickoff should preserve full visibility, keep Gemini as the preferred first lane, and still route the heavy repo work into Cursor.",
            category="delegation",
            tags=["telegram", "collaboration", "delegated", "visibility", "models"],
            suspected_files=[
                "services/andrea_sync/adapters/telegram.py",
                "services/andrea_sync/server.py",
                "scripts/andrea_sync_openclaw_hybrid.py",
            ],
            runner=_scenario_masterclass_tri_llm_visibility,
        ),
        ExperienceScenario(
            scenario_id="openclaw_repo_triage_stays_bounded",
            title="OpenClaw repo triage avoids unnecessary Cursor escalation",
            description="Delegated repo triage should stay inside OpenClaw when Cursor is not actually needed, and the orchestration should remain bounded.",
            category="delegation",
            tags=["telegram", "delegation", "delegated", "openclaw"],
            suspected_files=[
                "services/andrea_sync/server.py",
                "services/andrea_sync/telegram_format.py",
                "scripts/andrea_sync_openclaw_hybrid.py",
            ],
            runner=_scenario_openclaw_repo_triage_stays_bounded,
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
            scenario_id="recent_text_messages_via_bluebubbles",
            title="Recent text-message asks use the BlueBubbles lane",
            description="Inbox-style text-message requests should stay direct, use the verified BlueBubbles lane, and avoid delegated lifecycle noise.",
            category="capability_truth",
            tags=["telegram", "bluebubbles", "direct", "messages"],
            suspected_files=[
                "services/andrea_sync/server.py",
                "services/andrea_sync/andrea_router.py",
                "services/andrea_sync/user_surface.py",
            ],
            runner=_scenario_recent_text_messages_via_bluebubbles,
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
        ExperienceScenario(
            scenario_id="text_messages_from_today_via_bluebubbles",
            title="From-today text-message asks use the BlueBubbles lane",
            description="Inbox-style asks that mention today should still route through the structured recent-messages lane.",
            category="capability_truth",
            tags=["telegram", "bluebubbles", "direct", "messages"],
            suspected_files=[
                "services/andrea_sync/server.py",
                "services/andrea_sync/andrea_router.py",
            ],
            runner=_scenario_text_messages_from_today_via_bluebubbles,
        ),
        ExperienceScenario(
            scenario_id="recent_text_shorthand_followup_same_thread",
            title="Recent-text shorthand follow-ups stay structured",
            description=(
                "After an explicit recent-text-messages ask, a narrow follow-up like `from today?` should "
                "reuse the BlueBubbles structured lane instead of falling through to generic direct routing."
            ),
            category="capability_truth",
            tags=["telegram", "bluebubbles", "direct", "messages", "followup"],
            suspected_files=[
                "services/andrea_sync/server.py",
                "services/andrea_sync/andrea_router.py",
            ],
            runner=_scenario_recent_text_shorthand_followup_same_thread,
        ),
        ExperienceScenario(
            scenario_id="structured_lookup_rejects_provider_leak_summary",
            title="Structured lookups reject embedding and quota leakage",
            description="When OpenClaw returns provider-quota style text, Andrea must fail closed instead of echoing it.",
            category="calmness",
            tags=["user-surface", "openclaw", "direct"],
            suspected_files=[
                "services/andrea_sync/server.py",
                "services/andrea_sync/user_surface.py",
            ],
            runner=_scenario_structured_lookup_rejects_provider_leak_summary,
        ),
        ExperienceScenario(
            scenario_id="bounded_collaboration_verify_fail_repo",
            title="Verify-fail records bounded collaboration metadata",
            description=(
                "For repoHelpVerified delegated runs, a failing verification pass should persist "
                "collaboration/repair strategist metadata without breaking plan storage."
            ),
            category="trust",
            tags=["plan", "collaboration", "verification", "repo"],
            suspected_files=[
                "services/andrea_sync/plan_runtime.py",
                "services/andrea_sync/arbitration_policy.py",
                "services/andrea_sync/collaboration_schema.py",
            ],
            runner=_scenario_bounded_collaboration_verify_fail_repo,
        ),
        ExperienceScenario(
            scenario_id="trusted_layer_unsafe_request",
            title="Trusted layer marks unsafe asks as unsupported",
            description="Scenario resolver should classify clearly unsafe intents as unsupported with calm refusal copy available.",
            category="trust",
            tags=["scenario", "safety", "unsupported"],
            suspected_files=[
                "services/andrea_sync/scenario_runtime.py",
                "services/andrea_sync/scenario_registry.py",
                "services/andrea_sync/server.py",
            ],
            runner=_scenario_trusted_layer_unsafe_request,
        ),
        ExperienceScenario(
            scenario_id="trusted_layer_outbound_blocks_delegate",
            title="Outbound draft scenarios block auto delegation",
            description="Approval-required outbound jobs stay draft-only: resolver + block helper prevent silent delegated execution.",
            category="trust",
            tags=["scenario", "outbound", "delegation"],
            suspected_files=[
                "services/andrea_sync/scenario_runtime.py",
                "services/andrea_sync/server.py",
            ],
            runner=_scenario_trusted_layer_outbound_blocks_delegate,
        ),
        ExperienceScenario(
            scenario_id="trusted_layer_verification_sensitive_approval",
            title="Verification-sensitive language forces approval",
            description="Explicit proof-before-done language maps to verificationSensitiveAction and triggers scenario approval policy.",
            category="trust",
            tags=["scenario", "verification", "approval"],
            suspected_files=[
                "services/andrea_sync/scenario_runtime.py",
                "services/andrea_sync/approval_policy.py",
            ],
            runner=_scenario_trusted_layer_verification_sensitive_forces_approval,
        ),
        ExperienceScenario(
            scenario_id="trusted_layer_false_support_guard",
            title="Status follow-ups are not falsely unsupported",
            description="Regression guard: normal status language must remain in the supported status/follow-up scenario family.",
            category="trust",
            tags=["scenario", "status", "regression"],
            suspected_files=[
                "services/andrea_sync/scenario_runtime.py",
            ],
            runner=_scenario_trusted_layer_false_support_guard,
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


def blueprint_platform_health(conn: Any) -> Dict[str, Any]:
    """Structural reachability check for blueprint tables (Phase 6 observability hook)."""
    try:
        conn.execute("SELECT 1 FROM goals LIMIT 1").fetchone()
        conn.execute("SELECT 1 FROM workflows LIMIT 1").fetchone()
        conn.execute("SELECT 1 FROM task_goals LIMIT 1").fetchone()
        conn.execute("SELECT 1 FROM goal_artifacts LIMIT 1").fetchone()
        return {"ok": True, "tables_reachable": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "tables_reachable": False}

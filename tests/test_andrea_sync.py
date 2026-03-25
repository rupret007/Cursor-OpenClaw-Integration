"""Unit tests for Andrea lockstep (no live HTTP)."""

from __future__ import annotations

import json
import os
import sys
from typing import Any
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.adapters import alexa as alexa_adapt  # noqa: E402
from services.andrea_sync.adapters import telegram as tg_adapt  # noqa: E402
from services.andrea_sync.bus import handle_command  # noqa: E402
from services.andrea_sync.policy import evaluate_skill_absence_claim  # noqa: E402
from services.andrea_sync.projector import project_task_dict  # noqa: E402
from services.andrea_sync.schema import CommandType, EventType, TaskStatus, normalize_idempotency_base  # noqa: E402
from services.andrea_sync.store import (  # noqa: E402
    SYSTEM_TASK_ID,
    append_event,
    connect,
    create_goal,
    create_goal_approval,
    get_meta,
    link_task_principal,
    link_task_to_goal,
    list_tasks,
    list_telegram_task_ids_for_chat,
    load_events_for_task,
    load_recent_telegram_history,
    migrate,
    set_meta,
)
from services.andrea_sync.andrea_router import (  # noqa: E402
    _scrub_history_for_direct,
    build_direct_reply,
    classify_route,
    is_generic_direct_reply,
    is_standalone_casual_social_turn,
    route_message,
)
from services.andrea_sync.user_surface import is_stale_openclaw_narrative  # noqa: E402
from services.andrea_sync.telegram_format import (  # noqa: E402
    format_ack_message,
    format_direct_message,
    format_final_message,
    format_progress_message,
    format_running_message,
)


class TestAndreaSync(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = Path(self._tmp.name)
        self._prev_db = os.environ.get("ANDREA_SYNC_DB")
        self._prev_background = os.environ.get("ANDREA_SYNC_BACKGROUND_ENABLED")
        self._prev_notifier = os.environ.get("ANDREA_SYNC_TELEGRAM_NOTIFIER")
        self._prev_telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self._prev_telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        self._prev_public_base = os.environ.get("ANDREA_SYNC_PUBLIC_BASE")
        self._prev_webhook_autofix = os.environ.get("ANDREA_SYNC_TELEGRAM_WEBHOOK_AUTOFIX")
        self._prev_telegram_auto_cursor = os.environ.get("ANDREA_SYNC_TELEGRAM_AUTO_CURSOR")
        self._prev_delegated_execution = os.environ.get("ANDREA_SYNC_DELEGATED_EXECUTION_ENABLED")
        self._prev_openai_enabled = os.environ.get("OPENAI_API_ENABLED")
        self._prev_openai_key = os.environ.get("OPENAI_API_KEY")
        self._prev_delegate_lane = os.environ.get("ANDREA_TELEGRAM_DELEGATE_LANE")
        self._prev_openclaw_refresh_mode = os.environ.get("ANDREA_OPENCLAW_REFRESH_MODE")
        os.environ["ANDREA_SYNC_DB"] = str(self.db_path)
        os.environ["ANDREA_SYNC_PUBLIC_BASE"] = ""
        os.environ["ANDREA_SYNC_TELEGRAM_WEBHOOK_AUTOFIX"] = "0"
        self.conn = connect(self.db_path)
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        if self._prev_db is None:
            os.environ.pop("ANDREA_SYNC_DB", None)
        else:
            os.environ["ANDREA_SYNC_DB"] = self._prev_db
        if self._prev_background is None:
            os.environ.pop("ANDREA_SYNC_BACKGROUND_ENABLED", None)
        else:
            os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = self._prev_background
        if self._prev_notifier is None:
            os.environ.pop("ANDREA_SYNC_TELEGRAM_NOTIFIER", None)
        else:
            os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = self._prev_notifier
        if self._prev_telegram_bot_token is None:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        else:
            os.environ["TELEGRAM_BOT_TOKEN"] = self._prev_telegram_bot_token
        if self._prev_telegram_chat_id is None:
            os.environ.pop("TELEGRAM_CHAT_ID", None)
        else:
            os.environ["TELEGRAM_CHAT_ID"] = self._prev_telegram_chat_id
        if self._prev_public_base is None:
            os.environ.pop("ANDREA_SYNC_PUBLIC_BASE", None)
        else:
            os.environ["ANDREA_SYNC_PUBLIC_BASE"] = self._prev_public_base
        if self._prev_webhook_autofix is None:
            os.environ.pop("ANDREA_SYNC_TELEGRAM_WEBHOOK_AUTOFIX", None)
        else:
            os.environ["ANDREA_SYNC_TELEGRAM_WEBHOOK_AUTOFIX"] = self._prev_webhook_autofix
        if self._prev_telegram_auto_cursor is None:
            os.environ.pop("ANDREA_SYNC_TELEGRAM_AUTO_CURSOR", None)
        else:
            os.environ["ANDREA_SYNC_TELEGRAM_AUTO_CURSOR"] = self._prev_telegram_auto_cursor
        if self._prev_delegated_execution is None:
            os.environ.pop("ANDREA_SYNC_DELEGATED_EXECUTION_ENABLED", None)
        else:
            os.environ["ANDREA_SYNC_DELEGATED_EXECUTION_ENABLED"] = self._prev_delegated_execution
        if self._prev_openai_enabled is None:
            os.environ.pop("OPENAI_API_ENABLED", None)
        else:
            os.environ["OPENAI_API_ENABLED"] = self._prev_openai_enabled
        if self._prev_openai_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._prev_openai_key
        if self._prev_delegate_lane is None:
            os.environ.pop("ANDREA_TELEGRAM_DELEGATE_LANE", None)
        else:
            os.environ["ANDREA_TELEGRAM_DELEGATE_LANE"] = self._prev_delegate_lane
        if self._prev_openclaw_refresh_mode is None:
            os.environ.pop("ANDREA_OPENCLAW_REFRESH_MODE", None)
        else:
            os.environ["ANDREA_OPENCLAW_REFRESH_MODE"] = self._prev_openclaw_refresh_mode
        self.db_path.unlink(missing_ok=True)
        for suf in ("-wal", "-shm"):
            p = Path(str(self.db_path) + suf)
            p.unlink(missing_ok=True)
        Path(str(self.db_path) + ".kill").unlink(missing_ok=True)

    def test_idempotency_duplicate_command(self) -> None:
        body = {
            "command_type": CommandType.CREATE_TASK.value,
            "channel": "cli",
            "external_id": "e1",
            "payload": {"summary": "hello"},
        }
        r1 = handle_command(self.conn, body)
        r2 = handle_command(self.conn, body)
        self.assertTrue(r1.get("ok"))
        self.assertTrue(r2.get("ok"))
        self.assertEqual(r1["task_id"], r2["task_id"])
        self.assertFalse(r1.get("deduped"))
        self.assertTrue(r2.get("deduped"))

    def test_cursor_job_projection(self) -> None:
        r = handle_command(
            self.conn,
            {
                "command_type": CommandType.CREATE_CURSOR_JOB.value,
                "channel": "telegram",
                "external_id": "m1",
                "payload": {"prompt": "fix bug", "summary": "fix"},
            },
        )
        self.assertTrue(r.get("ok"))
        tid = r["task_id"]
        handle_command(
            self.conn,
            {
                "command_type": CommandType.REPORT_CURSOR_EVENT.value,
                "channel": "cursor",
                "task_id": tid,
                "payload": {
                    "event_type": EventType.JOB_STARTED.value,
                    "payload": {"cursor_agent_id": "bc-test"},
                },
            },
        )
        handle_command(
            self.conn,
            {
                "command_type": CommandType.REPORT_CURSOR_EVENT.value,
                "channel": "cursor",
                "task_id": tid,
                "payload": {
                    "event_type": EventType.JOB_COMPLETED.value,
                    "payload": {"summary": "done"},
                },
            },
        )
        proj = project_task_dict(self.conn, tid, "telegram")
        self.assertEqual(proj["status"], TaskStatus.COMPLETED.value)
        self.assertEqual(proj["cursor_agent_id"], "bc-test")

    @mock.patch(
        "services.andrea_sync.execution_runtime.submit_agent_followup_payload",
        return_value={"ok": True, "returncode": 0, "outer": {}, "response": {}},
    )
    def test_cursor_followup_command_runs_execution_runtime(self, _m: mock.MagicMock) -> None:
        from services.andrea_sync.store import create_execution_attempt

        created = handle_command(
            self.conn,
            {
                "command_type": CommandType.CREATE_TASK.value,
                "channel": "cli",
                "external_id": "cfollow-1",
                "payload": {"summary": "delegated"},
            },
        )
        self.assertTrue(created.get("ok"), created)
        tid = str(created["task_id"])
        create_execution_attempt(
            self.conn,
            tid,
            "",
            lane="direct_cursor",
            backend="cursor",
            handle_dict={"cursor_agent_id": "ag_cf"},
        )
        r = handle_command(
            self.conn,
            {
                "command_type": CommandType.CURSOR_FOLLOWUP.value,
                "channel": "cursor",
                "task_id": tid,
                "payload": {"prompt": "Please continue"},
            },
        )
        self.assertTrue(r.get("ok"), r)
        ev = load_events_for_task(self.conn, tid)
        self.assertTrue(
            any(
                e[2] == EventType.JOB_PROGRESS.value
                and "cursor_followup" in str((e[3] or {}).get("message", ""))
                for e in ev
            ),
            ev,
        )

    def test_projection_outcome_marks_overdelegated_meta_question(self) -> None:
        created = handle_command(
            self.conn,
            {
                "command_type": CommandType.CREATE_TASK.value,
                "channel": "telegram",
                "external_id": "outcome-meta-q",
                "payload": {"summary": "Is this OpenClaw?"},
            },
        )
        tid = created["task_id"]
        append_event(
            self.conn,
            tid,
            EventType.USER_MESSAGE,
            {
                "text": "Is this OpenClaw?",
                "routing_text": "Is this OpenClaw?",
                "channel": "telegram",
                "chat_id": 55,
                "message_id": 100,
            },
        )
        append_event(
            self.conn,
            tid,
            EventType.JOB_QUEUED,
            {
                "kind": "openclaw",
                "execution_lane": "openclaw_hybrid",
                "runner": "openclaw",
                "route_reason": "stack_or_tooling_question",
                "visibility_mode": "summary",
                "collaboration_mode": "auto",
            },
        )
        proj = project_task_dict(self.conn, tid, "telegram")
        outcome = proj["meta"]["outcome"]
        self.assertEqual(outcome["route_mode"], "delegate")
        self.assertIn("overdelegated_meta_question", outcome["ux_flags"])
        self.assertTrue(outcome["optimization_candidate"])

    def test_submit_user_feedback_updates_projection_outcome(self) -> None:
        created = handle_command(
            self.conn,
            {
                "command_type": CommandType.CREATE_TASK.value,
                "channel": "telegram",
                "external_id": "feedback-direct",
                "payload": {"summary": "hello"},
            },
        )
        tid = created["task_id"]
        append_event(
            self.conn,
            tid,
            EventType.ASSISTANT_REPLIED,
            {
                "text": "Andrea answered directly.",
                "route": "direct",
                "reason": "greeting_or_social",
            },
        )
        result = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_FEEDBACK.value,
                "channel": "telegram",
                "task_id": tid,
                "payload": {
                    "label": "negative",
                    "comment": "That felt too noisy.",
                    "source": "telegram",
                },
            },
        )
        self.assertTrue(result.get("ok"))
        proj = project_task_dict(self.conn, tid, "telegram")
        self.assertEqual(proj["meta"]["feedback"]["count"], 1)
        self.assertEqual(proj["meta"]["feedback"]["last_label"], "negative")
        self.assertIn("negative_feedback", proj["meta"]["outcome"]["ux_flags"])
        self.assertEqual(proj["meta"]["outcome"]["feedback_average"], -1.0)

    def test_projection_outcome_marks_blocked_capability_and_runtime_trace(self) -> None:
        created = handle_command(
            self.conn,
            {
                "command_type": CommandType.CREATE_TASK.value,
                "channel": "telegram",
                "external_id": "blocked-capability",
                "payload": {"summary": "Need Cursor coordination"},
            },
        )
        tid = created["task_id"]
        append_event(
            self.conn,
            tid,
            EventType.JOB_COMPLETED,
            {
                "summary": "I hit an internal collaboration limitation while trying to pass work between reasoning lanes.",
                "user_summary": "I hit an internal collaboration limitation while trying to pass work between reasoning lanes.",
                "backend": "openclaw",
                "runner": "openclaw",
                "blocked_reason": "I hit an internal collaboration limitation while trying to pass work between reasoning lanes.",
                "internal_trace": "sessions_spawn.attachments.enabled is disabled.",
                "collaboration_trace": ["OpenClaw prepared the first pass.", "Andrea kept the fallback calm."],
            },
        )
        proj = project_task_dict(self.conn, tid, "telegram")
        outcome = proj["meta"]["outcome"]
        self.assertIn("blocked_capability", outcome["ux_flags"])
        self.assertIn("internal_runtime_trace", outcome["ux_flags"])
        self.assertEqual(outcome["collaboration_trace_count"], 2)
        self.assertIn("blocked_reason", outcome)

    def test_projection_outcome_tracks_orchestration_and_proactive_failures(self) -> None:
        created = handle_command(
            self.conn,
            {
                "command_type": CommandType.CREATE_TASK.value,
                "channel": "telegram",
                "external_id": "orchestration-failure",
                "payload": {"summary": "Need a repo fix"},
            },
        )
        tid = created["task_id"]
        append_event(
            self.conn,
            tid,
            EventType.ORCHESTRATION_STEP,
            {
                "phase": "plan",
                "status": "completed",
                "lane": "openclaw",
                "summary": "OpenClaw built the initial plan.",
            },
        )
        append_event(
            self.conn,
            tid,
            EventType.ORCHESTRATION_STEP,
            {
                "phase": "execution",
                "status": "failed",
                "lane": "cursor",
                "summary": "Cursor failed during execution.",
            },
        )
        append_event(
            self.conn,
            tid,
            EventType.REMINDER_FAILED,
            {
                "reminder_id": "rem_demo",
                "error": "delivery_failed",
            },
        )
        proj = project_task_dict(self.conn, tid, "telegram")
        outcome = proj["meta"]["outcome"]
        self.assertEqual(outcome["planner_steps"], 1)
        self.assertEqual(outcome["executor_steps"], 0)
        self.assertEqual(outcome["failed_orchestration_phase"], "execution")
        self.assertIn("executor_failure", outcome["ux_flags"])
        self.assertIn("proactive_delivery_failed", outcome["ux_flags"])

    def test_run_optimization_cycle_internal_command_records_system_audit(self) -> None:
        created = handle_command(
            self.conn,
            {
                "command_type": CommandType.CREATE_TASK.value,
                "channel": "telegram",
                "external_id": "optimizer-overdelegation",
                "payload": {"summary": "Is this OpenClaw?"},
            },
        )
        tid = created["task_id"]
        append_event(
            self.conn,
            tid,
            EventType.USER_MESSAGE,
            {
                "text": "Is this OpenClaw?",
                "routing_text": "Is this OpenClaw?",
                "channel": "telegram",
                "chat_id": 77,
                "message_id": 200,
            },
        )
        append_event(
            self.conn,
            tid,
            EventType.JOB_QUEUED,
            {
                "kind": "openclaw",
                "execution_lane": "openclaw_hybrid",
                "runner": "openclaw",
                "route_reason": "stack_or_tooling_question",
                "visibility_mode": "summary",
                "collaboration_mode": "auto",
            },
        )
        append_event(
            self.conn,
            tid,
            EventType.JOB_COMPLETED,
            {
                "summary": "Delegated path completed.",
                "backend": "openclaw",
                "runner": "openclaw",
            },
        )

        result = handle_command(
            self.conn,
            {
                "command_type": CommandType.RUN_OPTIMIZATION_CYCLE.value,
                "channel": "internal",
                "payload": {
                    "limit": 20,
                    "regression_report": {"passed": True, "total": 10},
                    "emit_proposals": True,
                },
            },
        )
        self.assertTrue(result.get("ok"))
        self.assertEqual(result["task_id"], SYSTEM_TASK_ID)
        self.assertTrue(any(row["category"] == "overdelegation" for row in result["findings"]))
        self.assertTrue(any(row["category"] == "overdelegation" for row in result["proposals"]))

        events = load_events_for_task(self.conn, SYSTEM_TASK_ID)
        event_types = [event_type for _seq, _ts, event_type, _payload in events]
        self.assertIn(EventType.OPTIMIZATION_RUN_COMPLETED.value, event_types)
        self.assertIn(EventType.EVALUATION_RECORDED.value, event_types)
        self.assertIn(EventType.OPTIMIZATION_PROPOSAL.value, event_types)

    def test_unknown_channel_rejected(self) -> None:
        r = handle_command(
            self.conn,
            {
                "command_type": CommandType.CREATE_TASK.value,
                "channel": "not-a-real-channel",
                "external_id": "x1",
                "payload": {"summary": "nope"},
            },
        )
        self.assertFalse(r.get("ok"))
        self.assertIn("unknown channel", str(r.get("error", "")).lower())

    def test_normalize_idempotency_base_stable(self) -> None:
        a = normalize_idempotency_base("telegram", "42", "SubmitUserMessage")
        b = normalize_idempotency_base("telegram", "42", "SubmitUserMessage")
        self.assertEqual(a, b)
        self.assertNotEqual(
            a, normalize_idempotency_base("telegram", "43", "SubmitUserMessage")
        )

    def test_telegram_update_to_command(self) -> None:
        cmd = tg_adapt.update_to_command(
            {
                "update_id": 99,
                "message": {
                    "text": "hello",
                    "message_id": 55,
                    "chat": {"id": 1},
                    "from": {"id": 2, "username": "demo"},
                },
            }
        )
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd["command_type"], "SubmitUserMessage")
        self.assertEqual(cmd["external_id"], "99")
        self.assertFalse(cmd["payload"]["auto_cursor_job"])
        self.assertEqual(cmd["payload"]["message_id"], 55)
        self.assertEqual(cmd["payload"]["from_username"], "demo")
        self.assertNotIn("message_thread_id", cmd["payload"])

    def test_telegram_update_to_command_includes_reply_to_message_id(self) -> None:
        cmd = tg_adapt.update_to_command(
            {
                "update_id": 100,
                "message": {
                    "text": "follow up",
                    "message_id": 56,
                    "reply_to_message": {"message_id": 55},
                    "chat": {"id": 1},
                    "from": {"id": 2, "username": "demo"},
                },
            }
        )
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd["payload"]["reply_to_message_id"], 55)

    def test_telegram_continuation_skips_mismatched_forum_thread_id(self) -> None:
        from services.andrea_sync.telegram_continuation import attach_continuation_if_applicable

        prev = os.environ.get("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS")
        os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = "300"
        try:
            cmd1 = tg_adapt.update_to_command(
                {
                    "update_id": 8801,
                    "message": {
                        "text": "forum part one",
                        "message_id": 501,
                        "message_thread_id": 111,
                        "chat": {"id": 8888, "type": "supergroup"},
                        "from": {"id": 42},
                    },
                }
            )
            assert cmd1
            r1 = handle_command(self.conn, cmd1)
            tid = r1["task_id"]
            cmd2 = tg_adapt.update_to_command(
                {
                    "update_id": 8802,
                    "message": {
                        "text": "forum part two",
                        "message_id": 502,
                        "message_thread_id": 222,
                        "chat": {"id": 8888, "type": "supergroup"},
                        "from": {"id": 42},
                    },
                }
            )
            assert cmd2
            self.assertFalse(attach_continuation_if_applicable(self.conn, cmd2))
            r2 = handle_command(self.conn, cmd2)
            self.assertNotEqual(r2["task_id"], tid)
        finally:
            if prev is None:
                os.environ.pop("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS", None)
            else:
                os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = prev

    def test_telegram_update_to_command_includes_forum_message_thread_id(self) -> None:
        cmd = tg_adapt.update_to_command(
            {
                "update_id": 101,
                "message": {
                    "text": "hello topic",
                    "message_id": 56,
                    "message_thread_id": 424242,
                    "chat": {"id": -100123, "type": "supergroup", "is_forum": True},
                    "from": {"id": 2},
                },
            }
        )
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd["payload"]["message_thread_id"], 424242)

    def test_telegram_extract_routing_hints(self) -> None:
        routing = tg_adapt.extract_routing_hints("@Andrea @Cursor please work together on this")
        self.assertEqual(routing["routing_hint"], "collaborate")
        self.assertEqual(routing["collaboration_mode"], "collaborative")
        self.assertEqual(routing["mention_targets"], ["andrea", "cursor"])
        self.assertEqual(routing["routing_text"], "please work together on this")
        self.assertEqual(routing["visibility_mode"], "summary")
        self.assertEqual(routing["requested_capability"], "collaboration")

    def test_telegram_extract_routing_hints_detects_full_dialogue_mode(self) -> None:
        routing = tg_adapt.extract_routing_hints(
            "@Andrea @Cursor work together and show the full dialogue while you do it"
        )
        self.assertEqual(routing["routing_hint"], "collaborate")
        self.assertEqual(routing["collaboration_mode"], "collaborative")
        self.assertEqual(routing["visibility_mode"], "full")
        self.assertEqual(routing["requested_capability"], "collaboration")

    def test_telegram_extract_routing_hints_masterclass_with_collaboration_sets_full_visibility(
        self,
    ) -> None:
        routing = tg_adapt.extract_routing_hints(
            "@Andrea @Cursor I want a disciplined masterclass sprint on this repo"
        )
        self.assertEqual(routing["routing_hint"], "collaborate")
        self.assertEqual(routing["visibility_mode"], "full")

    def test_telegram_extract_routing_hints_masterclass_with_two_models_sets_full_visibility(
        self,
    ) -> None:
        routing = tg_adapt.extract_routing_hints(
            "@Gemini @Minimax run a masterclass-style review of the plan"
        )
        self.assertEqual(routing["visibility_mode"], "full")
        self.assertEqual(routing["model_mentions"], ["gemini", "minimax"])

    def test_telegram_extract_routing_hints_masterclass_alone_stays_summary(self) -> None:
        routing = tg_adapt.extract_routing_hints(
            "That keynote was a real masterclass on distributed systems"
        )
        self.assertEqual(routing["visibility_mode"], "summary")
        self.assertEqual(routing["routing_hint"], "auto")

    def test_telegram_extract_routing_hints_detects_direct_model_lane(self) -> None:
        routing = tg_adapt.extract_routing_hints("@Gemini review this plan with Andrea")
        self.assertEqual(routing["preferred_model_family"], "gemini")
        self.assertEqual(routing["preferred_model_label"], "Gemini")
        self.assertEqual(routing["model_mentions"], ["gemini"])
        self.assertEqual(routing["routing_text"], "review this plan with Andrea")
        self.assertEqual(routing["requested_capability"], "assistant")

    def test_telegram_webhook_url_match_ignores_query_order(self) -> None:
        self.assertTrue(
            tg_adapt.webhook_urls_match(
                "https://example.com/v1/telegram/webhook?x=1&secret=abc",
                "https://example.com/v1/telegram/webhook?secret=abc&x=1",
            )
        )

    def test_telegram_webhook_url_match_detects_drift(self) -> None:
        self.assertFalse(
            tg_adapt.webhook_urls_match(
                "https://wrong.example/v1/telegram/webhook?secret=abc",
                "https://example.com/v1/telegram/webhook?secret=abc",
            )
        )

    def test_send_text_message_rejects_telegram_ok_false_body(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b'{"ok": false, "description": "chat not found"}'
        response.__enter__.return_value = response
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                tg_adapt.send_text_message(bot_token="token", chat_id=1, text="hello")
        self.assertIn("telegram sendMessage rejected", str(ctx.exception))

    def test_send_text_message_sends_reply_parameters_and_forum_thread(self) -> None:
        captured: dict[str, Any] = {}

        def fake_urlopen(req, timeout=20):  # noqa: ANN001
            captured["body"] = json.loads(req.data.decode("utf-8"))
            response = mock.MagicMock()
            response.read.return_value = b'{"ok": true, "result": {}}'
            response.__enter__.return_value = response
            return response

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            tg_adapt.send_text_message(
                bot_token="token",
                chat_id=-1001,
                text="status",
                reply_to_message_id=77,
                message_thread_id=99,
            )
        body = captured["body"]
        self.assertEqual(body["message_thread_id"], 99)
        self.assertEqual(body["reply_parameters"]["message_id"], 77)
        self.assertTrue(body["reply_parameters"]["allow_sending_without_reply"])

    def test_telegram_update_to_command_with_cursor_mention(self) -> None:
        cmd = tg_adapt.update_to_command(
            {
                "update_id": 100,
                "message": {
                    "text": "@Cursor please fix the failing tests",
                    "message_id": 56,
                    "chat": {"id": 1},
                    "from": {"id": 2, "username": "demo"},
                },
            }
        )
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd["payload"]["routing_hint"], "cursor")
        self.assertEqual(cmd["payload"]["collaboration_mode"], "cursor_primary")
        self.assertEqual(cmd["payload"]["routing_text"], "please fix the failing tests")

    def test_telegram_user_message_auto_queues_cursor_job(self) -> None:
        result = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-1",
                "payload": {
                    "text": "review the bridge",
                    "chat_id": 777,
                    "chat_type": "private",
                    "message_id": 42,
                    "from_user": 88,
                    "from_username": "andrea",
                    "auto_cursor_job": True,
                },
            },
        )
        self.assertTrue(result.get("ok"))
        self.assertTrue(result.get("queued_cursor_job"))
        proj = project_task_dict(self.conn, result["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.QUEUED.value)
        self.assertEqual(proj["meta"]["telegram"]["chat_id"], 777)
        self.assertEqual(proj["meta"]["telegram"]["message_id"], 42)
        self.assertEqual(proj["meta"]["cursor"]["prompt_excerpt"], "review the bridge")

    def test_telegram_continuation_reply_to_anchor_still_merges_split_prompt(self) -> None:
        """Split prompt with explicit reply-to-anchor must still merge (reply overrides '?')."""
        from services.andrea_sync.telegram_continuation import attach_continuation_if_applicable

        prev = os.environ.get("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS")
        os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = "300"
        try:
            cmd1 = tg_adapt.update_to_command(
                {
                    "update_id": 501,
                    "message": {
                        "text": "@Andrea section A of the spec",
                        "message_id": 1001,
                        "chat": {"id": 4242, "type": "private"},
                        "from": {"id": 99, "username": "u1"},
                    },
                }
            )
            self.assertIsNotNone(cmd1)
            assert cmd1 is not None
            r1 = handle_command(self.conn, cmd1)
            tid = r1["task_id"]

            cmd2 = tg_adapt.update_to_command(
                {
                    "update_id": 502,
                    "message": {
                        "text": "Section B: acceptance criteria?",
                        "message_id": 1002,
                        "chat": {"id": 4242, "type": "private"},
                        "from": {"id": 99, "username": "u1"},
                        "reply_to_message": {"message_id": 1001},
                    },
                }
            )
            self.assertIsNotNone(cmd2)
            assert cmd2 is not None
            self.assertTrue(attach_continuation_if_applicable(self.conn, cmd2))
            self.assertEqual(cmd2["task_id"], tid)
        finally:
            if prev is None:
                os.environ.pop("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS", None)
            else:
                os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = prev

    def test_telegram_continuation_merges_second_chunk_same_task(self) -> None:
        from services.andrea_sync.telegram_continuation import attach_continuation_if_applicable

        prev = os.environ.get("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS")
        os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = "300"
        try:
            cmd1 = tg_adapt.update_to_command(
                {
                    "update_id": 501,
                    "message": {
                        "text": "@Andrea @Cursor please collaborate on repo cleanup part one",
                        "message_id": 1001,
                        "chat": {"id": 4242, "type": "private"},
                        "from": {"id": 99, "username": "u1"},
                    },
                }
            )
            self.assertIsNotNone(cmd1)
            assert cmd1 is not None
            r1 = handle_command(self.conn, cmd1)
            self.assertTrue(r1.get("ok"))
            tid = r1["task_id"]

            cmd2 = tg_adapt.update_to_command(
                {
                    "update_id": 502,
                    "message": {
                        "text": "Definition of done: tests pass and docs updated.",
                        "message_id": 1002,
                        "chat": {"id": 4242, "type": "private"},
                        "from": {"id": 99, "username": "u1"},
                    },
                }
            )
            self.assertIsNotNone(cmd2)
            assert cmd2 is not None
            self.assertTrue(attach_continuation_if_applicable(self.conn, cmd2))
            self.assertEqual(cmd2["task_id"], tid)
            self.assertTrue(cmd2["payload"].get("telegram_continuation"))
            self.assertEqual(cmd2["payload"]["collaboration_mode"], "collaborative")
            self.assertEqual(cmd2["payload"]["routing_hint"], "collaborate")

            r2 = handle_command(self.conn, cmd2)
            self.assertTrue(r2.get("ok"))
            self.assertEqual(r2["task_id"], tid)

            proj = project_task_dict(self.conn, tid, "telegram")
            self.assertEqual(proj["meta"]["telegram"].get("continuation_count"), 1)
            self.assertEqual(proj["meta"]["telegram"].get("first_user_message_id"), 1001)
            self.assertEqual(proj["meta"]["telegram"].get("message_id"), 1002)
            acc = str(proj["meta"]["telegram"].get("accumulated_prompt") or "")
            self.assertIn("part one", acc.lower())
            self.assertIn("definition of done", acc.lower())
        finally:
            if prev is None:
                os.environ.pop("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS", None)
            else:
                os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = prev

    def test_telegram_continuation_does_not_merge_new_cursor_mention(self) -> None:
        from services.andrea_sync.telegram_continuation import attach_continuation_if_applicable

        prev = os.environ.get("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS")
        os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = "300"
        try:
            cmd1 = tg_adapt.update_to_command(
                {
                    "update_id": 601,
                    "message": {
                        "text": "@Andrea just a question about the plan",
                        "message_id": 2001,
                        "chat": {"id": 9191, "type": "private"},
                        "from": {"id": 77, "username": "u2"},
                    },
                }
            )
            assert cmd1
            r1 = handle_command(self.conn, cmd1)
            tid1 = r1["task_id"]

            cmd2 = tg_adapt.update_to_command(
                {
                    "update_id": 602,
                    "message": {
                        "text": "@Cursor please implement the feature now",
                        "message_id": 2002,
                        "chat": {"id": 9191, "type": "private"},
                        "from": {"id": 77, "username": "u2"},
                    },
                }
            )
            assert cmd2
            self.assertFalse(attach_continuation_if_applicable(self.conn, cmd2))
            self.assertIsNone(cmd2.get("task_id"))

            r2 = handle_command(self.conn, cmd2)
            self.assertTrue(r2.get("ok"))
            self.assertNotEqual(r2["task_id"], tid1)
        finally:
            if prev is None:
                os.environ.pop("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS", None)
            else:
                os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = prev

    def test_telegram_continuation_plain_new_message_does_not_merge_active_task(self) -> None:
        from services.andrea_sync.telegram_continuation import attach_continuation_if_applicable

        prev = os.environ.get("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS")
        os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = "300"
        try:
            cmd1 = tg_adapt.update_to_command(
                {
                    "update_id": 611,
                    "message": {
                        "text": "@Andrea @Cursor please collaborate on repo cleanup",
                        "message_id": 2101,
                        "chat": {"id": 9292, "type": "private"},
                        "from": {"id": 88, "username": "u3"},
                    },
                }
            )
            assert cmd1
            r1 = handle_command(self.conn, cmd1)
            tid1 = r1["task_id"]
            append_event(
                self.conn,
                tid1,
                EventType.JOB_QUEUED,
                {
                    "kind": "openclaw",
                    "execution_lane": "openclaw_hybrid",
                    "runner": "openclaw",
                },
            )

            cmd2 = tg_adapt.update_to_command(
                {
                    "update_id": 612,
                    "message": {
                        "text": "Is this OpenClaw?",
                        "message_id": 2102,
                        "chat": {"id": 9292, "type": "private"},
                        "from": {"id": 88, "username": "u3"},
                    },
                }
            )
            assert cmd2
            self.assertFalse(attach_continuation_if_applicable(self.conn, cmd2))
            self.assertIsNone(cmd2.get("task_id"))
        finally:
            if prev is None:
                os.environ.pop("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS", None)
            else:
                os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = prev

    def test_telegram_continuation_hi_andrea_does_not_merge_queued_collab_task(self) -> None:
        from services.andrea_sync.telegram_continuation import attach_continuation_if_applicable

        prev = os.environ.get("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS")
        os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = "300"
        try:
            cmd1 = tg_adapt.update_to_command(
                {
                    "update_id": 613,
                    "message": {
                        "text": "@Andrea @Cursor please collaborate on repo cleanup",
                        "message_id": 2111,
                        "chat": {"id": 9293, "type": "private"},
                        "from": {"id": 88, "username": "u3b"},
                    },
                }
            )
            assert cmd1
            r1 = handle_command(self.conn, cmd1)
            tid1 = r1["task_id"]
            append_event(
                self.conn,
                tid1,
                EventType.JOB_QUEUED,
                {
                    "kind": "openclaw",
                    "execution_lane": "openclaw_hybrid",
                    "runner": "openclaw",
                },
            )

            cmd2 = tg_adapt.update_to_command(
                {
                    "update_id": 614,
                    "message": {
                        "text": "Hi Andrea",
                        "message_id": 2112,
                        "chat": {"id": 9293, "type": "private"},
                        "from": {"id": 88, "username": "u3b"},
                    },
                }
            )
            assert cmd2
            self.assertFalse(attach_continuation_if_applicable(self.conn, cmd2))
            self.assertIsNone(cmd2.get("task_id"))
        finally:
            if prev is None:
                os.environ.pop("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS", None)
            else:
                os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = prev

    def test_telegram_continuation_good_morning_andrea_does_not_merge_queued_collab_task(
        self,
    ) -> None:
        from services.andrea_sync.telegram_continuation import attach_continuation_if_applicable

        prev = os.environ.get("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS")
        os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = "300"
        try:
            cmd1 = tg_adapt.update_to_command(
                {
                    "update_id": 615,
                    "message": {
                        "text": "@Andrea @Cursor please collaborate on repo cleanup",
                        "message_id": 2121,
                        "chat": {"id": 9294, "type": "private"},
                        "from": {"id": 88, "username": "u3c"},
                    },
                }
            )
            assert cmd1
            r1 = handle_command(self.conn, cmd1)
            tid1 = r1["task_id"]
            append_event(
                self.conn,
                tid1,
                EventType.JOB_QUEUED,
                {
                    "kind": "openclaw",
                    "execution_lane": "openclaw_hybrid",
                    "runner": "openclaw",
                },
            )

            cmd2 = tg_adapt.update_to_command(
                {
                    "update_id": 616,
                    "message": {
                        "text": "Good morning Andrea",
                        "message_id": 2122,
                        "chat": {"id": 9294, "type": "private"},
                        "from": {"id": 88, "username": "u3c"},
                    },
                }
            )
            assert cmd2
            self.assertFalse(attach_continuation_if_applicable(self.conn, cmd2))
            self.assertIsNone(cmd2.get("task_id"))
        finally:
            if prev is None:
                os.environ.pop("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS", None)
            else:
                os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = prev

    def test_telegram_continuation_question_does_not_merge_created_task(self) -> None:
        """Regression: 'Is this OpenClaw?' must not merge onto a CREATED technical task."""
        from services.andrea_sync.telegram_continuation import attach_continuation_if_applicable

        prev = os.environ.get("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS")
        os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = "300"
        try:
            cmd1 = tg_adapt.update_to_command(
                {
                    "update_id": 711,
                    "message": {
                        "text": "Please fix the repo and run the tests",
                        "message_id": 2201,
                        "chat": {"id": 9393, "type": "private"},
                        "from": {"id": 89, "username": "u4"},
                    },
                }
            )
            assert cmd1
            r1 = handle_command(self.conn, cmd1)
            tid1 = r1["task_id"]
            proj = project_task_dict(self.conn, tid1, "telegram")
            self.assertEqual(proj["status"], TaskStatus.CREATED.value)

            cmd2 = tg_adapt.update_to_command(
                {
                    "update_id": 712,
                    "message": {
                        "text": "Is this OpenClaw?",
                        "message_id": 2202,
                        "chat": {"id": 9393, "type": "private"},
                        "from": {"id": 89, "username": "u4"},
                    },
                }
            )
            assert cmd2
            self.assertFalse(attach_continuation_if_applicable(self.conn, cmd2))
            self.assertIsNone(cmd2.get("task_id"))

            r2 = handle_command(self.conn, cmd2)
            self.assertTrue(r2.get("ok"))
            self.assertNotEqual(r2["task_id"], tid1)
        finally:
            if prev is None:
                os.environ.pop("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS", None)
            else:
                os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = prev

    def test_telegram_continuation_ignores_completed_task(self) -> None:
        from services.andrea_sync.telegram_continuation import attach_continuation_if_applicable

        prev = os.environ.get("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS")
        os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = "300"
        try:
            cmd1 = tg_adapt.update_to_command(
                {
                    "update_id": 701,
                    "message": {
                        "text": "hello from telegram",
                        "message_id": 3001,
                        "chat": {"id": 1313, "type": "private"},
                        "from": {"id": 5, "username": "u3"},
                    },
                }
            )
            assert cmd1
            r1 = handle_command(self.conn, cmd1)
            tid = r1["task_id"]
            append_event(
                self.conn,
                tid,
                EventType.ASSISTANT_REPLIED,
                {"text": "done", "route": "direct", "reason": "test"},
            )

            cmd2 = tg_adapt.update_to_command(
                {
                    "update_id": 702,
                    "message": {
                        "text": "second chunk without mentions",
                        "message_id": 3002,
                        "chat": {"id": 1313, "type": "private"},
                        "from": {"id": 5, "username": "u3"},
                    },
                }
            )
            assert cmd2
            self.assertFalse(attach_continuation_if_applicable(self.conn, cmd2))
        finally:
            if prev is None:
                os.environ.pop("ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS", None)
            else:
                os.environ["ANDREA_TELEGRAM_CONTINUATION_WINDOW_SECONDS"] = prev

    def test_submit_user_message_existing_task_idempotent_by_external_id(self) -> None:
        first = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-anchor",
                "payload": {
                    "text": "hello",
                    "chat_id": 50001,
                    "message_id": 1,
                    "from_user": 100,
                },
            },
        )
        self.assertTrue(first.get("ok"))
        tid = first["task_id"]
        dup_body = {
            "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
            "channel": "telegram",
            "task_id": tid,
            "external_id": "tg-chunk-dup",
            "payload": {
                "text": "continuation",
                "chat_id": 50001,
                "message_id": 2,
                "from_user": 100,
            },
        }
        self.assertTrue(handle_command(self.conn, dup_body).get("ok"))
        r_dup = handle_command(self.conn, dup_body)
        self.assertTrue(r_dup.get("ok"))
        self.assertTrue(r_dup.get("deduped"))

    def test_submit_user_message_without_external_id_creates_distinct_tasks(self) -> None:
        first = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "payload": {
                    "text": "first orphan message",
                    "chat_id": 91001,
                    "message_id": 1,
                    "from_user": 1,
                },
            },
        )
        second = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "payload": {
                    "text": "second orphan message",
                    "chat_id": 91001,
                    "message_id": 2,
                    "from_user": 1,
                },
            },
        )
        self.assertTrue(first.get("ok"))
        self.assertTrue(second.get("ok"))
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertFalse(first.get("deduped"))
        self.assertFalse(second.get("deduped"))

    def test_report_cursor_event_idempotent_same_payload(self) -> None:
        created = handle_command(
            self.conn,
            {
                "command_type": CommandType.CREATE_CURSOR_JOB.value,
                "channel": "telegram",
                "external_id": "idem-cursor",
                "payload": {"prompt": "fix bug", "summary": "fix"},
            },
        )
        self.assertTrue(created.get("ok"))
        tid = created["task_id"]
        body = {
            "command_type": CommandType.REPORT_CURSOR_EVENT.value,
            "channel": "cursor",
            "task_id": tid,
            "payload": {
                "event_type": EventType.JOB_PROGRESS.value,
                "payload": {"message": "step", "n": 1},
            },
        }
        self.assertTrue(handle_command(self.conn, body).get("ok"))
        r2 = handle_command(self.conn, body)
        self.assertTrue(r2.get("ok"))
        self.assertTrue(r2.get("deduped"))

    def test_external_ref_kind_reflects_channel_on_existing_task(self) -> None:
        created = handle_command(
            self.conn,
            {
                "command_type": CommandType.CREATE_TASK.value,
                "channel": "cursor",
                "external_id": "ext-cursor-task",
                "payload": {"summary": "cli cursor task"},
            },
        )
        self.assertTrue(created.get("ok"))
        tid = created["task_id"]
        handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "cursor",
                "task_id": tid,
                "external_id": "ext-cursor-msg",
                "payload": {"text": "go", "routing_text": "go"},
            },
        )
        events = load_events_for_task(self.conn, tid)
        kinds = [
            p.get("kind")
            for _s, _t, et, p in events
            if et == EventType.EXTERNAL_REF.value and isinstance(p, dict)
        ]
        self.assertIn("cursor_update", kinds)

    def test_alexa_verify_skipped_when_disabled(self) -> None:
        from services.andrea_sync import alexa_request_verify as arv

        prev = os.environ.get("ANDREA_ALEXA_VERIFY_SIGNATURES")
        os.environ.pop("ANDREA_ALEXA_VERIFY_SIGNATURES", None)
        try:
            self.assertFalse(arv.alexa_signature_verification_enabled())
            arv.verify_alexa_http_request(
                b'{"session":{"application":{"applicationId":"any"}},"request":{"timestamp":"2020-01-01T00:00:00Z"}}',
                {},
                expected_application_id="",
            )
        finally:
            if prev is None:
                os.environ.pop("ANDREA_ALEXA_VERIFY_SIGNATURES", None)
            else:
                os.environ["ANDREA_ALEXA_VERIFY_SIGNATURES"] = prev

    def test_parse_alexa_body_tolerates_non_object_intent_shapes(self) -> None:
        cmd, response = alexa_adapt.parse_alexa_body(
            {
                "request": {"type": "IntentRequest", "intent": "bad-shape"},
                "session": "bad-shape",
                "context": {"System": "bad-shape"},
            }
        )
        self.assertIsNone(cmd)
        self.assertIn("did not catch that", response["response"]["outputSpeech"]["text"].lower())

    def test_list_telegram_task_ids_for_chat_prefers_recent_tasks(self) -> None:
        a = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "chat-order-a",
                "payload": {
                    "text": "a",
                    "chat_id": 60001,
                    "message_id": 1,
                    "from_user": 1,
                },
            },
        )
        b = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "chat-order-b",
                "payload": {
                    "text": "b",
                    "chat_id": 60001,
                    "message_id": 2,
                    "from_user": 1,
                },
            },
        )
        self.assertTrue(a.get("ok") and b.get("ok"))
        ids = list_telegram_task_ids_for_chat(self.conn, 60001, limit=10)
        self.assertGreaterEqual(len(ids), 2)
        self.assertEqual(ids[0], b["task_id"])

    def test_router_greeting_stays_direct(self) -> None:
        decision = route_message("hi andrea how are you?")
        self.assertEqual(decision.mode, "direct")
        low = decision.reply_text.lower()
        self.assertTrue(
            "thanks" in low or "good" in low or "well" in low,
            msg=decision.reply_text,
        )
        self.assertNotIn("cursor", low)

    def test_router_casual_checkin_is_natural_without_cursor(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        decision = route_message("How's it going?")
        self.assertEqual(decision.mode, "direct")
        low = decision.reply_text.lower()
        self.assertNotIn("cursor", low)
        self.assertNotIn("bring in cursor", low)
        self.assertNotIn("i can help with that directly", low)

    def test_router_casual_checkin_smart_apostrophe_is_natural_without_cursor(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        decision = route_message("How’s it going?")
        self.assertEqual(decision.mode, "direct")
        low = decision.reply_text.lower()
        self.assertNotIn("say a bit more about what you want", low)
        self.assertNotIn("i can help with that directly", low)

    def test_classify_route_casual_checkin_is_greeting_or_social(self) -> None:
        mode, reason, target, collab = classify_route("How's it going?")
        self.assertEqual(mode, "direct")
        self.assertEqual(reason, "greeting_or_social")
        self.assertEqual(target, "")
        self.assertNotEqual(collab, "cursor_primary")

    def test_classify_route_answer_before_delegate_blocks_repo_keyword_escalation(self) -> None:
        mode, reason, target, collab = classify_route(
            "What are we working on with Andrea regarding the repository and failing tests?",
        )
        self.assertEqual(mode, "direct")
        self.assertEqual(reason, "answer_before_delegate")
        self.assertEqual(target, "")

    def test_classify_route_heavy_implementation_still_delegates(self) -> None:
        mode, reason, target, collab = classify_route(
            "What are we working on? Please implement the auth fix in the repo.",
        )
        self.assertEqual(mode, "delegate")
        self.assertIn("technical_or_repo_request", reason)

    def test_classify_route_blocked_now_prefers_direct(self) -> None:
        mode, reason, target, collab = classify_route("What's blocked right now?")
        self.assertEqual(mode, "direct")
        self.assertEqual(reason, "answer_before_delegate")

    def test_is_standalone_casual_social_turn_covers_planned_phrases(self) -> None:
        self.assertTrue(is_standalone_casual_social_turn("Hi Andrea"))
        self.assertTrue(is_standalone_casual_social_turn("Good morning Andrea"))
        self.assertTrue(is_standalone_casual_social_turn("Hey Andrea good morning"))
        self.assertTrue(is_standalone_casual_social_turn("How's it going?"))
        self.assertFalse(is_standalone_casual_social_turn("Please fix the repo and run the tests"))

    def test_router_agenda_today_soft_limit_without_cursor(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        decision = route_message("What on the agenda today?")
        self.assertEqual(decision.mode, "direct")
        low = decision.reply_text.lower()
        self.assertNotIn("cursor", low)
        self.assertNotIn("bring in cursor", low)
        self.assertIn("calendar", low)

    def test_router_opinion_about_that_asks_clarifier_without_cursor(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        decision = route_message("What do you think about that?")
        self.assertEqual(decision.mode, "direct")
        low = decision.reply_text.lower()
        self.assertNotIn("cursor", low)
        self.assertTrue("mean" in low or "part" in low or "discuss" in low, msg=decision.reply_text)

    def test_router_opinion_with_history_weighs_in_without_cursor(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        decision = route_message(
            "What do you think about that?",
            history=[
                {"role": "user", "content": "We're debating the new rollout timeline."},
                {"role": "assistant", "content": "A shorter window could reduce risk if QA stays tight."},
            ],
        )
        self.assertEqual(decision.mode, "direct")
        low = decision.reply_text.lower()
        self.assertNotIn("cursor", low)
        self.assertTrue(
            "shorter" in low or "window" in low or "seriously" in low or "rollout" in low,
            msg=decision.reply_text,
        )

    def test_router_news_today_openai_off_still_mentions_news_not_cursor(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        decision = route_message("What's in the news today?")
        self.assertEqual(decision.mode, "direct")
        low = decision.reply_text.lower()
        self.assertIn("news", low)
        self.assertNotIn("cursor", low)
        self.assertNotIn("bring in cursor", low)

    def test_router_greeting_plus_news_request_stays_on_request(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        decision = route_message("Hi Andrea what's the news today?")
        self.assertEqual(decision.mode, "direct")
        self.assertIn("news", decision.reply_text.lower())
        self.assertNotIn("what would you like to do", decision.reply_text.lower())

    def test_router_coding_request_delegates(self) -> None:
        decision = route_message("Please inspect the repo, fix the failing tests, and open a PR.")
        self.assertEqual(decision.mode, "delegate")
        self.assertEqual(decision.delegate_target, "openclaw_hybrid")

    def test_router_help_with_technical_request_delegates(self) -> None:
        decision = route_message("Help me debug this traceback and fix the failing tests.")
        self.assertEqual(decision.mode, "delegate")
        self.assertEqual(decision.delegate_target, "openclaw_hybrid")

    def test_router_productivity_request_targets_openclaw_hybrid(self) -> None:
        decision = route_message("Remind me to follow up with the StoryLiner team tomorrow.")
        self.assertEqual(decision.mode, "delegate")
        self.assertEqual(decision.delegate_target, "openclaw_hybrid")

    def test_router_andrea_mention_forces_direct(self) -> None:
        decision = route_message(
            "how are you?",
            routing_hint="andrea",
            collaboration_mode="andrea_primary",
        )
        self.assertEqual(decision.mode, "direct")
        self.assertEqual(decision.reason, "explicit_andrea_mention")

    def test_router_andrea_mention_can_still_delegate_actionable_work(self) -> None:
        decision = route_message(
            "remind me to review StoryLiner tomorrow",
            routing_hint="andrea",
            collaboration_mode="andrea_primary",
        )
        self.assertEqual(decision.mode, "delegate")
        self.assertEqual(decision.delegate_target, "openclaw_hybrid")

    def test_router_cursor_mention_forces_hybrid_delegate(self) -> None:
        decision = route_message(
            "how are you?",
            routing_hint="cursor",
            collaboration_mode="cursor_primary",
        )
        self.assertEqual(decision.mode, "delegate")
        self.assertEqual(decision.delegate_target, "openclaw_hybrid")
        self.assertEqual(decision.collaboration_mode, "cursor_primary")

    def test_router_model_mention_meta_question_stays_direct(self) -> None:
        decision = route_message(
            "What is Cursor?",
            preferred_model_family="gemini",
        )
        self.assertEqual(decision.mode, "direct")
        self.assertIn("cursor", decision.reply_text.lower())

    def test_router_model_mention_with_repo_work_delegates(self) -> None:
        decision = route_message(
            "Please review the repo approach",
            preferred_model_family="gemini",
        )
        self.assertEqual(decision.mode, "delegate")
        self.assertEqual(decision.delegate_target, "openclaw_hybrid")

    def test_router_collaboration_phrase_requests_joint_work(self) -> None:
        decision = route_message("Please work together and double-check the repo changes.")
        self.assertEqual(decision.mode, "delegate")
        self.assertEqual(decision.collaboration_mode, "collaborative")

    def test_router_meta_cursor_question_stays_direct(self) -> None:
        decision = route_message("Can you talk to Cursor when needed?")
        self.assertEqual(decision.mode, "direct")
        self.assertIn("@cursor", decision.reply_text.lower())
        self.assertNotIn("sessionkey", decision.reply_text.lower())

    def test_router_is_this_openclaw_stays_direct(self) -> None:
        decision = route_message("Is this OpenClaw?")
        self.assertEqual(decision.mode, "direct")
        self.assertEqual(decision.reason, "stack_or_tooling_question")
        self.assertIn("andrea", decision.reply_text.lower())
        self.assertIn("openclaw", decision.reply_text.lower())
        self.assertIn("collaboration layer", decision.reply_text.lower())

    def test_router_what_is_cursor_stays_direct(self) -> None:
        decision = route_message("What is Cursor?")
        self.assertEqual(decision.mode, "direct")
        self.assertIn("cursor", decision.reply_text.lower())
        self.assertIn("execution lane", decision.reply_text.lower())
        self.assertIn("andrea", decision.reply_text.lower())

    def test_router_have_cursor_fix_delegates(self) -> None:
        decision = route_message("Have Cursor fix the failing tests in the repo.")
        self.assertEqual(decision.mode, "delegate")
        self.assertEqual(decision.delegate_target, "openclaw_hybrid")
        self.assertEqual(decision.collaboration_mode, "cursor_primary")

    def test_router_thanks_with_repo_work_still_delegates(self) -> None:
        decision = route_message("Thanks, please inspect the repo and fix the failing tests.")
        self.assertEqual(decision.mode, "delegate")
        self.assertEqual(decision.delegate_target, "openclaw_hybrid")

    def test_router_what_llm_is_answering_stays_direct(self) -> None:
        decision = route_message("What LLM is answering?")
        self.assertEqual(decision.mode, "direct")
        self.assertEqual(decision.reason, "stack_or_tooling_question")
        self.assertIn("andrea", decision.reply_text.lower())
        self.assertIn("directly", decision.reply_text.lower())
        self.assertNotIn("execution lane", decision.reply_text.lower())

    def test_router_meta_stack_questions_use_distinct_direct_replies(self) -> None:
        openclaw = route_message("Is this OpenClaw?")
        cursor = route_message("What is Cursor?")
        llm = route_message("What LLM is answering?")
        self.assertNotEqual(openclaw.reply_text, cursor.reply_text)
        self.assertNotEqual(cursor.reply_text, llm.reply_text)
        self.assertNotEqual(openclaw.reply_text, llm.reply_text)

    def test_router_news_question_does_not_reuse_recent_context_hint(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        decision = route_message(
            "What's the news today?",
            history=[
                {
                    "role": "assistant",
                    "content": "You're talking with Andrea. OpenClaw is a collaboration layer I can use when deeper reasoning helps.",
                }
            ],
        )
        self.assertEqual(decision.mode, "direct")
        self.assertIn("news", decision.reply_text.lower())
        self.assertNotIn("latest useful thread", decision.reply_text.lower())
        self.assertNotIn("recent context from this chat", decision.reply_text.lower())
        self.assertNotIn("latest useful context", decision.reply_text.lower())
        self.assertNotIn("recent thread:", decision.reply_text.lower())

    def test_openai_direct_path_passes_inject_durable_memory_flag(self) -> None:
        with mock.patch(
            "services.andrea_sync.andrea_router._openai_direct_reply",
            return_value="Brief ok reply without fluff.",
        ) as m:
            build_direct_reply(
                "What's the news today?",
                history=[],
                memory_notes=["principal note about sprint"],
                turn_domain="external_information",
                context_boundary="external_world_only",
                inject_durable_memory=False,
            )
        kw = m.call_args.kwargs
        self.assertFalse(kw.get("inject_durable_memory", True))

    def test_build_direct_reply_replaces_model_generic_for_ranked_domains(self) -> None:
        weak = "I'm here. Say a bit more about what you want and I'll take it from there."
        with mock.patch(
            "services.andrea_sync.andrea_router._openai_direct_reply",
            return_value=weak,
        ):
            agenda_r = build_direct_reply(
                "What's on the agenda today?",
                history=[],
                memory_notes=[],
                turn_domain="personal_agenda",
                context_boundary="personal_agenda_state",
                inject_durable_memory=False,
            )
            self.assertFalse(is_generic_direct_reply(agenda_r))
            self.assertIn("calendar", agenda_r.lower())

            news_r = build_direct_reply(
                "What's the news today?",
                history=[],
                memory_notes=[],
                turn_domain="external_information",
                context_boundary="external_world_only",
                inject_durable_memory=False,
            )
            self.assertFalse(is_generic_direct_reply(news_r))
            self.assertIn("news", news_r.lower())

            opinion_r = build_direct_reply(
                "What do you think about that?",
                history=[],
                memory_notes=[],
                turn_domain="opinion_reflection",
                context_boundary="recent_thread_only",
                inject_durable_memory=False,
            )
            self.assertFalse(is_generic_direct_reply(opinion_r))

    def test_cross_domain_weak_generic_is_not_returned_where_guarded(self) -> None:
        weak = "Tell me what you need."
        domains_guarded = (
            "personal_agenda",
            "attention_today",
            "external_information",
            "opinion_reflection",
        )
        with mock.patch(
            "services.andrea_sync.andrea_router._openai_direct_reply",
            return_value=weak,
        ):
            for dom in domains_guarded:
                body = (
                    "What's on the agenda today?"
                    if dom == "personal_agenda"
                    else (
                        "What do I need to pay attention to today?"
                        if dom == "attention_today"
                        else (
                            "What's the news today?"
                            if dom == "external_information"
                            else "What do you think about that?"
                        )
                    )
                )
                r = build_direct_reply(
                    body,
                    history=[],
                    memory_notes=[],
                    turn_domain=dom,
                    context_boundary="test",
                    inject_durable_memory=False,
                )
                self.assertFalse(is_generic_direct_reply(r), msg=f"{dom}: {r!r}")

    def test_server_repair_swaps_generic_agenda_to_no_calendar_copy(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.andrea_router import AndreaRouteDecision  # noqa: E402
        from services.andrea_sync.scenario_registry import SCENARIO_CATALOG  # noqa: E402
        from services.andrea_sync.scenario_schema import ScenarioResolution  # noqa: E402
        from services.andrea_sync.server import SyncServer  # noqa: E402
        from services.andrea_sync.turn_intelligence import build_turn_plan  # noqa: E402

        server = SyncServer()
        sc = SCENARIO_CATALOG["statusFollowupContinue"]
        text = "What's on the agenda today?"
        plan = build_turn_plan(
            text, scenario_id=sc.scenario_id, projection_has_continuity_state=True
        )
        res = ScenarioResolution(
            scenario_id=sc.scenario_id,
            confidence=0.9,
            support_level=sc.support_level,
            reason="test",
            goal_id="",
            needs_plan=False,
            suggested_lane="direct_assistant",
            action_class=sc.action_class,
            proof_class=sc.proof_class,
            approval_mode=sc.approval_mode,
        )
        bad = AndreaRouteDecision(
            mode="direct",
            reason="short_general_request",
            reply_text=(
                "I'm here. Say a bit more about what you want and I'll take it from there."
            ),
        )
        out = server._maybe_repair_direct_reply_from_continuity(
            "nonexistent_task",
            classify_text=text,
            decision=bad,
            resolution=res,
            turn_plan=plan,
            history=[],
            memory_notes=[],
        )
        self.assertIn("calendar", out.reply_text.lower())
        self.assertEqual(out.reason, "domain_agenda_repaired_direct_reply")

    def test_server_repair_swaps_generic_attention_to_state_copy(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.andrea_router import AndreaRouteDecision  # noqa: E402
        from services.andrea_sync.scenario_registry import SCENARIO_CATALOG  # noqa: E402
        from services.andrea_sync.scenario_schema import ScenarioResolution  # noqa: E402
        from services.andrea_sync.server import SyncServer  # noqa: E402
        from services.andrea_sync.turn_intelligence import build_turn_plan  # noqa: E402

        server = SyncServer()
        sc = SCENARIO_CATALOG["statusFollowupContinue"]
        text = "What do I need to pay attention to today?"
        plan = build_turn_plan(
            text, scenario_id=sc.scenario_id, projection_has_continuity_state=True
        )
        res = ScenarioResolution(
            scenario_id=sc.scenario_id,
            confidence=0.9,
            support_level=sc.support_level,
            reason="test",
            goal_id="",
            needs_plan=False,
            suggested_lane="direct_assistant",
            action_class=sc.action_class,
            proof_class=sc.proof_class,
            approval_mode=sc.approval_mode,
        )
        bad = AndreaRouteDecision(
            mode="direct",
            reason="short_general_request",
            reply_text=(
                "I'm here. Say a bit more about what you want and I'll take it from there."
            ),
        )
        out = server._maybe_repair_direct_reply_from_continuity(
            "nonexistent_task",
            classify_text=text,
            decision=bad,
            resolution=res,
            turn_plan=plan,
            history=[],
            memory_notes=[],
        )
        low = out.reply_text.lower()
        self.assertTrue(
            "nothing urgent" in low or "reminders" in low or "follow-through" in low,
            msg=out.reply_text,
        )
        self.assertEqual(out.reason, "domain_attention_repaired_direct_reply")

    def test_composer_repairs_false_completion_when_followthrough_pending(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.andrea_router import AndreaRouteDecision  # noqa: E402
        from services.andrea_sync.scenario_registry import SCENARIO_CATALOG  # noqa: E402
        from services.andrea_sync.scenario_schema import ScenarioResolution  # noqa: E402
        from services.andrea_sync.server import SyncServer  # noqa: E402
        from services.andrea_sync.turn_intelligence import build_turn_plan  # noqa: E402

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "ft-false-done-1",
                "payload": {
                    "text": "hello",
                    "routing_text": "hello",
                    "chat_id": 66100,
                    "message_id": 1,
                    "from_user": 600,
                },
            },
        )
        tid = r0["task_id"]
        link_task_principal(self.conn, tid, "pri_ft_false", channel="telegram")
        gid = create_goal(self.conn, "pri_ft_false", "Rollout beta", channel="telegram")
        link_task_to_goal(self.conn, tid, gid)
        append_event(
            self.conn,
            tid,
            EventType.CLOSURE_DECISION_RECORDED,
            {
                "closure_state": "awaiting_user",
                "reason": "Waiting on your go/no-go for the rollout.",
                "decision_id": "d-ft-1",
            },
        )
        server = SyncServer()
        sc = SCENARIO_CATALOG["statusFollowupContinue"]
        text = "Where are we with the rollout?"
        plan = build_turn_plan(
            text, scenario_id=sc.scenario_id, projection_has_continuity_state=True
        )
        res = ScenarioResolution(
            scenario_id=sc.scenario_id,
            confidence=0.9,
            support_level=sc.support_level,
            reason="test",
            goal_id=gid,
            needs_plan=False,
            suggested_lane="direct_assistant",
            action_class=sc.action_class,
            proof_class=sc.proof_class,
            approval_mode=sc.approval_mode,
        )
        bad = AndreaRouteDecision(
            mode="direct",
            reason="balanced_default_direct",
            reply_text="You are all caught up — nothing pending on my side.",
        )
        out = server._maybe_repair_direct_reply_from_continuity(
            tid,
            classify_text=text,
            decision=bad,
            resolution=res,
            turn_plan=plan,
            history=[],
            memory_notes=[],
        )
        low = out.reply_text.lower()
        self.assertIn("follow-through", low)
        self.assertIn("rollout", low)
        self.assertEqual(out.reason, "continuity_state_repaired_direct_reply")

    def test_ranking_working_on_prefers_linked_goal(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "rank-goal-1",
                "payload": {
                    "text": "hello",
                    "routing_text": "hello",
                    "chat_id": 66001,
                    "message_id": 10,
                    "from_user": 500,
                },
            },
        )
        tid = r0["task_id"]
        link_task_principal(self.conn, tid, "pri_rank_goal", channel="telegram")
        gid = create_goal(
            self.conn, "pri_rank_goal", "Ship the ranking engine", channel="telegram"
        )
        link_task_to_goal(self.conn, tid, gid)
        handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "task_id": tid,
                "external_id": "rank-goal-2",
                "payload": {
                    "text": "What are we working on right now?",
                    "routing_text": "What are we working on right now?",
                    "chat_id": 66001,
                    "message_id": 11,
                    "from_user": 500,
                },
            },
        )
        server = SyncServer()
        decision, _applied = server._route_task_with_decision(
            tid, history=[], source="test_rank_goal"
        )
        self.assertEqual(decision.mode, "direct")
        low = decision.reply_text.lower()
        self.assertIn("ship the ranking engine", low)
        self.assertIn("goal", low)

    def test_ranking_approval_question_reports_none_pending_when_empty(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "rank-apr-1",
                "payload": {
                    "text": "hello",
                    "routing_text": "hello",
                    "chat_id": 66004,
                    "message_id": 40,
                    "from_user": 503,
                },
            },
        )
        tid = r0["task_id"]
        link_task_principal(self.conn, tid, "pri_rank_apr", channel="telegram")
        gid = create_goal(self.conn, "pri_rank_apr", "Side project", channel="telegram")
        link_task_to_goal(self.conn, tid, gid)
        handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "task_id": tid,
                "external_id": "rank-apr-2",
                "payload": {
                    "text": "What still needs my approval?",
                    "routing_text": "What still needs my approval?",
                    "chat_id": 66004,
                    "message_id": 41,
                    "from_user": 503,
                },
            },
        )
        server = SyncServer()
        decision, _applied = server._route_task_with_decision(
            tid, history=[], source="test_rank_apr"
        )
        self.assertEqual(decision.mode, "direct")
        low = decision.reply_text.lower()
        self.assertIn("approval requests waiting", low)

    def test_ranking_agenda_question_uses_calendar_visibility_not_generic(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "rank-age-1",
                "payload": {
                    "text": "hello",
                    "routing_text": "hello",
                    "chat_id": 66005,
                    "message_id": 50,
                    "from_user": 504,
                },
            },
        )
        tid = r0["task_id"]
        link_task_principal(self.conn, tid, "pri_rank_age", channel="telegram")
        gid = create_goal(self.conn, "pri_rank_age", "Big delivery", channel="telegram")
        link_task_to_goal(self.conn, tid, gid)
        handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "task_id": tid,
                "external_id": "rank-age-2",
                "payload": {
                    "text": "What's on the agenda today?",
                    "routing_text": "What's on the agenda today?",
                    "chat_id": 66005,
                    "message_id": 51,
                    "from_user": 504,
                },
            },
        )
        server = SyncServer()
        decision, _applied = server._route_task_with_decision(
            tid, history=[], source="test_rank_age"
        )
        self.assertEqual(decision.mode, "direct")
        low = decision.reply_text.lower()
        self.assertIn("calendar", low)
        self.assertNotIn("say a bit more about what you want", low)

    def test_ranking_news_question_stays_off_goal_copy_with_active_goal(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "rank-news-1",
                "payload": {
                    "text": "seed",
                    "routing_text": "seed",
                    "chat_id": 66002,
                    "message_id": 20,
                    "from_user": 501,
                },
            },
        )
        tid = r0["task_id"]
        link_task_principal(self.conn, tid, "pri_rank_news", channel="telegram")
        gid = create_goal(
            self.conn,
            "pri_rank_news",
            "Secret goal title for contamination test",
            channel="telegram",
        )
        link_task_to_goal(self.conn, tid, gid)
        handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "task_id": tid,
                "external_id": "rank-news-2",
                "payload": {
                    "text": "What's the news today?",
                    "routing_text": "What's the news today?",
                    "chat_id": 66002,
                    "message_id": 21,
                    "from_user": 501,
                },
            },
        )
        server = SyncServer()
        decision, _applied = server._route_task_with_decision(
            tid, history=[], source="test_rank_news"
        )
        self.assertEqual(decision.mode, "direct")
        self.assertNotIn("secret goal title", decision.reply_text.lower())

    def test_ranking_opinion_uses_thread_not_goal_state(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        r0 = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "rank-op-1",
                "payload": {
                    "text": "seed",
                    "routing_text": "seed",
                    "chat_id": 66003,
                    "message_id": 30,
                    "from_user": 502,
                },
            },
        )
        tid = r0["task_id"]
        link_task_principal(self.conn, tid, "pri_rank_op", channel="telegram")
        gid = create_goal(self.conn, "pri_rank_op", "Milestone gamma", channel="telegram")
        link_task_to_goal(self.conn, tid, gid)
        hist = [
            {"role": "user", "content": "We're debating the rollout timeline for April."},
            {
                "role": "assistant",
                "content": "April could work if QA keeps two weeks buffer.",
            },
        ]
        handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "task_id": tid,
                "external_id": "rank-op-2",
                "payload": {
                    "text": "What do you think about that?",
                    "routing_text": "What do you think about that?",
                    "chat_id": 66003,
                    "message_id": 31,
                    "from_user": 502,
                },
            },
        )
        server = SyncServer()
        decision, _applied = server._route_task_with_decision(
            tid, history=hist, source="test_rank_op"
        )
        self.assertEqual(decision.mode, "direct")
        low = decision.reply_text.lower()
        self.assertTrue(
            "april" in low or "rollout" in low or "qa" in low or "buffer" in low,
            msg=decision.reply_text,
        )
        self.assertNotIn("milestone gamma", low)

    def test_router_openclaw_presence_question_stays_specific_with_history(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        decision = route_message(
            "OpenClaw are you there?",
            history=[
                {
                    "role": "assistant",
                    "content": "I can help with current news. Tell me the topic or place you want, and I'll focus the update there.",
                }
            ],
        )
        self.assertEqual(decision.mode, "direct")
        self.assertIn("andrea", decision.reply_text.lower())
        self.assertIn("openclaw", decision.reply_text.lower())
        self.assertNotIn("latest useful thread", decision.reply_text.lower())
        self.assertNotIn("recent context from this chat", decision.reply_text.lower())
        self.assertNotIn("latest useful context", decision.reply_text.lower())
        self.assertNotIn("recent thread:", decision.reply_text.lower())

    def test_server_routes_latest_message_not_accumulated_thread(self) -> None:
        """Regression: router must classify the latest user turn, not merged accumulated_prompt."""
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        first = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-thread-openclaw-meta",
                "payload": {
                    "text": "Please inspect the repo, fix the failing tests, and open a PR.",
                    "routing_text": "Please inspect the repo, fix the failing tests, and open a PR.",
                    "chat_id": 88001,
                    "message_id": 101,
                },
            },
        )
        tid = first["task_id"]
        handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "task_id": tid,
                "external_id": "tg-thread-openclaw-meta-2",
                "payload": {
                    "text": "Is this OpenClaw?",
                    "routing_text": "Is this OpenClaw?",
                    "chat_id": 88001,
                    "message_id": 102,
                },
            },
        )
        acc = server._extract_cursor_prompt(tid)
        self.assertIn("OpenClaw", acc)
        self.assertIn("failing tests", acc)
        self.assertEqual(server._routing_classification_text(tid), "Is this OpenClaw?")
        decision, _applied = server._route_task_with_decision(
            tid,
            history=[],
            source="test_latest_vs_accumulated",
        )
        self.assertEqual(decision.mode, "direct")
        self.assertEqual(decision.reason, "stack_or_tooling_question")

    def test_router_bare_openclaw_mention_stays_direct_when_short(self) -> None:
        decision = route_message("Just checking — openclaw?")
        self.assertEqual(decision.mode, "direct")

    def test_router_memory_question_uses_history_fallback(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        decision = route_message(
            "Hi do you remember before?",
            history=[
                {"role": "user", "content": "We were talking about reboot startup."},
                {"role": "assistant", "content": "We were working on reboot startup and Telegram memory."},
            ],
        )
        self.assertEqual(decision.mode, "direct")
        self.assertIn("remember the recent conversation", decision.reply_text.lower())
        self.assertIn("reboot startup", decision.reply_text.lower())

    def test_router_followup_prompt_uses_recent_context(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        decision = route_message(
            "Can you say anything else?",
            history=[
                {"role": "assistant", "content": "We were working on reboot startup and Telegram memory."},
            ],
        )
        self.assertEqual(decision.mode, "direct")
        self.assertIn("reboot startup", decision.reply_text.lower())

    def test_router_memory_prefers_substantive_history_over_generic_reply(self) -> None:
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        decision = route_message(
            "Hi do you remember before?",
            history=[
                {"role": "user", "content": "Let's finish the reboot startup work."},
                {
                    "role": "assistant",
                    "content": "I can help with that directly when it's lightweight, and I'll bring in Cursor when the task needs deeper technical work. Tell me what you need.",
                },
            ],
        )
        self.assertIn("reboot startup", decision.reply_text.lower())

    def test_load_recent_telegram_history_returns_prior_turns(self) -> None:
        first = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-history-1",
                "payload": {
                    "text": "Let's finish the reboot startup work.",
                    "chat_id": 55,
                    "message_id": 10,
                },
            },
        )
        append_event(
            self.conn,
            first["task_id"],
            EventType.ASSISTANT_REPLIED,
            {
                "text": "We were working on reboot startup and Telegram memory.",
                "route": "direct",
                "reason": "history",
            },
        )
        history = load_recent_telegram_history(self.conn, 55)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["role"], "user")
        self.assertIn("reboot startup", history[0]["content"].lower())
        self.assertEqual(history[1]["role"], "assistant")
        self.assertIn("telegram memory", history[1]["content"].lower())

    def test_load_recent_telegram_history_prefers_routing_text(self) -> None:
        first = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-history-mention",
                "payload": {
                    "text": "@Cursor please fix the failing tests",
                    "routing_text": "please fix the failing tests",
                    "routing_hint": "cursor",
                    "collaboration_mode": "cursor_primary",
                    "mention_targets": ["cursor"],
                    "chat_id": 56,
                    "message_id": 11,
                },
            },
        )
        append_event(
            self.conn,
            first["task_id"],
            EventType.JOB_COMPLETED,
            {
                "summary": "OpenClaw and Cursor completed the repo fix.",
                "backend": "openclaw",
                "delegated_to_cursor": True,
            },
        )
        history = load_recent_telegram_history(self.conn, 56)
        self.assertEqual(history[0]["content"], "please fix the failing tests")
        self.assertIn("openclaw and cursor completed", history[1]["content"].lower())

    def test_direct_message_format_is_short(self) -> None:
        text = format_direct_message("Hi! I'm doing well and ready to help.")
        self.assertEqual(text, "Andrea:\nHi! I'm doing well and ready to help.")

    def test_telegram_ack_message_format(self) -> None:
        text = format_ack_message("tsk_demo")
        self.assertIn("Andrea:", text)
        self.assertIn("What happens next:", text)
        self.assertIn("Technical details:", text)
        self.assertIn("Task: tsk_demo", text)
        self.assertIn("Status: queued", text)

    def test_telegram_ack_message_format_for_openclaw(self) -> None:
        text = format_ack_message("tsk_demo", worker_label="OpenClaw")
        self.assertIn("OpenClaw", text)
        low = text.lower()
        self.assertIn("coordinates first", low)
        self.assertIn("delegates to cursor", low)
        self.assertIn("threaded under your message", low)

    def test_telegram_ack_message_mentions_cursor_request(self) -> None:
        text = format_ack_message(
            "tsk_demo",
            worker_label="OpenClaw",
            routing_hint="cursor",
            collaboration_mode="cursor_primary",
        )
        self.assertIn("Cursor execution lane", text)

    def test_telegram_ack_message_mentions_preferred_model_lane(self) -> None:
        text = format_ack_message(
            "tsk_demo",
            worker_label="OpenClaw",
            preferred_model_label="Gemini",
        )
        self.assertIn("Preferred OpenClaw lane: Gemini", text)

    def test_telegram_ack_message_mentions_manual_cursor_start_when_disabled(self) -> None:
        text = format_ack_message("tsk_demo", worker_label="Cursor", auto_start=False)
        self.assertIn("auto-start is currently disabled", text)

    def test_telegram_running_message_format(self) -> None:
        text = format_running_message("tsk_demo", agent_url="https://cursor.com/agents/demo")
        self.assertIn("Andrea:", text)
        self.assertIn("Cursor is actively working", text)
        self.assertIn("Technical details:", text)
        self.assertIn("Agent: https://cursor.com/agents/demo", text)

    def test_telegram_running_message_format_for_openclaw(self) -> None:
        text = format_running_message("tsk_demo", worker_label="OpenClaw")
        self.assertIn("OpenClaw is actively working", text)

    def test_telegram_running_message_for_cursor_uses_neutral_model_context(self) -> None:
        text = format_running_message(
            "tsk_demo",
            worker_label="Cursor",
            provider="openai",
            model="gpt-5",
        )
        self.assertIn("Active model context: openai / gpt-5.", text)
        self.assertNotIn("OpenClaw is currently coordinating", text)

    def test_telegram_running_message_format_for_collaboration(self) -> None:
        text = format_running_message(
            "tsk_demo",
            worker_label="OpenClaw",
            delegated_to_cursor=True,
            collaboration_mode="collaborative",
        )
        self.assertIn("OpenClaw and Cursor are actively working", text)

    def test_telegram_progress_message_format(self) -> None:
        text = format_progress_message(
            "tsk_demo",
            progress_text="OpenClaw completed the triage pass and Cursor is taking the repo-heavy execution.",
            worker_label="OpenClaw and Cursor",
            collaboration_mode="collaborative",
            provider="google",
            model="gemini-2.5-flash",
        )
        self.assertIn("coordination update", text.lower())
        self.assertIn("gemini-2.5-flash", text)
        self.assertIn("work together", text.lower())

    def test_telegram_progress_message_can_show_preferred_lane_without_live_model(self) -> None:
        text = format_progress_message(
            "tsk_demo",
            progress_text="OpenClaw is starting with the requested reasoning lane.",
            worker_label="OpenClaw",
            preferred_model_label="MiniMax",
        )
        self.assertIn("Preferred OpenClaw lane: MiniMax", text)

    def test_telegram_progress_message_uses_neutral_model_label_for_cursor(self) -> None:
        text = format_progress_message(
            "tsk_demo",
            progress_text="Cursor is running the fix.",
            worker_label="Cursor",
            provider="openai",
            model="gpt-5",
        )
        self.assertIn("Active model: openai / gpt-5", text)
        self.assertNotIn("Active OpenClaw model", text)

    def test_telegram_final_message_separates_andrea_from_cursor(self) -> None:
        text = format_final_message(
            "tsk_demo",
            status="completed",
            summary=(
                "Implemented a new talk command.\n\n"
                "### What changed\n"
                "- Added CLI support\n"
                "- Updated docs"
            ),
            pr_url="https://github.com/example/repo/pull/1",
            agent_url="https://cursor.com/agents/demo",
        )
        self.assertIn("Andrea:", text)
        self.assertIn("What happened:", text)
        self.assertNotIn("Cursor said:", text)
        self.assertIn("Technical details:", text)
        self.assertIn("PR: https://github.com/example/repo/pull/1", text)
        self.assertNotIn("### What changed", text)

    def test_telegram_final_message_for_openclaw_only(self) -> None:
        text = format_final_message(
            "tsk_demo",
            status="completed",
            summary="Created a reminder and scheduled the follow-up.",
            worker_label="OpenClaw",
            openclaw_session_id="sess_demo",
        )
        self.assertIn("OpenClaw finished processing", text)
        self.assertNotIn("OpenClaw said:", text)
        self.assertNotIn("OpenClaw session:", text)

    def test_telegram_final_message_for_cursor_pr_mentions_cursor(self) -> None:
        text = format_final_message(
            "tsk_demo",
            status="completed",
            summary="Implemented the fix and opened a PR.",
            worker_label="Cursor",
            pr_url="https://github.com/example/repo/pull/2",
        )
        self.assertIn("Cursor prepared a PR for review", text)
        self.assertNotIn("OpenClaw completed it successfully", text)

    def test_telegram_final_message_uses_openclaw_model_speaker_label(self) -> None:
        text = format_final_message(
            "tsk_demo",
            status="completed",
            summary="Reviewed the plan and produced a concise synthesis.",
            worker_label="OpenClaw",
            provider="google",
            model="gemini-2.5-flash",
        )
        self.assertIn("OpenClaw model used: google / gemini-2.5-flash", text)

    def test_telegram_final_message_notes_cursor_primary_request(self) -> None:
        text = format_final_message(
            "tsk_demo",
            status="completed",
            summary="Cursor reviewed the result and the PR is ready.",
            worker_label="OpenClaw and Cursor",
            delegated_to_cursor=True,
            routing_hint="cursor",
            collaboration_mode="cursor_primary",
        )
        self.assertIn("Cursor execution lane", text)

    def test_telegram_final_message_full_visibility_shows_curated_collaboration_trace(self) -> None:
        text = format_final_message(
            "tsk_demo",
            status="completed",
            summary="Implemented the fix and verified the result.",
            worker_label="OpenClaw and Cursor",
            delegated_to_cursor=True,
            visibility_mode="full",
            collaboration_trace=[
                "OpenClaw triaged the issue and framed the plan.",
                "Cursor handled the repo-heavy execution.",
            ],
        )
        self.assertIn("Collaboration trace:", text)
        self.assertIn("OpenClaw triaged the issue", text)
        self.assertIn("Cursor handled the repo-heavy execution", text)

    def test_telegram_final_message_omits_duplicate_summary_block_when_summary_is_short(self) -> None:
        text = format_final_message(
            "tsk_demo",
            status="completed",
            summary="Implemented the fix and verified the result.",
            worker_label="OpenClaw",
        )
        self.assertNotIn("said:", text)
        self.assertEqual(text.count("Implemented the fix and verified the result."), 1)

    def test_telegram_final_message_failed_uses_error_footer(self) -> None:
        text = format_final_message(
            "tsk_demo",
            status="failed",
            summary="The agent could not finish the request.",
            last_error="cursor_status_failed",
        )
        self.assertIn("could not complete", text)
        self.assertIn("Failure:", text)
        self.assertIn("Error: cursor_status_failed", text)

    def test_telegram_final_message_failed_hybrid_mentions_both_workers(self) -> None:
        text = format_final_message(
            "tsk_demo",
            status="failed",
            summary="The collaboration failed before completion.",
            last_error="handoff_failed",
            worker_label="OpenClaw and Cursor",
            delegated_to_cursor=True,
        )
        self.assertIn("OpenClaw and Cursor did not complete this task successfully", text)

    def test_telegram_final_message_completed_soft_failure_summary_does_not_claim_finish(self) -> None:
        text = format_final_message(
            "tsk_demo",
            status="completed",
            summary=(
                "I apologize, but I am currently unable to hand off this troubleshooting task to Cursor."
            ),
            worker_label="OpenClaw",
        )
        self.assertNotIn("I finished your request.", text)
        self.assertIn("could not complete your request", text.lower())

    def test_server_followups_route_greeting_direct(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-direct",
                "payload": {
                    "text": "hi andrea how are you?",
                    "chat_id": 1,
                    "message_id": 2,
                },
            },
        )
        server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.COMPLETED.value)
        self.assertEqual(proj["meta"]["assistant"]["route"], "direct")
        self.assertNotIn("cursor", proj["meta"])

    def test_server_followups_route_hows_it_going_greeting_or_social(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-how-going",
                "payload": {
                    "text": "How's it going?",
                    "routing_text": "How's it going?",
                    "chat_id": 92002,
                    "message_id": 3,
                },
            },
        )
        server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.COMPLETED.value)
        self.assertEqual(proj["meta"]["assistant"]["route"], "direct")
        self.assertEqual(proj["meta"]["assistant"].get("reason"), "greeting_or_social")
        self.assertNotIn("cursor", proj["meta"])
        last_reply = str(proj["meta"].get("assistant", {}).get("last_reply") or "").lower()
        self.assertNotIn("technical details", last_reply)
        self.assertNotIn("what happened", last_reply)

    def test_server_followups_plain_hi_andrea_direct_without_task_summary_surface(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-hi-andrea",
                "payload": {
                    "text": "Hi Andrea",
                    "routing_text": "Hi Andrea",
                    "chat_id": 92003,
                    "message_id": 4,
                },
            },
        )
        server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.COMPLETED.value)
        self.assertEqual(proj["meta"]["assistant"]["route"], "direct")
        self.assertEqual(proj["meta"]["assistant"].get("reason"), "greeting_or_social")
        self.assertNotIn("cursor", proj["meta"])
        outcome = proj.get("meta", {}).get("outcome") or {}
        self.assertNotEqual(outcome.get("route_mode"), "delegate")
        last_reply = str(proj["meta"].get("assistant", {}).get("last_reply") or "").lower()
        self.assertNotIn("technical details", last_reply)
        self.assertNotIn("what happened", last_reply)

    def test_server_followups_route_cli_greeting_direct(self) -> None:
        prev_cli_auto = os.environ.get("ANDREA_SYNC_CLI_SUBMIT_AUTO_ROUTE")
        try:
            os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
            os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
            os.environ["ANDREA_SYNC_CLI_SUBMIT_AUTO_ROUTE"] = "1"
            from services.andrea_sync.server import SyncServer  # noqa: E402

            server = SyncServer()
            result = handle_command(
                server.conn,
                {
                    "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                    "channel": "cli",
                    "external_id": "cli-direct-greet",
                    "payload": {
                        "text": "hi andrea how are you?",
                        "routing_text": "hi andrea how are you?",
                    },
                },
            )
            server._handle_task_followups(result["task_id"])
            proj = project_task_dict(server.conn, result["task_id"], "cli")
            self.assertEqual(proj["status"], TaskStatus.COMPLETED.value)
            self.assertEqual(proj["meta"]["assistant"]["route"], "direct")
            self.assertNotIn("cursor", proj["meta"])
        finally:
            if prev_cli_auto is None:
                os.environ.pop("ANDREA_SYNC_CLI_SUBMIT_AUTO_ROUTE", None)
            else:
                os.environ["ANDREA_SYNC_CLI_SUBMIT_AUTO_ROUTE"] = prev_cli_auto

    def test_server_followups_repairs_generic_direct_with_goal_continuity(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-continuity-repair",
                "payload": {
                    "text": "What are we working on?",
                    "chat_id": 92001,
                    "message_id": 1,
                },
            },
        )
        task_id = str(result["task_id"])
        pre = project_task_dict(server.conn, task_id, "telegram")
        principal_id = str(((pre.get("meta") or {}).get("identity") or {}).get("principal_id") or "")
        self.assertTrue(principal_id)

        created = handle_command(
            server.conn,
            {
                "command_type": CommandType.CREATE_GOAL.value,
                "channel": "internal",
                "payload": {"principal_id": principal_id, "summary": "Finish continuity rollout"},
            },
        )
        self.assertTrue(created.get("ok"), created)
        goal_id = str(created.get("goal_id") or "")
        linked = handle_command(
            server.conn,
            {
                "command_type": CommandType.LINK_TASK_TO_GOAL.value,
                "channel": "internal",
                "task_id": task_id,
                "payload": {"task_id": task_id, "goal_id": goal_id},
            },
        )
        self.assertTrue(linked.get("ok"), linked)

        server._handle_task_followups(task_id)
        proj = project_task_dict(server.conn, task_id, "telegram")
        assistant = (proj.get("meta") or {}).get("assistant") or {}
        text = str(assistant.get("last_reply") or "")
        self.assertIn(
            assistant.get("reason"),
            {
                "continuity_state_repaired_direct_reply",
                "goal_runtime_status",
                "semantic_state_goal_status",
                "semantic_state_goal_continuity",
            },
        )
        self.assertIn(goal_id, text)
        self.assertNotIn("say a bit more", text.lower())

    def test_server_followups_status_right_now_without_active_work_is_graceful(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-status-right-now",
                "payload": {
                    "text": "What are we working on right now?",
                    "chat_id": 92002,
                    "message_id": 1,
                },
            },
        )
        task_id = str(result["task_id"])
        server._handle_task_followups(task_id)
        proj = project_task_dict(server.conn, task_id, "telegram")
        assistant = (proj.get("meta") or {}).get("assistant") or {}
        text = str(assistant.get("last_reply") or "").lower()
        self.assertNotIn("say a bit more about what you want", text)
        self.assertIn("do not see active tracked work right now", text)

    def test_server_followups_status_with_andrea_phrase_without_active_work_is_graceful(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-status-with-andrea",
                "payload": {
                    "text": "What are we working on with Andrea?",
                    "chat_id": 92003,
                    "message_id": 1,
                },
            },
        )
        task_id = str(result["task_id"])
        server._handle_task_followups(task_id)
        proj = project_task_dict(server.conn, task_id, "telegram")
        assistant = (proj.get("meta") or {}).get("assistant") or {}
        text = str(assistant.get("last_reply") or "").lower()
        self.assertNotIn("say a bit more about what you want", text)
        self.assertIn("do not see active tracked work right now", text)

    def test_server_followups_approval_prefers_pending_rows_over_stale_context(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        submit = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-approval-ranking",
                "payload": {
                    "text": "What still needs my approval?",
                    "chat_id": 92004,
                    "message_id": 1,
                },
            },
        )
        task_id = str(submit["task_id"])
        pre = project_task_dict(server.conn, task_id, "telegram")
        principal_id = str(((pre.get("meta") or {}).get("identity") or {}).get("principal_id") or "")
        created = handle_command(
            server.conn,
            {
                "command_type": CommandType.CREATE_GOAL.value,
                "channel": "internal",
                "payload": {"principal_id": principal_id, "summary": "Approval queue validation"},
            },
        )
        goal_id = str(created.get("goal_id") or "")
        handle_command(
            server.conn,
            {
                "command_type": CommandType.LINK_TASK_TO_GOAL.value,
                "channel": "internal",
                "task_id": task_id,
                "payload": {"task_id": task_id, "goal_id": goal_id},
            },
        )
        approval_id = create_goal_approval(
            server.conn,
            goal_id,
            task_id,
            rationale="Waiting on your sign-off for execution.",
        )
        server._handle_task_followups(task_id)
        proj = project_task_dict(server.conn, task_id, "telegram")
        assistant = (proj.get("meta") or {}).get("assistant") or {}
        text = str(assistant.get("last_reply") or "")
        self.assertIn("Pending approvals for tracked task", text)
        self.assertIn(approval_id, text)
        self.assertNotIn("OpenClaw run:", text)

    def test_server_followups_approval_none_pending_is_explicit(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        submit = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-approval-none",
                "payload": {
                    "text": "What still needs my approval?",
                    "chat_id": 92005,
                    "message_id": 1,
                },
            },
        )
        task_id = str(submit["task_id"])
        server._handle_task_followups(task_id)
        proj = project_task_dict(server.conn, task_id, "telegram")
        assistant = (proj.get("meta") or {}).get("assistant") or {}
        text = str(assistant.get("last_reply") or "").lower()
        self.assertIn("approval requests waiting on you right now", text)
        self.assertNotIn("say a bit more about what you want", text)

    def test_server_followups_common_intents_do_not_emit_generic_fallback(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        prompts = [
            ("tg-intent-casual", "How’s it going?"),
            ("tg-intent-status", "What are we working on right now?"),
            ("tg-intent-approval", "What still needs my approval?"),
        ]
        for external_id, text in prompts:
            submit = handle_command(
                server.conn,
                {
                    "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                    "channel": "telegram",
                    "external_id": external_id,
                    "payload": {
                        "text": text,
                        "chat_id": 92100,
                        "message_id": 1,
                    },
                },
            )
            task_id = str(submit["task_id"])
            server._handle_task_followups(task_id)
            proj = project_task_dict(server.conn, task_id, "telegram")
            assistant = (proj.get("meta") or {}).get("assistant") or {}
            reply = str(assistant.get("last_reply") or "").lower()
            self.assertNotIn("say a bit more about what you want", reply)

    def test_server_followups_cli_skips_routing_when_auto_route_disabled(self) -> None:
        prev_cli_auto = os.environ.get("ANDREA_SYNC_CLI_SUBMIT_AUTO_ROUTE")
        try:
            os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
            os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
            os.environ["ANDREA_SYNC_CLI_SUBMIT_AUTO_ROUTE"] = "0"
            from services.andrea_sync.server import SyncServer  # noqa: E402

            server = SyncServer()
            result = handle_command(
                server.conn,
                {
                    "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                    "channel": "cli",
                    "external_id": "cli-no-auto-route",
                    "payload": {
                        "text": "hi andrea how are you?",
                        "routing_text": "hi andrea how are you?",
                    },
                },
            )
            server._handle_task_followups(result["task_id"])
            proj = project_task_dict(server.conn, result["task_id"], "cli")
            self.assertEqual(proj["status"], TaskStatus.CREATED.value)
        finally:
            if prev_cli_auto is None:
                os.environ.pop("ANDREA_SYNC_CLI_SUBMIT_AUTO_ROUTE", None)
            else:
                os.environ["ANDREA_SYNC_CLI_SUBMIT_AUTO_ROUTE"] = prev_cli_auto

    def test_telegram_followups_summary_skips_lifecycle_when_quiet(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "1"
        os.environ["ANDREA_SYNC_TELEGRAM_QUIET_LIFECYCLE"] = "1"
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-quiet-lifecycle",
                "payload": {
                    "text": "Please inspect the repo and fix the failing tests.",
                    "chat_id": 91001,
                    "message_id": 1,
                },
            },
        )
        with mock.patch.object(tg_adapt, "send_text_message") as send_mock:
            with mock.patch.object(server, "_schedule_delegated_execution"):
                server._handle_task_followups(result["task_id"])
                server._handle_task_followups(result["task_id"])
        send_mock.assert_not_called()

    def test_telegram_followups_summary_sends_ack_when_lifecycle_verbose(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "1"
        os.environ["ANDREA_SYNC_TELEGRAM_QUIET_LIFECYCLE"] = "0"
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            self.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-verbose-lifecycle",
                "payload": {
                    "text": "Please inspect the repo and fix the failing tests.",
                    "chat_id": 91002,
                    "message_id": 2,
                },
            },
        )
        with mock.patch.object(tg_adapt, "send_text_message") as send_mock:
            with mock.patch.object(server, "_schedule_delegated_execution"):
                server._handle_task_followups(result["task_id"])
                server._handle_task_followups(result["task_id"])
        self.assertGreaterEqual(send_mock.call_count, 1)
        first = send_mock.call_args.kwargs["text"]
        self.assertTrue("queued" in first.lower() or "task" in first.lower())

    def test_server_followups_route_repo_request_delegate(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-delegate",
                "payload": {
                    "text": "Please inspect the repo and fix the failing tests.",
                    "chat_id": 1,
                    "message_id": 3,
                },
            },
        )
        server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.QUEUED.value)
        self.assertEqual(proj["meta"]["cursor"]["kind"], "openclaw")
        self.assertEqual(proj["meta"]["execution"]["lane"], "openclaw_hybrid")

    def test_turn_plan_forces_delegate_for_troubleshoot_domain(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-turn-plan-troubleshoot",
                "payload": {
                    "text": "It keeps crashing after startup. What should we do?",
                    "chat_id": 1,
                    "message_id": 203,
                },
            },
        )
        server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.QUEUED.value)
        self.assertEqual(proj["meta"]["execution"]["lane"], "openclaw_hybrid")
        self.assertEqual(
            proj["meta"]["execution"]["route_reason"],
            "turn_plan_technical_execution",
        )

    def test_create_openclaw_job_passes_explicit_session_id(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        with mock.patch.object(server, "_run_json_subprocess", return_value={"ok": True}) as run_mock:
            server._create_openclaw_job(
                "tsk_demo",
                "do thing",
                "technical_or_repo_request",
                "auto",
                "",
                "",
                session_id="sess-demo-1",
            )
        argv = run_mock.call_args.args[0]
        self.assertIn("--session-id", argv)
        self.assertIn("sess-demo-1", argv)

    def test_refresh_openclaw_runtime_falls_back_to_session_rotation(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["ANDREA_OPENCLAW_REFRESH_MODE"] = "auto"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        with mock.patch.object(
            server,
            "_run_text_subprocess",
            side_effect=RuntimeError("restart failed"),
        ):
            refresh = server._refresh_openclaw_runtime(
                "tsk_demo",
                skill_key="voice-call",
                heal_result={
                    "refresh_required": True,
                    "actions": [{"kind": "config_repair"}],
                },
            )
        self.assertTrue(refresh["ok"])
        self.assertEqual(refresh["mode"], "session_rotation")
        self.assertIn("andrea-sync", refresh["session_id"])

    def test_server_answers_messaging_capability_from_runtime_truth(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-text-capability",
                "payload": {
                    "text": "You can send text messages right?",
                    "chat_id": 9001,
                    "message_id": 44,
                },
            },
        )
        with mock.patch.object(
            server,
            "_resolve_messaging_capability",
            return_value={"label": "text messaging", "truth": {"status": "verified_available"}},
        ):
            server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        reply = proj["meta"]["assistant"]["last_reply"]
        self.assertIn("verified text messaging lane", reply.lower())
        self.assertNotIn("session", reply.lower())

    def test_server_messaging_read_capability_uses_retrieval_oriented_copy(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-text-read-capability",
                "payload": {
                    # Read-focused capability ask (not the structured recent-text fetch path).
                    "text": "Are you able to read my iMessages through BlueBubbles?",
                    "chat_id": 9002,
                    "message_id": 45,
                },
            },
        )
        with (
            mock.patch.object(
                server,
                "_parse_recent_text_messages_request",
                return_value=None,
            ),
            mock.patch.object(
                server,
                "_resolve_messaging_capability",
                return_value={
                    "label": "text messaging",
                    "skill_key": "bluebubbles",
                    "truth": {"status": "verified_available"},
                },
            ),
        ):
            server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        reply = proj["meta"]["assistant"]["last_reply"]
        self.assertEqual(proj["meta"]["assistant"]["reason"], "messaging_capability_read_answer")
        self.assertIn("reading recent", reply.lower())
        self.assertIn("draft", reply.lower())

    def test_server_live_news_request_uses_capability_backed_summary(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-live-news",
                "payload": {
                    "text": "What's the news today?",
                    "chat_id": 90011,
                    "message_id": 441,
                },
            },
        )
        with (
            mock.patch.object(
                server,
                "_resolve_runtime_skill",
                return_value={"skill_key": "brave-api-search", "truth": {"status": "verified_available"}},
            ) as resolve_mock,
            mock.patch.object(
                server,
                "_create_openclaw_job",
                return_value={
                    "ok": True,
                    "user_summary": "Live news: AI funding and market headlines led the day, with major policy updates still developing.",
                },
            ) as job_mock,
        ):
            server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        reply = proj["meta"]["assistant"]["last_reply"]
        self.assertEqual(proj["meta"]["assistant"]["route"], "direct")
        self.assertEqual(proj["meta"]["assistant"]["reason"], "news_summary_ready")
        self.assertIn("live news", reply.lower())
        self.assertNotIn("session", reply.lower())
        self.assertNotIn("what would you like to do", reply.lower())
        resolve_mock.assert_called_once()
        self.assertEqual(resolve_mock.call_args.kwargs["skill_key"], "brave-api-search")
        job_mock.assert_called_once()
        event_types = [event_type for _seq, _ts, event_type, _payload in load_events_for_task(server.conn, result["task_id"])]
        self.assertNotIn(EventType.JOB_QUEUED.value, event_types)

    def test_server_live_news_request_prefers_raw_text_over_generic_openclaw_summary(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-live-news-raw-text",
                "payload": {
                    "text": "What's the news today?",
                    "chat_id": 90015,
                    "message_id": 445,
                },
            },
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
                    "summary": "OpenClaw completed the delegated task.",
                    "raw_text": "Live news: AI funding and market headlines led the day, with policy updates still moving.",
                },
            ),
        ):
            server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        reply = proj["meta"]["assistant"]["last_reply"]
        self.assertEqual(proj["meta"]["assistant"]["reason"], "news_summary_ready")
        self.assertIn("live news", reply.lower())
        self.assertNotIn("completed the delegated task", reply.lower())

    def test_run_direct_openclaw_lookup_rejects_contaminated_summary(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        with mock.patch.object(
            server,
            "_create_openclaw_job",
            return_value={
                "ok": True,
                "user_summary": (
                    "Continuing the Extreme Masterclass Self-Improvement Sprint from yesterday."
                ),
            },
        ):
            reply, reason = server._run_direct_openclaw_lookup(
                "t_contaminated",
                prompt="news",
                route_reason="structured_live_news",
                success_reason="news_summary_ready",
                success_fallback="fallback ok",
                failure_reason="news_summary_failed",
                failure_reply="I couldn't pull a grounded live news summary cleanly just now.",
            )
        self.assertEqual(reason, "news_summary_failed_contaminated")
        self.assertEqual(reply, "I couldn't pull a grounded live news summary cleanly just now.")
        self.assertNotIn("Masterclass", reply)

    def test_run_direct_openclaw_lookup_rejects_embedding_quota_summary(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        with mock.patch.object(
            server,
            "_create_openclaw_job",
            return_value={
                "ok": True,
                "user_summary": "embedding quota exceeded; try again later.",
            },
        ):
            reply, reason = server._run_direct_openclaw_lookup(
                "t_embed_leak",
                prompt="msgs",
                route_reason="structured_recent_text_messages",
                success_reason="recent_text_messages_ready",
                success_fallback="fallback ok",
                failure_reason="recent_text_messages_failed",
                failure_reply="I couldn't retrieve your recent text messages cleanly just now.",
            )
        self.assertEqual(reason, "recent_text_messages_failed_contaminated")
        self.assertEqual(
            reply,
            "I couldn't retrieve your recent text messages cleanly just now.",
        )

    def test_run_direct_openclaw_lookup_uses_ephemeral_openclaw_session(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        captured: dict[str, str] = {}

        def capture_job(*_args: Any, **kwargs: Any) -> dict[str, Any]:
            captured["session_id"] = str(kwargs.get("session_id") or "")
            return {"ok": True, "user_summary": "Grounded one-line summary."}

        with mock.patch.object(server, "_create_openclaw_job", side_effect=capture_job):
            server._run_direct_openclaw_lookup(
                "t_eph",
                prompt="news",
                route_reason="structured_live_news",
                success_reason="news_summary_ready",
                success_fallback="fb",
                failure_reason="news_summary_failed",
                failure_reply="fail",
            )
        sid = captured.get("session_id", "")
        self.assertTrue(sid.startswith("andrea-lookup-structured_live_news-"), msg=sid)

    def test_run_direct_openclaw_lookup_respects_task_session_when_ephemeral_disabled(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        with mock.patch.dict(os.environ, {"ANDREA_OPENCLAW_LOOKUP_EPHEMERAL_SESSION": "0"}):
            from services.andrea_sync.server import SyncServer  # noqa: E402

            server = SyncServer()
            captured: dict[str, str] = {}

            def capture_job(*_args: Any, **kwargs: Any) -> dict[str, Any]:
                captured["session_id"] = str(kwargs.get("session_id") or "")
                return {"ok": True, "user_summary": "OK"}

            with mock.patch.object(server, "_create_openclaw_job", side_effect=capture_job):
                server._run_direct_openclaw_lookup(
                    "t_task_sess",
                    prompt="x",
                    route_reason="structured_recent_text_messages",
                    success_reason="ok",
                    success_fallback="fb",
                    failure_reason="failed",
                    failure_reply="fail",
                )
        self.assertEqual(captured.get("session_id"), "andrea-sync-main-t_task_sess-0")

    def test_is_stale_openclaw_narrative_detects_handoff_and_runtime(self) -> None:
        self.assertTrue(is_stale_openclaw_narrative("Extreme Masterclass recap"))
        self.assertTrue(is_stale_openclaw_narrative("multi-agent handoff complete"))
        self.assertTrue(is_stale_openclaw_narrative("Set sessions_spawn.attachments.enabled to true"))
        self.assertTrue(is_stale_openclaw_narrative("OpenClaw embedding quota exceeded for this session."))
        self.assertTrue(is_stale_openclaw_narrative("context window exceeded for the model"))
        self.assertFalse(is_stale_openclaw_narrative("Live news: markets moved higher today."))

    def test_scrub_history_for_direct_drops_stale_assistant_turn(self) -> None:
        hist = [
            {"role": "user", "content": "Hi"},
            {
                "role": "assistant",
                "content": "We are in an Extreme Masterclass Self-Improvement Sprint.",
            },
            {"role": "user", "content": "What's the weather like?"},
        ]
        out = _scrub_history_for_direct(hist)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["role"], "user")
        self.assertEqual(out[1]["content"], "What's the weather like?")

    def test_scrub_history_for_direct_keeps_clean_assistant_turn(self) -> None:
        hist = [
            {"role": "assistant", "content": "Your meeting is at 3pm."},
        ]
        out = _scrub_history_for_direct(hist)
        self.assertEqual(len(out), 1)
        self.assertIn("3pm", out[0]["content"])

    def test_scrub_history_for_direct_drops_embedding_quota_assistant_turn(self) -> None:
        hist = [
            {"role": "user", "content": "Any texts today?"},
            {
                "role": "assistant",
                "content": "Sorry, embedding quota exceeded for this session.",
            },
            {"role": "user", "content": "What's the weather?"},
        ]
        out = _scrub_history_for_direct(hist)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["content"], "What's the weather?")

    def test_parse_recent_text_messages_request_accepts_from_today_phrasing(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        text = "Any texts from today?"
        self.assertEqual(server._parse_recent_text_messages_request(text), text)

    def test_parse_recent_text_messages_request_accepts_pull_via_bluebubbles(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        text = "Can you pull text messages from BlueBubbles?"
        self.assertEqual(server._parse_recent_text_messages_request(text), text)

    def test_server_live_news_request_stays_truthful_when_lane_unavailable(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-live-news-unavailable",
                "payload": {
                    "text": "What's the latest news?",
                    "chat_id": 90012,
                    "message_id": 442,
                },
            },
        )
        with mock.patch.object(
            server,
            "_resolve_runtime_skill",
            return_value={"skill_key": "brave-api-search", "truth": {"status": "installed_but_not_eligible"}},
        ):
            server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        reply = proj["meta"]["assistant"]["last_reply"]
        self.assertEqual(proj["meta"]["assistant"]["route"], "direct")
        self.assertEqual(proj["meta"]["assistant"]["reason"], "news_summary_unavailable")
        self.assertIn("live news lane", reply.lower())
        self.assertIn("local setup", reply.lower())
        event_types = [event_type for _seq, _ts, event_type, _payload in load_events_for_task(server.conn, result["task_id"])]
        self.assertNotIn(EventType.JOB_QUEUED.value, event_types)

    def test_server_recent_text_messages_use_bluebubbles_summary(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-recent-texts",
                "payload": {
                    "text": "What are my recent text messages?",
                    "chat_id": 90013,
                    "message_id": 443,
                },
            },
        )
        with (
            mock.patch.object(
                server,
                "_resolve_messaging_capability",
                return_value={
                    "skill_key": "bluebubbles",
                    "label": "text messaging",
                    "truth": {"status": "verified_available"},
                },
            ) as resolve_mock,
            mock.patch.object(
                server,
                "_create_openclaw_job",
                return_value={
                    "ok": True,
                    "user_summary": "Recent texts: Candace said she's on her way, and Michael asked whether tomorrow still works.",
                },
            ) as job_mock,
        ):
            server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        reply = proj["meta"]["assistant"]["last_reply"]
        self.assertEqual(proj["meta"]["assistant"]["route"], "direct")
        self.assertEqual(proj["meta"]["assistant"]["reason"], "recent_text_messages_ready")
        self.assertIn("recent texts", reply.lower())
        self.assertNotIn("session", reply.lower())
        resolve_mock.assert_called_once()
        self.assertEqual(resolve_mock.call_args.args[0], result["task_id"])
        job_mock.assert_called_once()
        event_types = [event_type for _seq, _ts, event_type, _payload in load_events_for_task(server.conn, result["task_id"])]
        self.assertNotIn(EventType.JOB_QUEUED.value, event_types)

    def test_server_recent_text_messages_fall_back_when_openclaw_summary_is_generic(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-recent-texts-generic-summary",
                "payload": {
                    "text": "What are my recent text messages?",
                    "chat_id": 90016,
                    "message_id": 446,
                },
            },
        )
        with mock.patch.object(
            server,
            "_resolve_messaging_capability",
            return_value={
                "skill_key": "bluebubbles",
                "label": "text messaging",
                "truth": {"status": "verified_available"},
            },
        ), mock.patch.object(
            server,
            "_create_openclaw_job",
            return_value={
                "ok": True,
                "summary": "OpenClaw completed the delegated task.",
            },
        ):
            server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        reply = proj["meta"]["assistant"]["last_reply"]
        self.assertEqual(proj["meta"]["assistant"]["reason"], "recent_text_messages_ready")
        self.assertIn("recent text-message summary", reply.lower())
        self.assertNotIn("completed the delegated task", reply.lower())

    def test_server_recent_text_messages_stay_truthful_when_lane_unavailable(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-recent-texts-unavailable",
                "payload": {
                    "text": "What are my recent text messages?",
                    "chat_id": 90014,
                    "message_id": 444,
                },
            },
        )
        with mock.patch.object(
            server,
            "_resolve_messaging_capability",
            return_value={
                "skill_key": "bluebubbles",
                "label": "text messaging",
                "truth": {"status": "installed_but_not_eligible"},
            },
        ):
            server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        reply = proj["meta"]["assistant"]["last_reply"]
        self.assertEqual(proj["meta"]["assistant"]["route"], "direct")
        self.assertEqual(proj["meta"]["assistant"]["reason"], "recent_text_messages_unavailable")
        self.assertIn("bluebubbles", reply.lower())
        self.assertIn("recent messages", reply.lower())
        self.assertNotIn("session", reply.lower())
        event_types = [event_type for _seq, _ts, event_type, _payload in load_events_for_task(server.conn, result["task_id"])]
        self.assertNotIn(EventType.JOB_QUEUED.value, event_types)

    def test_server_drafts_outbound_message_before_send(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-draft-message",
                "payload": {
                    "text": "Tell Candace hi from you",
                    "chat_id": 9002,
                    "message_id": 45,
                },
            },
        )
        with mock.patch.object(
            server,
            "_resolve_messaging_capability",
            return_value={
                "skill_key": "bluebubbles",
                "label": "text messaging",
                "truth": {"status": "verified_available"},
            },
        ):
            server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        reply = proj["meta"]["assistant"]["last_reply"]
        self.assertIn("Draft for Candace", reply)
        pending = server._load_pending_outbound_draft(result["task_id"])
        self.assertEqual(pending.get("target"), "Candace")
        self.assertEqual(pending.get("message"), "hi from you")

    def test_server_confirmation_sends_pending_outbound_message(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        first = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-draft-message-1",
                "payload": {
                    "text": "Tell Candace hi from you",
                    "chat_id": 9003,
                    "message_id": 46,
                },
            },
        )
        with mock.patch.object(
            server,
            "_resolve_messaging_capability",
            return_value={
                "skill_key": "bluebubbles",
                "label": "text messaging",
                "truth": {"status": "verified_available"},
            },
        ):
            server._handle_task_followups(first["task_id"])
        second = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-draft-message-2",
                "payload": {
                    "text": "send it",
                    "chat_id": 9003,
                    "message_id": 47,
                },
            },
        )

        def _fake_send(task_id: str, draft: dict[str, Any]) -> tuple[str, str]:
            server._clear_pending_outbound_draft(task_id)
            return "I sent it to Candace.", "outbound_message_sent"

        with mock.patch.object(
            server,
            "_send_pending_outbound_message",
            side_effect=_fake_send,
        ):
            server._handle_task_followups(second["task_id"])
        proj = project_task_dict(server.conn, second["task_id"], "telegram")
        reply = proj["meta"]["assistant"]["last_reply"]
        self.assertEqual(reply, "I sent it to Candace.")
        self.assertFalse(server._load_pending_outbound_draft(second["task_id"]))

    def test_server_outbound_phone_number_only_stays_product_safe(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-phone-only-message",
                "payload": {
                    "text": "Send a message to +15555550123 saying hi",
                    "chat_id": 9004,
                    "message_id": 48,
                },
            },
        )
        server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        reply = proj["meta"]["assistant"]["last_reply"]
        self.assertIn("resolvable contact or thread", reply)
        self.assertNotIn("session", reply.lower())

    def test_telegram_final_summary_sanitizes_install_and_config_jargon(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        text = server._telegram_final_summary_text(
            {
                "summary": "openclaw skills install bluebubbles failed",
                "meta": {
                    "openclaw": {
                        "blocked_reason": "plugins.entries.voice-call.enabled is still missing",
                    }
                },
            }
        )
        self.assertNotIn("openclaw skills install", text.lower())
        self.assertNotIn("plugins.entries", text.lower())
        self.assertIn("internal limitation", text.lower())

    def test_server_followups_explicit_andrea_mention_stays_direct(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-andrea-mention",
                "payload": {
                    "text": "@Andrea how are you today?",
                    "routing_text": "how are you today?",
                    "routing_hint": "andrea",
                    "collaboration_mode": "andrea_primary",
                    "mention_targets": ["andrea"],
                    "chat_id": 1,
                    "message_id": 29,
                },
            },
        )
        server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.COMPLETED.value)
        self.assertEqual(proj["meta"]["telegram"]["routing_hint"], "andrea")
        self.assertEqual(proj["meta"]["assistant"]["route"], "direct")

    def test_server_followups_explicit_andrea_mention_can_delegate_action(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-andrea-action",
                "payload": {
                    "text": "@Andrea please inspect the repo and fix the failing tests",
                    "routing_text": "please inspect the repo and fix the failing tests",
                    "routing_hint": "andrea",
                    "collaboration_mode": "andrea_primary",
                    "mention_targets": ["andrea"],
                    "chat_id": 1,
                    "message_id": 291,
                },
            },
        )
        server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.QUEUED.value)
        self.assertEqual(proj["meta"]["execution"]["lane"], "openclaw_hybrid")

    def test_server_followups_structured_reminder_stays_direct_and_creates_proactive_meta(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-structured-reminder",
                "payload": {
                    "text": "Remind me to review the StoryLiner repo tomorrow morning.",
                    "routing_text": "remind me to review the StoryLiner repo tomorrow morning",
                    "chat_id": 1,
                    "message_id": 292,
                    "from_user": 11,
                },
            },
        )
        with mock.patch.object(
            server,
            "_resolve_runtime_skill",
            return_value={"truth": {"status": "verified_available"}},
        ):
            server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.COMPLETED.value)
        self.assertEqual(proj["meta"]["assistant"]["reason"], "reminder_created")
        self.assertIn("Apple Reminders lane is verified", proj["meta"]["assistant"]["last_reply"])
        self.assertEqual(proj["meta"]["proactive"]["pending_reminder_count"], 1)
        self.assertEqual(proj["meta"]["outcome"]["pending_reminder_count"], 1)

    def test_server_followups_memory_note_is_saved_on_principal(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-memory-note",
                "payload": {
                    "text": "remember that I prefer full dialogue for repo work",
                    "routing_text": "remember that I prefer full dialogue for repo work",
                    "chat_id": 1,
                    "message_id": 293,
                    "from_user": 12,
                },
            },
        )
        with mock.patch.object(
            server,
            "_resolve_runtime_skill",
            return_value={"truth": {"status": "verified_available"}},
        ):
            server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.COMPLETED.value)
        self.assertEqual(proj["meta"]["assistant"]["reason"], "principal_memory_saved")
        self.assertIn("Apple Notes lane is verified", proj["meta"]["assistant"]["last_reply"])
        self.assertEqual(proj["meta"]["identity"]["memory_count"], 1)
        self.assertTrue(proj["meta"]["identity"]["principal_id"].startswith("prn_"))

    def test_server_followups_explicit_cursor_mention_marks_cursor_primary(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-cursor-mention",
                "payload": {
                    "text": "@Cursor fix the failing tests",
                    "routing_text": "fix the failing tests",
                    "routing_hint": "cursor",
                    "collaboration_mode": "cursor_primary",
                    "mention_targets": ["cursor"],
                    "chat_id": 1,
                    "message_id": 30,
                },
            },
        )
        server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.QUEUED.value)
        self.assertEqual(proj["meta"]["execution"]["routing_hint"], "cursor")
        self.assertEqual(proj["meta"]["execution"]["collaboration_mode"], "cursor_primary")

    def test_server_followups_explicit_model_mention_marks_preferred_lane(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-gemini-mention",
                "payload": {
                    "text": "@Gemini please review the repo approach",
                    "routing_text": "please review the repo approach",
                    "preferred_model_family": "gemini",
                    "preferred_model_label": "Gemini",
                    "model_mentions": ["gemini"],
                    "chat_id": 1,
                    "message_id": 31,
                },
            },
        )
        server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.QUEUED.value)
        self.assertEqual(proj["meta"]["telegram"]["preferred_model_family"], "gemini")
        self.assertEqual(proj["meta"]["execution"]["preferred_model_label"], "Gemini")

    def test_server_followups_collaborative_full_visibility_sets_execution_meta(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-collab-full",
                "payload": {
                    "text": "@Andrea @Cursor work together and show the full dialogue",
                    "routing_text": "work together and show the full dialogue",
                    "routing_hint": "collaborate",
                    "collaboration_mode": "collaborative",
                    "visibility_mode": "full",
                    "mention_targets": ["andrea", "cursor"],
                    "chat_id": 1,
                    "message_id": 301,
                },
            },
        )
        server._handle_task_followups(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.QUEUED.value)
        self.assertEqual(proj["meta"]["telegram"]["visibility_mode"], "full")
        self.assertEqual(proj["meta"]["execution"]["visibility_mode"], "full")
        self.assertEqual(proj["meta"]["telegram"]["requested_capability"], "collaboration")

    def test_task_projection_sets_phase_hint_for_running_cursor_execution(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-phase-running",
                "payload": {
                    "text": "@Cursor fix the repo issue",
                    "routing_text": "fix the repo issue",
                    "routing_hint": "cursor",
                    "collaboration_mode": "cursor_primary",
                    "chat_id": 1,
                    "message_id": 303,
                },
            },
        )
        append_event(
            server.conn,
            result["task_id"],
            EventType.JOB_QUEUED,
            {
                "kind": "openclaw",
                "execution_lane": "openclaw_hybrid",
                "runner": "openclaw",
                "collaboration_mode": "cursor_primary",
            },
        )
        append_event(
            server.conn,
            result["task_id"],
            EventType.JOB_STARTED,
            {
                "backend": "openclaw",
                "execution_lane": "openclaw_hybrid",
                "runner": "openclaw",
                "delegated_to_cursor": True,
                "agent_url": "https://cursor.com/agents/running-phase",
            },
        )
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        outcome = proj["meta"]["outcome"]
        self.assertEqual(outcome["current_phase"], "execution")
        self.assertEqual(outcome["current_phase_status"], "running")
        self.assertEqual(outcome["current_phase_lane"], "cursor")
        self.assertEqual(outcome["completed_phases"], [])

    def test_task_projection_records_completed_phase_hints_from_phase_outputs(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-phase-complete",
                "payload": {
                    "text": "@Andrea @Cursor work together on a fix",
                    "routing_text": "work together on a fix",
                    "routing_hint": "collaborate",
                    "collaboration_mode": "collaborative",
                    "visibility_mode": "full",
                    "chat_id": 1,
                    "message_id": 304,
                },
            },
        )
        append_event(
            server.conn,
            result["task_id"],
            EventType.JOB_QUEUED,
            {
                "kind": "openclaw",
                "execution_lane": "openclaw_hybrid",
                "runner": "openclaw",
                "collaboration_mode": "collaborative",
                "visibility_mode": "full",
            },
        )
        append_event(
            server.conn,
            result["task_id"],
            EventType.JOB_COMPLETED,
            {
                "summary": "Finished the fix.",
                "backend": "openclaw",
                "execution_lane": "openclaw_hybrid",
                "runner": "openclaw",
                "delegated_to_cursor": True,
                "phase_outputs": {
                    "plan": {"lane": "openclaw", "status": "completed", "summary": "Planned the fix."},
                    "critique": {"lane": "openclaw", "status": "completed", "summary": "Critiqued the plan."},
                    "execution": {"lane": "cursor", "status": "completed", "summary": "Applied and tested the patch."},
                    "synthesis": {"lane": "openclaw", "status": "completed", "summary": "Finished the fix."},
                },
            },
        )
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        outcome = proj["meta"]["outcome"]
        self.assertEqual(outcome["current_phase"], "synthesis")
        self.assertEqual(outcome["current_phase_status"], "completed")
        self.assertEqual(
            outcome["completed_phases"],
            ["plan", "critique", "execution", "synthesis"],
        )
        self.assertEqual(outcome["phase_statuses"]["execution"], "completed")

    def test_task_visibility_mode_prefers_full_when_telegram_upgrades(self) -> None:
        """When telegram has 'full' and execution has 'summary', use full (continuation upgrade)."""
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-visibility-upgrade",
                "payload": {
                    "text": "fix the repo",
                    "routing_text": "fix the repo",
                    "visibility_mode": "summary",
                    "chat_id": 888,
                    "message_id": 401,
                },
            },
        )
        append_event(
            server.conn,
            result["task_id"],
            EventType.JOB_QUEUED,
            {
                "kind": "openclaw",
                "execution_lane": "openclaw_hybrid",
                "runner": "openclaw",
                "visibility_mode": "summary",
            },
        )
        append_event(
            server.conn,
            result["task_id"],
            EventType.USER_MESSAGE,
            {
                "channel": "telegram",
                "text": "and show the full dialogue",
                "routing_text": "and show the full dialogue",
                "visibility_mode": "full",
                "chat_id": 888,
                "message_id": 402,
                "telegram_continuation": True,
            },
        )
        mode = server._task_visibility_mode(result["task_id"])
        self.assertEqual(mode, "full")

    def test_server_running_followups_emit_progress_message_for_full_visibility(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "1"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        created = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-progress-full",
                "payload": {
                    "text": "@Andrea @Cursor work together and show the full dialogue",
                    "routing_text": "work together and show the full dialogue",
                    "routing_hint": "collaborate",
                    "collaboration_mode": "collaborative",
                    "visibility_mode": "full",
                    "mention_targets": ["andrea", "cursor"],
                    "chat_id": 777,
                    "message_id": 302,
                },
            },
        )
        append_event(
            server.conn,
            created["task_id"],
            EventType.JOB_STARTED,
            {
                "backend": "openclaw",
                "execution_lane": "openclaw_hybrid",
                "runner": "openclaw",
                "routing_hint": "collaborate",
                "collaboration_mode": "collaborative",
                "visibility_mode": "full",
                "provider": "google",
                "model": "gemini-2.5-flash",
            },
        )
        append_event(
            server.conn,
            created["task_id"],
            EventType.JOB_PROGRESS,
            {
                "message": "OpenClaw completed the triage pass and Cursor is preparing the execution step.",
                "backend": "openclaw",
                "runner": "openclaw",
                "provider": "google",
                "model": "gemini-2.5-flash",
                "visibility_mode": "full",
                "force_telegram_note": True,
            },
        )
        snapshot = server._task_snapshot(created["task_id"])
        assert snapshot is not None
        with mock.patch.object(tg_adapt, "send_text_message") as send_mock:
            server._handle_telegram_followups(created["task_id"], snapshot)
        sent_texts = [call.kwargs["text"] for call in send_mock.call_args_list]
        self.assertEqual(len(sent_texts), 2)
        self.assertTrue(any("actively working" in text.lower() for text in sent_texts))
        self.assertTrue(any("coordination update" in text.lower() for text in sent_texts))

    def test_server_openclaw_only_job_completes(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-openclaw",
                "payload": {
                    "text": "Please inspect the repo and summarize the likely failing tests.",
                    "chat_id": 1,
                    "message_id": 30,
                },
            },
        )
        server._handle_task_followups(result["task_id"])
        with mock.patch.object(
            server,
            "_create_openclaw_job",
            return_value={
                "ok": True,
                "summary": "Created the reminder and captured the follow-up.",
                "backend": "openclaw",
                "execution_lane": "openclaw_hybrid",
                "delegated_to_cursor": False,
                "openclaw_run_id": "run-demo",
                "openclaw_session_id": "sess-demo",
                "provider": "google",
                "model": "gemini-2.5-flash",
            },
        ):
            server._run_delegated_job(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.COMPLETED.value)
        self.assertEqual(proj["meta"]["execution"]["backend"], "openclaw")
        self.assertFalse(proj["meta"]["execution"]["delegated_to_cursor"])
        self.assertEqual(proj["meta"]["openclaw"]["session_id"], "sess-demo")

    def test_server_openclaw_escalation_carries_cursor_metadata(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-openclaw-cursor",
                "payload": {
                    "text": "Please inspect the repo, fix the failing tests, and open a PR.",
                    "chat_id": 1,
                    "message_id": 31,
                },
            },
        )
        server._handle_task_followups(result["task_id"])
        oc_ret = {
            "ok": True,
            "summary": "OpenClaw used cursor_handoff and a PR is ready.",
            "backend": "openclaw",
            "execution_lane": "openclaw_hybrid",
            "delegated_to_cursor": True,
            "openclaw_run_id": "run-demo",
            "openclaw_session_id": "sess-demo",
            "provider": "google",
            "model": "gemini-2.5-flash",
            "cursor_agent_id": "bc-demo",
            "agent_url": "https://cursor.com/agents/demo",
            "pr_url": "https://github.com/example/repo/pull/1",
        }
        with mock.patch.object(server, "_create_openclaw_job", return_value=oc_ret), mock.patch.object(
            server,
            "_poll_cursor_agent_terminal",
            return_value=(
                "FINISHED",
                {},
                str(oc_ret.get("agent_url") or ""),
                str(oc_ret.get("pr_url") or ""),
            ),
        ):
            server._run_delegated_job(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.COMPLETED.value)
        self.assertTrue(proj["meta"]["execution"]["delegated_to_cursor"])
        self.assertEqual(proj["meta"]["cursor"]["agent_url"], "https://cursor.com/agents/demo")
        self.assertEqual(proj["meta"]["cursor"]["pr_url"], "https://github.com/example/repo/pull/1")

    def test_server_cursor_primary_escalates_if_openclaw_does_not_delegate(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-cursor-primary-fallback",
                "payload": {
                    "text": "@Cursor review this plan",
                    "routing_text": "review this plan",
                    "routing_hint": "cursor",
                    "collaboration_mode": "cursor_primary",
                    "mention_targets": ["cursor"],
                    "chat_id": 1,
                    "message_id": 32,
                },
            },
        )
        server._handle_task_followups(result["task_id"])

        def fake_cursor_run(task_id: str) -> None:
            server._append_task_event(
                task_id,
                EventType.JOB_COMPLETED,
                {
                    "summary": "Cursor reviewed the plan and suggested improvements.",
                    "backend": "cursor",
                    "delegated_to_cursor": True,
                    "cursor_agent_id": "bc-fallback",
                },
            )

        with (
            mock.patch.object(
                server,
                "_create_openclaw_job",
                return_value={
                    "ok": True,
                    "summary": "OpenClaw reviewed the plan.",
                    "backend": "openclaw",
                    "execution_lane": "openclaw_hybrid",
                    "delegated_to_cursor": False,
                },
            ),
            mock.patch.object(server, "_run_cursor_job", side_effect=fake_cursor_run),
        ):
            server._run_delegated_job(result["task_id"])
        proj = project_task_dict(server.conn, result["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.COMPLETED.value)
        self.assertTrue(proj["meta"]["execution"]["delegated_to_cursor"])
        self.assertEqual(proj["cursor_agent_id"], "bc-fallback")

    def test_server_followups_memory_question_uses_prior_chat_history(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        prior = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-memory-prior",
                "payload": {
                    "text": "Let's finish the reboot startup work.",
                    "chat_id": 77,
                    "message_id": 11,
                },
            },
        )
        append_event(
            server.conn,
            prior["task_id"],
            EventType.ASSISTANT_REPLIED,
            {
                "text": "We were working on reboot startup and Telegram memory.",
                "route": "direct",
                "reason": "history",
            },
        )
        current = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-memory-current",
                "payload": {
                    "text": "Hi do you remember before?",
                    "chat_id": 77,
                    "message_id": 12,
                },
            },
        )
        server._handle_task_followups(current["task_id"])
        proj = project_task_dict(server.conn, current["task_id"], "telegram")
        self.assertEqual(proj["status"], TaskStatus.COMPLETED.value)
        self.assertIn("remember the recent conversation", proj["meta"]["assistant"]["last_reply"].lower())
        self.assertIn("reboot startup", proj["meta"]["assistant"]["last_reply"].lower())
        self.assertEqual(proj["summary"], proj["meta"]["assistant"]["last_reply"][:500])

    def test_server_routing_retry_after_append_failure(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-retry",
                "payload": {
                    "text": "Please inspect the repo and fix the failing tests.",
                    "chat_id": 1,
                    "message_id": 4,
                },
            },
        )
        task_id = result["task_id"]
        original_append = server._append_task_event
        attempts = {"count": 0}

        def flaky_append(*args, **kwargs):
            if attempts["count"] == 0:
                attempts["count"] += 1
                raise RuntimeError("append failed")
            return original_append(*args, **kwargs)

        with mock.patch.object(server, "_append_task_event", side_effect=flaky_append):
            with self.assertRaises(RuntimeError):
                server._handle_task_followups(task_id)
        server._handle_task_followups(task_id)
        proj = project_task_dict(server.conn, task_id, "telegram")
        self.assertEqual(proj["status"], TaskStatus.QUEUED.value)
        self.assertEqual(proj["meta"]["cursor"]["kind"], "openclaw")

    def test_server_cursor_poll_timeout_stays_running(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        result = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-long-run",
                "payload": {
                    "text": "Please inspect the repo and fix the failing tests.",
                    "chat_id": 1,
                    "message_id": 5,
                    "auto_cursor_job": True,
                },
            },
        )
        task_id = result["task_id"]
        server.cursor_status_poll_attempts = 2
        server.cursor_status_poll_interval = 0.0
        with (
            mock.patch.object(
                server,
                "_create_cursor_job",
                return_value={
                    "agent_id": "bc-demo",
                    "agent_url": "https://cursor.com/agents/demo",
                    "status": "SUBMITTED",
                    "backend": "api",
                },
            ),
            mock.patch.object(
                server,
                "_cursor_agent_status",
                side_effect=[
                    {
                        "response": {
                            "status": "RUNNING",
                            "target": {"url": "https://cursor.com/agents/demo"},
                        }
                    },
                    {
                        "response": {
                            "status": "RUNNING",
                            "target": {"url": "https://cursor.com/agents/demo"},
                        }
                    },
                ],
            ),
        ):
            server._run_cursor_job(task_id)
        proj = project_task_dict(server.conn, task_id, "telegram")
        self.assertEqual(proj["status"], TaskStatus.RUNNING.value)
        self.assertIsNone(proj["last_error"])
        events = load_events_for_task(server.conn, task_id)
        self.assertEqual(events[-1][2], EventType.JOB_PROGRESS.value)

    def test_server_cursor_plan_first_two_pass_when_enabled(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["ANDREA_TELEGRAM_CURSOR_PLAN_FIRST"] = "1"
        os.environ["ANDREA_TELEGRAM_CURSOR_PLANNER_MODEL"] = "planner-model"
        os.environ["ANDREA_TELEGRAM_CURSOR_EXECUTOR_MODEL"] = "executor-model"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        try:
            server = SyncServer()
            result = handle_command(
                server.conn,
                {
                    "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                    "channel": "telegram",
                    "external_id": "tg-plan-first",
                    "payload": {
                        "text": "Please refactor the sync server for clarity.",
                        "chat_id": 1,
                        "message_id": 50,
                        "auto_cursor_job": True,
                    },
                },
            )
            task_id = result["task_id"]
            server.cursor_status_poll_attempts = 1
            server.cursor_status_poll_interval = 0.0
            calls: list[tuple[str, dict[str, object]]] = []

            def create_side_effect(prompt: str, **kwargs: object) -> dict[str, object]:
                calls.append((prompt, dict(kwargs)))
                if len(calls) == 1:
                    return {
                        "agent_id": "planner-agent",
                        "backend": "api",
                        "status": "SUBMITTED",
                    }
                return {
                    "agent_id": "exec-agent",
                    "backend": "api",
                    "status": "FINISHED",
                }

            conv_payload = {
                "messages": [
                    {
                        "type": "assistant_message",
                        "text": (
                            "## CursorExecutionPlan\n\n"
                            "1. Open services/andrea_sync/server.py\n"
                            "2. Add comments\n"
                            "3. Run unit tests\n"
                        ),
                    }
                ]
            }

            with (
                mock.patch.object(server, "_create_cursor_job", side_effect=create_side_effect),
                mock.patch.object(
                    server,
                    "_poll_cursor_agent_terminal",
                    return_value=("FINISHED", {}, "https://cursor.example/a", ""),
                ),
                mock.patch.object(
                    server,
                    "_cursor_agent_conversation",
                    return_value={"response": conv_payload},
                ),
                mock.patch.object(
                    server,
                    "_cursor_terminal_summary",
                    return_value="summary text",
                ),
            ):
                server._run_cursor_job(task_id)
            self.assertEqual(len(calls), 2)
            self.assertTrue(calls[0][1].get("read_only"))
            self.assertEqual(calls[0][1].get("model"), "planner-model")
            self.assertIsNone(calls[1][1].get("read_only"))
            self.assertEqual(calls[1][1].get("model"), "executor-model")
            self.assertIn("Cursor planner output", calls[1][0])
            proj = project_task_dict(server.conn, task_id, "telegram")
            self.assertEqual(proj["meta"]["cursor"].get("cursor_strategy"), "plan_first")
        finally:
            os.environ.pop("ANDREA_TELEGRAM_CURSOR_PLAN_FIRST", None)
            os.environ.pop("ANDREA_TELEGRAM_CURSOR_PLANNER_MODEL", None)
            os.environ.pop("ANDREA_TELEGRAM_CURSOR_EXECUTOR_MODEL", None)

    def test_cursor_report_rejects_non_object_payload(self) -> None:
        created = handle_command(
            self.conn,
            {
                "command_type": CommandType.CREATE_CURSOR_JOB.value,
                "channel": "telegram",
                "external_id": "cursor-report-payload",
                "payload": {"prompt": "fix bug", "summary": "fix"},
            },
        )
        self.assertTrue(created.get("ok"))
        result = handle_command(
            self.conn,
            {
                "command_type": CommandType.REPORT_CURSOR_EVENT.value,
                "channel": "cursor",
                "task_id": created["task_id"],
                "payload": {
                    "event_type": EventType.JOB_STARTED.value,
                    "payload": ["not", "an", "object"],
                },
            },
        )
        self.assertFalse(result.get("ok"))
        self.assertIn("json object", str(result.get("error", "")).lower())

    def test_load_events_for_task_skips_malformed_seq_or_ts_rows(self) -> None:
        fake_conn = mock.Mock()
        fake_conn.execute.return_value.fetchall.return_value = [
            {
                "seq": 1,
                "ts": 123.0,
                "event_type": EventType.JOB_PROGRESS.value,
                "payload_json": "{}",
            },
            {
                "seq": 2,
                "ts": None,
                "event_type": EventType.JOB_PROGRESS.value,
                "payload_json": "{}",
            },
        ]
        events = load_events_for_task(fake_conn, "tsk_demo")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][2], EventType.JOB_PROGRESS.value)

    def test_alexa_parse_intent(self) -> None:
        body = {
            "session": {"sessionId": "s1"},
            "request": {
                "type": "IntentRequest",
                "requestId": "r1",
                "intent": {
                    "name": "AndreaCaptureIntent",
                    "slots": {
                        "utterance": {"value": "remind me to call dad"},
                    },
                },
            },
        }
        cmd, resp = alexa_adapt.parse_alexa_body(body)
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd["command_type"], "AlexaUtterance")
        self.assertIn("let me work on that", resp["response"]["outputSpeech"]["text"].lower())
        self.assertEqual(cmd["payload"]["request_id"], "r1")
        self.assertEqual(cmd["payload"]["intent_name"], "AndreaCaptureIntent")

    def test_alexa_voice_safe_text_clips_urls_and_markdown(self) -> None:
        text = alexa_adapt.voice_safe_text("See `this` PR: https://example.com/foo")
        self.assertNotIn("http", text)
        self.assertNotIn("`", text)
        self.assertIn("pull request", text.lower())

    def test_alexa_delegated_ack_omits_telegram_when_summary_disabled(self) -> None:
        response = alexa_adapt.build_ack_response(
            "do the work",
            delegated=True,
            telegram_summary_expected=False,
        )
        speech = response["response"]["outputSpeech"]["text"].lower()
        self.assertIn("background", speech)
        self.assertNotIn("telegram", speech)

    def test_alexa_process_direct_request_returns_spoken_reply(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        response = server._process_alexa_request(
            {
                "session": {"sessionId": "alexa-session-1"},
                "request": {
                    "type": "IntentRequest",
                    "requestId": "alexa-req-1",
                    "intent": {
                        "name": "AndreaCaptureIntent",
                        "slots": {"utterance": {"value": "how are you today"}},
                    },
                },
            }
        )
        self.assertTrue(response["response"]["shouldEndSession"])
        speech = response["response"]["outputSpeech"]["text"].lower()
        self.assertTrue("thanks" in speech or "good" in speech or "well" in speech)
        self.assertNotIn("cursor", speech)

    def test_alexa_process_delegate_request_returns_short_ack(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        response = server._process_alexa_request(
            {
                "session": {"sessionId": "alexa-session-2"},
                "request": {
                    "type": "IntentRequest",
                    "requestId": "alexa-req-2",
                    "intent": {
                        "name": "AndreaCaptureIntent",
                        "slots": {
                            "utterance": {
                                "value": "please inspect the repo and fix the failing tests"
                            }
                        },
                    },
                },
            }
        )
        self.assertTrue(response["response"]["shouldEndSession"])
        speech = response["response"]["outputSpeech"]["text"].lower()
        self.assertIn("background", speech)
        self.assertNotIn("telegram", speech)
        tasks = list_tasks(server.conn, limit=5)
        self.assertTrue(any(task["channel"] == "alexa" for task in tasks))

    def test_alexa_process_delegate_request_without_summary_uses_background_ack(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["ANDREA_SYNC_ALEXA_SUMMARY_TO_TELEGRAM"] = "0"
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        os.environ.pop("ANDREA_SYNC_ALEXA_SUMMARY_CHAT_ID", None)
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        response = server._process_alexa_request(
            {
                "session": {"sessionId": "alexa-session-3"},
                "request": {
                    "type": "IntentRequest",
                    "requestId": "alexa-req-3",
                    "intent": {
                        "name": "AndreaCaptureIntent",
                        "slots": {
                            "utterance": {
                                "value": "please inspect the repo and fix the failing tests"
                            }
                        },
                    },
                },
            }
        )
        speech = response["response"]["outputSpeech"]["text"].lower()
        self.assertIn("background", speech)
        self.assertNotIn("telegram", speech)

    def test_alexa_completed_task_sends_single_telegram_summary(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "1"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
        os.environ["TELEGRAM_CHAT_ID"] = "777"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        created = handle_command(
            server.conn,
            {
                "command_type": CommandType.ALEXA_UTTERANCE.value,
                "channel": "alexa",
                "external_id": "alexa-summary-1",
                "payload": {
                    "utterance": "remind me to stretch later today",
                    "routing_text": "remind me to stretch later today",
                    "session_id": "sess-1",
                },
            },
        )
        append_event(
            server.conn,
            created["task_id"],
            EventType.JOB_COMPLETED,
            {
                "summary": "OpenClaw completed the delegated task.",
                "backend": "openclaw",
                "runner": "openclaw",
            },
        )
        snapshot = server._task_snapshot(created["task_id"])
        assert snapshot is not None
        with mock.patch.object(tg_adapt, "send_text_message") as send_mock:
            server._handle_alexa_followups(created["task_id"], snapshot)
            server._handle_alexa_followups(created["task_id"], snapshot)
        send_mock.assert_called_once()
        sent_text = send_mock.call_args.kwargs["text"]
        self.assertIn("Alexa session summary", sent_text)
        self.assertIn("remind me to stretch later today", sent_text)

    def test_alexa_direct_summary_uses_andrea_reply_not_cursor_label(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "1"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
        os.environ["TELEGRAM_CHAT_ID"] = "777"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        created = handle_command(
            server.conn,
            {
                "command_type": CommandType.ALEXA_UTTERANCE.value,
                "channel": "alexa",
                "external_id": "alexa-summary-direct-1",
                "payload": {
                    "utterance": "how are you today",
                    "routing_text": "how are you today",
                    "session_id": "sess-direct-1",
                },
            },
        )
        append_event(
            server.conn,
            created["task_id"],
            EventType.ASSISTANT_REPLIED,
            {
                "text": "I am doing well and ready to help.",
                "route": "direct",
                "reason": "small_talk",
            },
        )
        snapshot = server._task_snapshot(created["task_id"])
        assert snapshot is not None
        with mock.patch.object(tg_adapt, "send_text_message") as send_mock:
            server._handle_alexa_followups(created["task_id"], snapshot)
        send_mock.assert_called_once()
        sent_text = send_mock.call_args.kwargs["text"]
        self.assertIn("Handled by: Andrea directly", sent_text)
        self.assertIn("I am doing well and ready to help.", sent_text)
        self.assertNotIn("Handled by: Cursor", sent_text)

    def test_alexa_created_task_from_commands_routes_direct_reply(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        created = handle_command(
            server.conn,
            {
                "command_type": CommandType.ALEXA_UTTERANCE.value,
                "channel": "alexa",
                "external_id": "alexa-command-direct-1",
                "payload": {
                    "utterance": "how are you today",
                    "routing_text": "how are you today",
                    "session_id": "sess-command-direct-1",
                },
            },
        )
        snapshot = server._task_snapshot(created["task_id"])
        assert snapshot is not None
        server._handle_alexa_followups(created["task_id"], snapshot)
        final_snapshot = server._task_snapshot(created["task_id"])
        assert final_snapshot is not None
        self.assertEqual(final_snapshot["projection"]["status"], "completed")
        self.assertEqual(final_snapshot["projection"]["meta"]["assistant"]["route"], "direct")

    def test_alexa_schedule_execution_ignores_telegram_auto_cursor_flag(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "1"
        os.environ["ANDREA_SYNC_DELEGATED_EXECUTION_ENABLED"] = "1"
        os.environ["ANDREA_SYNC_TELEGRAM_AUTO_CURSOR"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        created = handle_command(
            server.conn,
            {
                "command_type": CommandType.ALEXA_UTTERANCE.value,
                "channel": "alexa",
                "external_id": "alexa-command-delegate-1",
                "payload": {
                    "utterance": "please inspect the repo and fix the failing tests",
                    "routing_text": "please inspect the repo and fix the failing tests",
                    "session_id": "sess-command-delegate-1",
                },
            },
        )
        append_event(
            server.conn,
            created["task_id"],
            EventType.JOB_QUEUED,
            {
                "kind": "openclaw",
                "execution_lane": "openclaw_hybrid",
                "runner": "openclaw",
            },
        )
        with mock.patch("services.andrea_sync.server.threading.Thread") as thread_cls:
            thread_instance = mock.Mock()
            thread_cls.return_value = thread_instance
            server._schedule_cursor_execution(created["task_id"])
        thread_cls.assert_called_once()
        thread_instance.start.assert_called_once()

    def test_run_delegated_job_clears_executor_started_marker(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        marker = server._meta_key("executor_started", "tsk_demo")
        set_meta(server.conn, marker, "123.0")
        with mock.patch.object(server, "_task_execution_lane", return_value="direct_cursor"):
            with mock.patch.object(server, "_run_cursor_job") as run_cursor:
                server._run_delegated_job("tsk_demo")
        run_cursor.assert_called_once_with("tsk_demo")
        self.assertIsNone(get_meta(server.conn, marker))

    def test_telegram_followups_prefer_openclaw_user_summary_when_summary_is_generic(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "1"
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-test-token"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        created = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-openclaw-generic-summary",
                "payload": {
                    "text": "please do the heavier backend work",
                    "chat_id": 10001,
                    "message_id": 10,
                    "from_user": 7,
                },
            },
        )
        append_event(
            server.conn,
            created["task_id"],
            EventType.JOB_STARTED,
            {"backend": "openclaw", "runner": "openclaw", "execution_lane": "openclaw_hybrid"},
        )
        append_event(
            server.conn,
            created["task_id"],
            EventType.JOB_COMPLETED,
            {
                "summary": "OpenClaw completed the delegated task.",
                "user_summary": "Implemented the actual fix and updated the docs with the final behavior.",
                "backend": "openclaw",
                "runner": "openclaw",
                "raw_text": (
                    "sessions_spawn.attachments.enabled is disabled.\n"
                    "Implemented the actual fix and updated the docs with the final behavior."
                ),
            },
        )
        snapshot = server._task_snapshot(created["task_id"])
        assert snapshot is not None
        with mock.patch.object(tg_adapt, "send_text_message") as send_mock:
            server._handle_telegram_followups(created["task_id"], snapshot)
        sent_text = send_mock.call_args.kwargs["text"]
        self.assertIn("Implemented the actual fix and updated the docs", sent_text)
        self.assertNotIn("sessions_spawn.attachments.enabled", sent_text)

    def test_telegram_followups_skip_late_chunk_notice_for_continuation(self) -> None:
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "1"
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-test-token"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        from services.andrea_sync.server import SyncServer  # noqa: E402

        server = SyncServer()
        created = handle_command(
            server.conn,
            {
                "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
                "channel": "telegram",
                "external_id": "tg-continuation-running",
                "payload": {
                    "text": "first chunk",
                    "chat_id": 10002,
                    "message_id": 1,
                    "from_user": 7,
                },
            },
        )
        append_event(
            server.conn,
            created["task_id"],
            EventType.JOB_STARTED,
            {"backend": "openclaw", "runner": "openclaw", "execution_lane": "openclaw_hybrid"},
        )
        append_event(
            server.conn,
            created["task_id"],
            EventType.USER_MESSAGE,
            {
                "text": "second chunk",
                "routing_text": "second chunk",
                "chat_id": 10002,
                "message_id": 2,
                "from_user": 7,
                "telegram_continuation": True,
            },
        )
        snapshot = server._task_snapshot(created["task_id"])
        assert snapshot is not None
        with mock.patch.object(tg_adapt, "send_text_message") as send_mock:
            server._handle_telegram_followups(created["task_id"], snapshot)
        sent_texts = [call.kwargs["text"] for call in send_mock.call_args_list]
        self.assertTrue(any("folded this into the current heavy-lift task" in text for text in sent_texts))
        self.assertFalse(any("may not include it" in text for text in sent_texts))

    def test_publish_capability_requires_internal_channel(self) -> None:
        r = handle_command(
            self.conn,
            {
                "command_type": "PublishCapabilitySnapshot",
                "channel": "cli",
                "payload": {"rows": [], "summary": {}},
            },
        )
        self.assertFalse(r.get("ok"))

    def test_publish_capability_internal_and_verify_before_deny(self) -> None:
        r = handle_command(
            self.conn,
            {
                "command_type": "PublishCapabilitySnapshot",
                "channel": "internal",
                "payload": {
                    "rows": [{"id": "skill:telegram", "status": "ready"}],
                    "summary": {},
                },
            },
        )
        self.assertTrue(r.get("ok"))
        ev = evaluate_skill_absence_claim(self.conn, "telegram", max_age_seconds=900.0)
        self.assertFalse(ev.get("may_claim_absent"))

    def test_kill_switch_blocks_then_release(self) -> None:
        e = handle_command(
            self.conn,
            {
                "command_type": "KillSwitchEngage",
                "channel": "internal",
                "payload": {"reason": "test"},
            },
        )
        self.assertTrue(e.get("ok"))
        blocked = handle_command(
            self.conn,
            {
                "command_type": "CreateTask",
                "channel": "cli",
                "external_id": "ks1",
                "payload": {"summary": "nope"},
            },
        )
        self.assertFalse(blocked.get("ok"))
        rel = handle_command(
            self.conn,
            {
                "command_type": "KillSwitchRelease",
                "channel": "internal",
                "payload": {},
            },
        )
        self.assertTrue(rel.get("ok"))
        ok = handle_command(
            self.conn,
            {
                "command_type": "CreateTask",
                "channel": "cli",
                "external_id": "ks2",
                "payload": {"summary": "yes"},
            },
        )
        self.assertTrue(ok.get("ok"))

    def test_sanitize_user_surface_text_rejects_stale_fallback(self) -> None:
        from services.andrea_sync.user_surface import sanitize_user_surface_text

        out = sanitize_user_surface_text(
            "sessions_spawn.attachments.enabled",
            fallback="OpenClaw embedding quota exceeded for this workspace.",
            limit=200,
        )
        self.assertEqual(out, "")

    def test_build_direct_reply_fail_closed_on_contaminated_contextual_fallback(self) -> None:
        """OpenAI path failed: contaminated contextual fallback must not reach the user."""
        from services.andrea_sync import andrea_router

        with mock.patch.dict(os.environ, {"OPENAI_API_ENABLED": "0"}, clear=False):
            with mock.patch.object(
                andrea_router,
                "_openai_direct_reply",
                side_effect=RuntimeError("openai_direct_disabled"),
            ):
                with mock.patch.object(
                    andrea_router,
                    "_contextual_fallback",
                    return_value="sessions_spawn.attachments.enabled is on",
                ):
                    reply = andrea_router.build_direct_reply(
                        "What should I know about the timeline for the kitchen project?",
                        history=[],
                        memory_notes=[],
                    )
        self.assertNotIn("sessions_spawn", reply.lower())
        self.assertNotIn("attachments.enabled", reply.lower())

    def test_build_direct_reply_rejects_openai_sessions_spawn_leak(self) -> None:
        from services.andrea_sync.andrea_router import build_direct_reply

        mock_resp = mock.Mock()
        mock_resp.__enter__ = mock.Mock(return_value=mock_resp)
        mock_resp.__exit__ = mock.Mock(return_value=False)
        mock_resp.read.return_value = json.dumps(
            {
                "choices": [
                    {"message": {"content": "sessions_spawn.attachments.enabled is true"}}
                ],
            }
        ).encode()

        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "test-key", "OPENAI_API_ENABLED": "1"},
            clear=False,
        ):
            with mock.patch(
                "services.andrea_sync.andrea_router.urllib.request.urlopen",
                return_value=mock_resp,
            ):
                reply = build_direct_reply(
                    "What should I know about the timeline for the kitchen project?",
                    history=[],
                    memory_notes=[],
                )
        self.assertNotIn("sessions_spawn", reply.lower())
        self.assertNotIn("attachments.enabled", reply.lower())

    def test_expand_recent_text_shorthand_requires_prior_structured_reason(self) -> None:
        from services.andrea_sync.server import SyncServer

        server = SyncServer()
        with mock.patch.object(server, "_last_assistant_reply_reason", return_value=""):
            self.assertIsNone(server._expand_recent_text_messages_shorthand("task-x", "from today?"))
        with mock.patch.object(
            server,
            "_last_assistant_reply_reason",
            return_value="recent_text_messages_ready",
        ):
            expanded = server._expand_recent_text_messages_shorthand("task-x", "from today?")
        self.assertIsNotNone(expanded)
        self.assertIn("today", str(expanded).lower())


if __name__ == "__main__":
    unittest.main()

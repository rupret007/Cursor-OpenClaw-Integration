"""Unit tests for Andrea lockstep (no live HTTP)."""

from __future__ import annotations

import os
import sys
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
    append_event,
    connect,
    load_events_for_task,
    load_recent_telegram_history,
    migrate,
)
from services.andrea_sync.andrea_router import route_message  # noqa: E402
from services.andrea_sync.telegram_format import (  # noqa: E402
    format_ack_message,
    format_direct_message,
    format_final_message,
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
        self._prev_openai_enabled = os.environ.get("OPENAI_API_ENABLED")
        self._prev_openai_key = os.environ.get("OPENAI_API_KEY")
        os.environ["ANDREA_SYNC_DB"] = str(self.db_path)
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
        if self._prev_openai_enabled is None:
            os.environ.pop("OPENAI_API_ENABLED", None)
        else:
            os.environ["OPENAI_API_ENABLED"] = self._prev_openai_enabled
        if self._prev_openai_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._prev_openai_key
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

    def test_router_greeting_stays_direct(self) -> None:
        decision = route_message("hi andrea how are you?")
        self.assertEqual(decision.mode, "direct")
        self.assertIn("ready to help", decision.reply_text.lower())

    def test_router_coding_request_delegates(self) -> None:
        decision = route_message("Please inspect the repo, fix the failing tests, and open a PR.")
        self.assertEqual(decision.mode, "delegate")

    def test_router_help_with_technical_request_delegates(self) -> None:
        decision = route_message("Help me debug this traceback and fix the failing tests.")
        self.assertEqual(decision.mode, "delegate")

    def test_router_meta_cursor_question_stays_direct(self) -> None:
        decision = route_message("Can you talk to Cursor when needed?")
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

    def test_direct_message_format_is_short(self) -> None:
        text = format_direct_message("Hi! I'm doing well and ready to help.")
        self.assertEqual(text, "Andrea:\nHi! I'm doing well and ready to help.")

    def test_telegram_ack_message_format(self) -> None:
        text = format_ack_message("tsk_demo")
        self.assertIn("Andrea:", text)
        self.assertIn("What happened:", text)
        self.assertIn("Technical details:", text)
        self.assertIn("Task: tsk_demo", text)
        self.assertIn("Status: queued", text)

    def test_telegram_running_message_format(self) -> None:
        text = format_running_message("tsk_demo", agent_url="https://cursor.com/agents/demo")
        self.assertIn("Andrea:", text)
        self.assertIn("Cursor is actively working", text)
        self.assertIn("Technical details:", text)
        self.assertIn("Agent: https://cursor.com/agents/demo", text)

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
        self.assertIn("Cursor said:", text)
        self.assertIn("Technical details:", text)
        self.assertIn("PR: https://github.com/example/repo/pull/1", text)
        self.assertNotIn("### What changed", text)

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
        self.assertEqual(proj["meta"]["cursor"]["kind"], "cursor")

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
        self.assertEqual(proj["meta"]["cursor"]["kind"], "cursor")

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
        self.assertIn("Captured", resp["response"]["outputSpeech"]["text"])

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


if __name__ == "__main__":
    unittest.main()

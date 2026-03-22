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
    append_event,
    connect,
    get_meta,
    list_tasks,
    list_telegram_task_ids_for_chat,
    load_events_for_task,
    load_recent_telegram_history,
    migrate,
    set_meta,
)
from services.andrea_sync.andrea_router import route_message  # noqa: E402
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
        self.assertNotIn("message_thread_id", cmd["payload"])

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

    def test_telegram_extract_routing_hints_detects_full_dialogue_mode(self) -> None:
        routing = tg_adapt.extract_routing_hints(
            "@Andrea @Cursor work together and show the full dialogue while you do it"
        )
        self.assertEqual(routing["routing_hint"], "collaborate")
        self.assertEqual(routing["collaboration_mode"], "collaborative")
        self.assertEqual(routing["visibility_mode"], "full")

    def test_telegram_extract_routing_hints_detects_direct_model_lane(self) -> None:
        routing = tg_adapt.extract_routing_hints("@Gemini review this plan with Andrea")
        self.assertEqual(routing["preferred_model_family"], "gemini")
        self.assertEqual(routing["preferred_model_label"], "Gemini")
        self.assertEqual(routing["model_mentions"], ["gemini"])
        self.assertEqual(routing["routing_text"], "review this plan with Andrea")

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
        self.assertIn("ready to help", decision.reply_text.lower())

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

    def test_router_model_mention_forces_openclaw_delegate(self) -> None:
        decision = route_message(
            "how are you?",
            preferred_model_family="gemini",
        )
        self.assertEqual(decision.mode, "delegate")
        self.assertEqual(decision.reason, "explicit_model_mention")
        self.assertEqual(decision.delegate_target, "openclaw_hybrid")

    def test_router_collaboration_phrase_requests_joint_work(self) -> None:
        decision = route_message("Please work together and double-check the repo changes.")
        self.assertEqual(decision.mode, "delegate")
        self.assertEqual(decision.collaboration_mode, "collaborative")

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
        self.assertIn("addressed Cursor directly", text)

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
        self.assertIn("Cursor said:", text)
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
        self.assertIn("OpenClaw said:", text)
        self.assertIn("OpenClaw session: sess_demo", text)

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
        self.assertIn("OpenClaw coordinator (google / gemini-2.5-flash) said:", text)

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
        self.assertIn("addressed Cursor directly", text)

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
        self.assertEqual(proj["meta"]["cursor"]["kind"], "openclaw")
        self.assertEqual(proj["meta"]["execution"]["lane"], "openclaw_hybrid")

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
                    "text": "@Andrea remind me to drink water",
                    "routing_text": "remind me to drink water",
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
                    "text": "Remind me to review the StoryLiner repo tomorrow morning.",
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
        with mock.patch.object(
            server,
            "_create_openclaw_job",
            return_value={
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
            },
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
        self.assertIn("ready to help", response["response"]["outputSpeech"]["text"].lower())

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

    def test_telegram_followups_prefer_openclaw_raw_text_when_summary_is_generic(self) -> None:
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
                "backend": "openclaw",
                "runner": "openclaw",
                "raw_text": "Implemented the actual fix and updated the docs with the final behavior.",
            },
        )
        snapshot = server._task_snapshot(created["task_id"])
        assert snapshot is not None
        with mock.patch.object(tg_adapt, "send_text_message") as send_mock:
            server._handle_telegram_followups(created["task_id"], snapshot)
        sent_text = send_mock.call_args.kwargs["text"]
        self.assertIn("Implemented the actual fix and updated the docs", sent_text)

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
        self.assertTrue(any("Merged with your current task" in text for text in sent_texts))
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


if __name__ == "__main__":
    unittest.main()

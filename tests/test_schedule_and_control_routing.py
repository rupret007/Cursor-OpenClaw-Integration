from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.andrea_sync.adapters import telegram as tg_adapt
from services.andrea_sync.backends.cursor_control import CursorControlItemResult, CursorControlResult
from services.andrea_sync.bus import handle_command
from services.andrea_sync.server import SyncServer
from services.andrea_sync.store import connect, migrate
from services.andrea_sync.telegram_continuation import attach_continuation_if_applicable


class TestScheduleAndControlRouting(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = Path(self._tmp.name)
        self._prev_db = os.environ.get("ANDREA_SYNC_DB")
        self._prev_background = os.environ.get("ANDREA_SYNC_BACKGROUND_ENABLED")
        self._prev_notifier = os.environ.get("ANDREA_SYNC_TELEGRAM_NOTIFIER")
        self._prev_public_base = os.environ.get("ANDREA_SYNC_PUBLIC_BASE")
        self._prev_webhook = os.environ.get("ANDREA_SYNC_TELEGRAM_WEBHOOK_AUTOFIX")
        self._prev_calendar = os.environ.get("ANDREA_CALENDAR_EVENTS_JSON")
        os.environ["ANDREA_SYNC_DB"] = str(self.db_path)
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["ANDREA_SYNC_PUBLIC_BASE"] = ""
        os.environ["ANDREA_SYNC_TELEGRAM_WEBHOOK_AUTOFIX"] = "0"
        self.conn = connect(self.db_path)
        migrate(self.conn)
        self.server = SyncServer()

    def tearDown(self) -> None:
        self.server.shutdown_queue_worker()
        self.server.conn.close()
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
        if self._prev_public_base is None:
            os.environ.pop("ANDREA_SYNC_PUBLIC_BASE", None)
        else:
            os.environ["ANDREA_SYNC_PUBLIC_BASE"] = self._prev_public_base
        if self._prev_webhook is None:
            os.environ.pop("ANDREA_SYNC_TELEGRAM_WEBHOOK_AUTOFIX", None)
        else:
            os.environ["ANDREA_SYNC_TELEGRAM_WEBHOOK_AUTOFIX"] = self._prev_webhook
        if self._prev_calendar is None:
            os.environ.pop("ANDREA_CALENDAR_EVENTS_JSON", None)
        else:
            os.environ["ANDREA_CALENDAR_EVENTS_JSON"] = self._prev_calendar
        self.db_path.unlink(missing_ok=True)
        Path(str(self.db_path) + "-wal").unlink(missing_ok=True)
        Path(str(self.db_path) + "-shm").unlink(missing_ok=True)

    def _telegram_submit(self, update_id: int, text: str) -> str:
        cmd = tg_adapt.update_to_command(
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "text": text,
                    "chat": {"id": 555, "type": "private"},
                    "from": {"id": 999, "username": "tester"},
                },
            }
        )
        assert cmd is not None
        result = handle_command(self.server.conn, cmd)
        self.assertTrue(result.get("ok"), result)
        return str(result["task_id"])

    def test_openclaw_schedule_query_routes_direct_with_schedule_answer(self) -> None:
        os.environ["ANDREA_CALENDAR_EVENTS_JSON"] = json.dumps(
            [{"title": "Design review", "start": "2026-03-27T14:00:00-05:00"}]
        )
        task_id = self._telegram_submit(2001, "Ask @openclaw what's on my schedule today")
        decision, applied = self.server._route_task_with_decision(
            task_id,
            history=[],
            source="test",
        )
        self.assertTrue(applied)
        assert decision is not None
        self.assertEqual(decision.mode, "direct")
        self.assertIn("Here’s what you have today:", decision.reply_text)
        self.assertIn("Design review", decision.reply_text)

    @mock.patch("services.andrea_sync.server.cancel_all_jobs")
    def test_cursor_cancel_all_jobs_routes_direct_control_reply(
        self, cancel_mock: mock.MagicMock
    ) -> None:
        cancel_mock.return_value = CursorControlResult(
            action="cancel_jobs",
            requested_count=2,
            canceled_count=1,
            terminal_already_count=1,
            results=(
                CursorControlItemResult(id="job_1", status="canceled"),
                CursorControlItemResult(id="job_2", status="already_finished"),
            ),
        )
        task_id = self._telegram_submit(2002, "Ask @cursor to cancel all jobs")
        decision, applied = self.server._route_task_with_decision(
            task_id,
            history=[],
            source="test",
        )
        self.assertTrue(applied)
        assert decision is not None
        self.assertEqual(decision.mode, "direct")
        self.assertIn("Canceled 1 Cursor job(s).", decision.reply_text)
        self.assertIn("job_1: canceled", decision.reply_text)
        self.assertIn("job_2: already_finished", decision.reply_text)

    @mock.patch("services.andrea_sync.server.cancel_all_jobs")
    def test_multi_intent_cancel_and_schedule_combines_clean_reply(
        self, cancel_mock: mock.MagicMock
    ) -> None:
        cancel_mock.return_value = CursorControlResult(
            action="cancel_jobs",
            requested_count=1,
            canceled_count=1,
            results=(CursorControlItemResult(id="job_1", status="canceled"),),
        )
        os.environ["ANDREA_CALENDAR_EVENTS_JSON"] = json.dumps(
            [{"title": "1:1", "start": "2026-03-27T10:00:00-05:00"}]
        )
        task_id = self._telegram_submit(
            2003,
            "Cancel all jobs and tell me what's on my schedule today",
        )
        decision, applied = self.server._route_task_with_decision(
            task_id,
            history=[],
            source="test",
        )
        self.assertTrue(applied)
        assert decision is not None
        self.assertEqual(decision.mode, "direct")
        self.assertIn("Canceled 1 Cursor job(s).", decision.reply_text)
        self.assertIn("Here’s what you have today:", decision.reply_text)
        self.assertIn("1:1", decision.reply_text)

    def test_schedule_query_does_not_attach_to_existing_active_task(self) -> None:
        first_cmd = {
            "command_type": "SubmitUserMessage",
            "channel": "telegram",
            "external_id": "tg-active-1",
            "payload": {
                "text": "@Cursor please fix the failing tests",
                "routing_text": "@Cursor please fix the failing tests",
                "chat_id": 555,
                "message_id": 1,
                "from_user": 999,
                "mention_targets": ["cursor"],
                "requested_capability": "cursor_execution",
            },
        }
        first = handle_command(self.server.conn, first_cmd)
        self.assertTrue(first.get("ok"), first)
        cmd = {
            "command_type": "SubmitUserMessage",
            "channel": "telegram",
            "external_id": "tg-active-2",
            "payload": {
                "text": "Ask OpenClaw what's on my schedule today.",
                "routing_text": "Ask OpenClaw what's on my schedule today.",
                "chat_id": 555,
                "message_id": 2,
                "from_user": 999,
                "mention_targets": [],
                "requested_capability": "assistant",
            },
        }
        self.assertFalse(attach_continuation_if_applicable(self.server.conn, cmd))


if __name__ == "__main__":
    unittest.main()

"""Unit tests for Andrea lockstep (no live HTTP)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.adapters import alexa as alexa_adapt  # noqa: E402
from services.andrea_sync.adapters import telegram as tg_adapt  # noqa: E402
from services.andrea_sync.bus import handle_command  # noqa: E402
from services.andrea_sync.projector import project_task_dict  # noqa: E402
from services.andrea_sync.schema import CommandType, EventType, TaskStatus, normalize_idempotency_base  # noqa: E402
from services.andrea_sync.store import connect, migrate  # noqa: E402


class TestAndreaSync(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = Path(self._tmp.name)
        self.conn = connect(self.db_path)
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.db_path.unlink(missing_ok=True)
        for suf in ("-wal", "-shm"):
            p = Path(str(self.db_path) + suf)
            p.unlink(missing_ok=True)

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
                    "chat": {"id": 1},
                    "from": {"id": 2},
                },
            }
        )
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd["command_type"], "SubmitUserMessage")
        self.assertEqual(cmd["external_id"], "99")

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


if __name__ == "__main__":
    unittest.main()

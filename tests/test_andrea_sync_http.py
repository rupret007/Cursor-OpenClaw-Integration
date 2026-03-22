"""HTTP smoke tests for Andrea lockstep server (localhost, ephemeral DB)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.telegram_format import format_final_message  # noqa: E402
from services.andrea_sync.bus import handle_command  # noqa: E402
from services.andrea_sync.schema import EventType  # noqa: E402
from services.andrea_sync.store import append_event  # noqa: E402


class TestAndreaSyncHTTP(unittest.TestCase):
    _httpd: ThreadingHTTPServer
    _srv: object
    _port: int
    _thread: threading.Thread
    _dbpath: str

    @classmethod
    def setUpClass(cls) -> None:
        fd, cls._dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        cls._env_backup = {}
        for key in (
            "ANDREA_SYNC_DB",
            "ANDREA_SYNC_TELEGRAM_SECRET",
            "ANDREA_SYNC_INTERNAL_TOKEN",
            "ANDREA_SYNC_BACKGROUND_ENABLED",
            "ANDREA_SYNC_TELEGRAM_NOTIFIER",
            "TELEGRAM_BOT_TOKEN",
        ):
            cls._env_backup[key] = os.environ.get(key)
        os.environ["ANDREA_SYNC_DB"] = cls._dbpath
        os.environ["ANDREA_SYNC_TELEGRAM_SECRET"] = "testhooksecret"
        os.environ["ANDREA_SYNC_INTERNAL_TOKEN"] = "internal-test-token"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["TELEGRAM_BOT_TOKEN"] = ""

        from services.andrea_sync.server import SyncServer, make_handler

        cls._srv = SyncServer()
        cls._httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls._srv))
        cls._port = cls._httpd.server_address[1]
        cls._thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._httpd.shutdown()
        cls._httpd.server_close()
        for key, val in cls._env_backup.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        Path(cls._dbpath).unlink(missing_ok=True)
        Path(cls._dbpath + ".kill").unlink(missing_ok=True)
        for suf in ("-wal", "-shm"):
            Path(cls._dbpath + suf).unlink(missing_ok=True)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._port}{path}"

    def test_health_ok(self) -> None:
        req = urllib.request.Request(self._url("/v1/health"), method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(data.get("ok"))
        self.assertIn("kill_switch", data)
        self.assertIn("capability_digest_age_seconds", data)

    def test_status_ok(self) -> None:
        req = urllib.request.Request(self._url("/v1/status"), method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(data.get("ok"))
        self.assertIn("kill_switch", data)
        self.assertIn("capabilities", data)

    def test_command_create_and_fetch_task(self) -> None:
        body = json.dumps(
            {
                "command_type": "CreateTask",
                "channel": "cli",
                "external_id": "http-test-1",
                "payload": {"summary": "from http test"},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/commands"),
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            r1 = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(r1.get("ok"))
        tid = r1["task_id"]

        req2 = urllib.request.Request(self._url(f"/v1/tasks/{tid}"), method="GET")
        with urllib.request.urlopen(req2, timeout=5) as resp2:
            self.assertEqual(resp2.status, 200)
            detail = json.loads(resp2.read().decode("utf-8"))
        self.assertIn("task", detail)
        self.assertEqual(detail["task"]["task_id"], tid)

    def test_command_validation_400(self) -> None:
        body = json.dumps({"command_type": "NopeNotACommand", "channel": "cli"}).encode(
            "utf-8"
        )
        req = urllib.request.Request(
            self._url("/v1/commands"),
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_internal_events_unauthorized(self) -> None:
        body = json.dumps(
            {
                "task_id": "tsk_missing",
                "event_type": "JobCompleted",
                "payload": {},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/internal/events"),
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_internal_events_rejects_non_object_payload(self) -> None:
        create_body = json.dumps(
            {
                "command_type": "CreateTask",
                "channel": "cli",
                "external_id": "http-internal-payload",
                "payload": {"summary": "for payload validation"},
            }
        ).encode("utf-8")
        create_req = urllib.request.Request(
            self._url("/v1/commands"),
            data=create_body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(create_req, timeout=5) as resp:
            created = json.loads(resp.read().decode("utf-8"))
        body = json.dumps(
            {
                "task_id": created["task_id"],
                "event_type": "JobStarted",
                "payload": ["not", "an", "object"],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/internal/events"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer internal-test-token",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_telegram_webhook_forbidden_without_secret(self) -> None:
        body = json.dumps({"update_id": 1, "message": {"text": "hi"}}).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/telegram/webhook"),
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 403)

    def test_admin_command_requires_internal_auth(self) -> None:
        body = json.dumps(
            {
                "command_type": "KillSwitchEngage",
                "channel": "internal",
                "payload": {"reason": "x"},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/commands"),
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_kill_switch_blocks_commands_http(self) -> None:
        auth = {
            "Authorization": "Bearer internal-test-token",
            "Content-Type": "application/json",
        }
        engage = json.dumps(
            {
                "command_type": "KillSwitchEngage",
                "channel": "internal",
                "payload": {"reason": "t"},
            }
        ).encode("utf-8")
        req_e = urllib.request.Request(
            self._url("/v1/commands"), data=engage, method="POST", headers=auth
        )
        with urllib.request.urlopen(req_e, timeout=5) as r:
            self.assertEqual(r.status, 200)
        create = json.dumps(
            {
                "command_type": "CreateTask",
                "channel": "cli",
                "external_id": "http-ks",
                "payload": {"summary": "blocked"},
            }
        ).encode("utf-8")
        req_c = urllib.request.Request(
            self._url("/v1/commands"), data=create, method="POST", headers=auth
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_c, timeout=5)
        self.assertEqual(ctx.exception.code, 503)
        req_s = urllib.request.Request(self._url("/v1/status"), method="GET")
        with urllib.request.urlopen(req_s, timeout=5) as resp_s:
            self.assertEqual(resp_s.status, 200)
            status_body = json.loads(resp_s.read().decode("utf-8"))
        self.assertTrue(status_body["kill_switch"]["engaged"])
        rel = json.dumps(
            {
                "command_type": "KillSwitchRelease",
                "channel": "internal",
                "payload": {},
            }
        ).encode("utf-8")
        req_r = urllib.request.Request(
            self._url("/v1/commands"), data=rel, method="POST", headers=auth
        )
        with urllib.request.urlopen(req_r, timeout=5) as r:
            self.assertEqual(r.status, 200)
        create_after_release = json.dumps(
            {
                "command_type": "CreateTask",
                "channel": "cli",
                "external_id": "http-ks-released",
                "payload": {"summary": "allowed"},
            }
        ).encode("utf-8")
        req_ok = urllib.request.Request(
            self._url("/v1/commands"),
            data=create_after_release,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req_ok, timeout=5) as resp_ok:
            self.assertEqual(resp_ok.status, 200)
            data_ok = json.loads(resp_ok.read().decode("utf-8"))
        self.assertTrue(data_ok.get("ok"))

    def test_skill_absence_endpoint_after_publish(self) -> None:
        auth = {
            "Authorization": "Bearer internal-test-token",
            "Content-Type": "application/json",
        }
        pub = json.dumps(
            {
                "command_type": "PublishCapabilitySnapshot",
                "channel": "internal",
                "payload": {
                    "rows": [{"id": "skill:telegram", "status": "ready"}],
                    "summary": {},
                },
            }
        ).encode("utf-8")
        req_p = urllib.request.Request(
            self._url("/v1/commands"), data=pub, method="POST", headers=auth
        )
        with urllib.request.urlopen(req_p, timeout=5) as r:
            self.assertEqual(r.status, 200)
        req_g = urllib.request.Request(
            self._url("/v1/policy/skill-absence?skill=telegram"), method="GET"
        )
        with urllib.request.urlopen(req_g, timeout=5) as r:
            self.assertEqual(r.status, 200)
            data = json.loads(r.read().decode("utf-8"))
        self.assertFalse(data.get("may_claim_absent"))


class TestAndreaSyncHTTPWebhookHeader(unittest.TestCase):
    """Webhook auth via X-Telegram-Bot-Api-Secret-Token only (no query secret)."""

    _httpd: ThreadingHTTPServer
    _srv: object
    _port: int
    _thread: threading.Thread
    _dbpath: str

    @classmethod
    def setUpClass(cls) -> None:
        fd, cls._dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        cls._env_backup = {}
        for key in (
            "ANDREA_SYNC_DB",
            "ANDREA_SYNC_TELEGRAM_SECRET",
            "ANDREA_SYNC_TELEGRAM_WEBHOOK_SECRET",
            "ANDREA_SYNC_INTERNAL_TOKEN",
            "ANDREA_SYNC_BACKGROUND_ENABLED",
            "ANDREA_SYNC_TELEGRAM_NOTIFIER",
            "TELEGRAM_BOT_TOKEN",
        ):
            cls._env_backup[key] = os.environ.get(key)
        os.environ["ANDREA_SYNC_DB"] = cls._dbpath
        os.environ["ANDREA_SYNC_TELEGRAM_SECRET"] = ""
        os.environ["ANDREA_SYNC_TELEGRAM_WEBHOOK_SECRET"] = "hdrsecret"
        os.environ["ANDREA_SYNC_INTERNAL_TOKEN"] = "internal-test-token"
        os.environ["ANDREA_SYNC_BACKGROUND_ENABLED"] = "0"
        os.environ["ANDREA_SYNC_TELEGRAM_NOTIFIER"] = "0"
        os.environ["TELEGRAM_BOT_TOKEN"] = ""

        from services.andrea_sync.server import SyncServer, make_handler

        cls._srv = SyncServer()
        cls._httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls._srv))
        cls._port = cls._httpd.server_address[1]
        cls._thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._httpd.shutdown()
        cls._httpd.server_close()
        for key, val in cls._env_backup.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        Path(cls._dbpath).unlink(missing_ok=True)
        Path(cls._dbpath + ".kill").unlink(missing_ok=True)
        for suf in ("-wal", "-shm"):
            Path(cls._dbpath + suf).unlink(missing_ok=True)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._port}{path}"

    def test_telegram_webhook_accepts_header_secret(self) -> None:
        body = json.dumps(
            {
                "update_id": 42,
                "message": {
                    "text": "please inspect the repo and fix the tests",
                    "message_id": 9,
                    "chat": {"id": 1},
                    "from": {"id": 2},
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/telegram/webhook"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "hdrsecret",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        detail = None
        for _ in range(40):
            req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=5"), method="GET")
            with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
            telegram_tasks = [t for t in tasks if t["channel"] == "telegram"]
            if telegram_tasks:
                tid = telegram_tasks[0]["task_id"]
                req_task = urllib.request.Request(self._url(f"/v1/tasks/{tid}"), method="GET")
                with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                    detail = json.loads(resp_task.read().decode("utf-8"))
                if detail["task"]["status"] == "queued":
                    break
            time.sleep(0.05)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["task"]["status"], "queued")
        self.assertEqual(detail["task"]["meta"]["telegram"]["chat_id"], 1)
        self.assertEqual(detail["task"]["meta"]["telegram"]["message_id"], 9)
        self.assertEqual(detail["task"]["meta"]["execution"]["lane"], "openclaw_hybrid")
        self.assertEqual(detail["task"]["meta"]["cursor"]["kind"], "openclaw")

    def test_telegram_greeting_routes_direct_without_cursor_task(self) -> None:
        body = json.dumps(
            {
                "update_id": 43,
                "message": {
                    "text": "hi andrea how are you?",
                    "message_id": 10,
                    "chat": {"id": 2},
                    "from": {"id": 3},
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/telegram/webhook"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "hdrsecret",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        detail = None
        for _ in range(40):
            req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=10"), method="GET")
            with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
            telegram_tasks = [t for t in tasks if t["channel"] == "telegram"]
            for task in telegram_tasks:
                req_task = urllib.request.Request(self._url(f"/v1/tasks/{task['task_id']}"), method="GET")
                with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                    candidate = json.loads(resp_task.read().decode("utf-8"))
                meta = candidate["task"].get("meta", {})
                if meta.get("telegram", {}).get("message_id") == 10:
                    detail = candidate
                    break
            if detail and detail["task"]["status"] == "completed":
                break
            time.sleep(0.05)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["task"]["status"], "completed")
        self.assertEqual(detail["task"]["meta"]["assistant"]["route"], "direct")
        self.assertNotIn("cursor", detail["task"]["meta"])

    def test_telegram_memory_question_uses_prior_chat_context(self) -> None:
        prev_enabled = os.environ.get("OPENAI_API_ENABLED")
        prev_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_ENABLED"] = "0"
        os.environ.pop("OPENAI_API_KEY", None)
        try:
            prior = self._srv.with_lock(
                lambda c: handle_command(
                    c,
                    {
                        "command_type": "SubmitUserMessage",
                        "channel": "telegram",
                        "external_id": "http-memory-prior",
                        "payload": {
                            "text": "Let's finish the reboot startup work.",
                            "chat_id": 22,
                            "message_id": 30,
                        },
                    },
                )
            )
            self._srv.with_lock(
                lambda c: append_event(
                    c,
                    prior["task_id"],
                    EventType.ASSISTANT_REPLIED,
                    {
                        "text": "We were working on reboot startup and Telegram memory.",
                        "route": "direct",
                        "reason": "history",
                    },
                )
            )
            body = json.dumps(
                {
                    "update_id": 44,
                    "message": {
                        "text": "Hi do you remember before?",
                        "message_id": 31,
                        "chat": {"id": 22},
                        "from": {"id": 3},
                    },
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                self._url("/v1/telegram/webhook"),
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Telegram-Bot-Api-Secret-Token": "hdrsecret",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
            detail = None
            for _ in range(40):
                req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=20"), method="GET")
                with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                    tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
                telegram_tasks = [t for t in tasks if t["channel"] == "telegram"]
                for task in telegram_tasks:
                    req_task = urllib.request.Request(self._url(f"/v1/tasks/{task['task_id']}"), method="GET")
                    with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                        candidate = json.loads(resp_task.read().decode("utf-8"))
                    meta = candidate["task"].get("meta", {})
                    if meta.get("telegram", {}).get("message_id") == 31:
                        detail = candidate
                        break
                if detail and detail["task"]["status"] == "completed":
                    break
                time.sleep(0.05)
            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertIn("remember the recent conversation", detail["task"]["meta"]["assistant"]["last_reply"].lower())
            self.assertIn("reboot startup", detail["task"]["meta"]["assistant"]["last_reply"].lower())
        finally:
            if prev_enabled is None:
                os.environ.pop("OPENAI_API_ENABLED", None)
            else:
                os.environ["OPENAI_API_ENABLED"] = prev_enabled
            if prev_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = prev_key

    def test_telegram_final_message_clips_long_cursor_excerpt(self) -> None:
        long_summary = "Implemented result. " + ("detail " * 300)
        text = format_final_message(
            "tsk_demo",
            status="completed",
            summary=long_summary,
            agent_url="https://cursor.com/agents/demo",
        )
        self.assertIn("Cursor said:", text)
        self.assertIn("Technical details:", text)
        self.assertLess(len(text), 1600)

    def test_telegram_final_message_for_openclaw_only_lane(self) -> None:
        text = format_final_message(
            "tsk_demo",
            status="completed",
            summary="Created a reminder and captured the note.",
            worker_label="OpenClaw",
            openclaw_session_id="sess-demo",
        )
        self.assertIn("OpenClaw said:", text)
        self.assertIn("OpenClaw session: sess-demo", text)

    def test_telegram_cursor_mention_sets_cursor_primary_routing(self) -> None:
        body = json.dumps(
            {
                "update_id": 45,
                "message": {
                    "text": "@Cursor please fix the failing tests",
                    "message_id": 33,
                    "chat": {"id": 23},
                    "from": {"id": 3},
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url("/v1/telegram/webhook"),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "hdrsecret",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        detail = None
        for _ in range(40):
            req_tasks = urllib.request.Request(self._url("/v1/tasks?limit=20"), method="GET")
            with urllib.request.urlopen(req_tasks, timeout=5) as resp_tasks:
                tasks = json.loads(resp_tasks.read().decode("utf-8"))["tasks"]
            telegram_tasks = [t for t in tasks if t["channel"] == "telegram"]
            for task in telegram_tasks:
                req_task = urllib.request.Request(self._url(f"/v1/tasks/{task['task_id']}"), method="GET")
                with urllib.request.urlopen(req_task, timeout=5) as resp_task:
                    candidate = json.loads(resp_task.read().decode("utf-8"))
                meta = candidate["task"].get("meta", {})
                if meta.get("telegram", {}).get("message_id") == 33:
                    detail = candidate
                    break
            if detail and detail["task"]["status"] == "queued":
                break
            time.sleep(0.05)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["task"]["meta"]["telegram"]["routing_hint"], "cursor")
        self.assertEqual(detail["task"]["meta"]["execution"]["collaboration_mode"], "cursor_primary")
        self.assertEqual(detail["task"]["task_id"], detail["task"]["task_id"])


if __name__ == "__main__":
    unittest.main()

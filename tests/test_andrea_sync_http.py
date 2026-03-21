"""HTTP smoke tests for Andrea lockstep server (localhost, ephemeral DB)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


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
        ):
            cls._env_backup[key] = os.environ.get(key)
        os.environ["ANDREA_SYNC_DB"] = cls._dbpath
        os.environ["ANDREA_SYNC_TELEGRAM_SECRET"] = "testhooksecret"
        os.environ["ANDREA_SYNC_INTERNAL_TOKEN"] = "internal-test-token"

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


if __name__ == "__main__":
    unittest.main()

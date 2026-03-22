"""Unit tests for andrea_lockstep_telegram_e2e helpers (no network)."""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_e2e():
    path = REPO_ROOT / "scripts" / "andrea_lockstep_telegram_e2e.py"
    spec = importlib.util.spec_from_file_location("andrea_lockstep_telegram_e2e", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["andrea_lockstep_telegram_e2e"] = mod
    spec.loader.exec_module(mod)
    return mod


e2e = _load_e2e()


class TestLockstepTelegramE2EHelpers(unittest.TestCase):
    def test_build_webhook_url(self) -> None:
        u = e2e.build_webhook_url("https://abc.trycloudflare.com", "s3cr=t")
        self.assertTrue(u.startswith("https://abc.trycloudflare.com/v1/telegram/webhook?"))
        self.assertIn("secret=", u)
        self.assertNotIn("s3cr=t", u)  # should be percent-encoded

    def test_redact_url(self) -> None:
        raw = "https://h.example/v1/telegram/webhook?secret=TOP&x=1"
        r = e2e.redact_url(raw)
        self.assertIn("secret=%2A%2A%2A", r)  # urllib may encode ***
        self.assertNotIn("TOP", r)

    def test_load_env_matches_live_server_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "repo"
            root.mkdir()
            cwd = tmp_path / "cwd"
            cwd.mkdir()
            home = tmp_path / "home"
            home.mkdir()
            extra = tmp_path / "extra.env"
            (root / ".env").write_text('TELEGRAM_BOT_TOKEN="repo-token"\n', encoding="utf-8")
            (cwd / ".env").write_text('ANDREA_SYNC_TELEGRAM_SECRET="cwd-secret"\n', encoding="utf-8")
            (home / "andrea-lockstep.env").write_text(
                'ANDREA_SYNC_PUBLIC_BASE="https://home.example"\n',
                encoding="utf-8",
            )
            extra.write_text('TELEGRAM_BOT_TOKEN="extra-token"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"ANDREA_ENV_FILE": str(extra)}, clear=True):
                with mock.patch.object(e2e, "repo_root", return_value=root):
                    with mock.patch.object(e2e.Path, "home", return_value=home):
                        with mock.patch.object(e2e.Path, "cwd", return_value=cwd):
                            e2e.load_env()
                            self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "extra-token")
                            self.assertEqual(os.environ["ANDREA_SYNC_TELEGRAM_SECRET"], "cwd-secret")
                            self.assertEqual(os.environ["ANDREA_SYNC_PUBLIC_BASE"], "https://home.example")

    def test_classify_webhook_health_unset(self) -> None:
        health = e2e.classify_webhook_health(
            {"ok": True, "result": {"url": ""}},
            expected_url="https://example.com/v1/telegram/webhook?secret=abc",
        )
        self.assertEqual(health["status"], "unset")
        self.assertFalse(health["registered"])
        self.assertFalse(health["matches_expected"])

    def test_classify_webhook_health_healthy_when_query_order_differs(self) -> None:
        health = e2e.classify_webhook_health(
            {
                "ok": True,
                "result": {
                    "url": "https://example.com/v1/telegram/webhook?x=1&secret=abc",
                },
            },
            expected_url="https://example.com/v1/telegram/webhook?secret=abc&x=1",
        )
        self.assertEqual(health["status"], "healthy")
        self.assertTrue(health["registered"])
        self.assertTrue(health["matches_expected"])

    def test_classify_webhook_health_drifted(self) -> None:
        health = e2e.classify_webhook_health(
            {
                "ok": True,
                "result": {
                    "url": "https://other.example/v1/telegram/webhook?secret=abc",
                },
            },
            expected_url="https://example.com/v1/telegram/webhook?secret=abc",
        )
        self.assertEqual(health["status"], "drifted")
        self.assertTrue(health["registered"])
        self.assertFalse(health["matches_expected"])

    def test_cmd_webhook_info_retries_until_match(self) -> None:
        responses = [
            {"ok": True, "result": {"url": ""}},
            {
                "ok": True,
                "result": {"url": "https://example.com/v1/telegram/webhook?secret=abc"},
            },
        ]
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "token",
                "ANDREA_SYNC_TELEGRAM_SECRET": "abc",
                "ANDREA_SYNC_PUBLIC_BASE": "https://example.com",
            },
            clear=True,
        ):
            with mock.patch.object(e2e, "load_env"):
                with mock.patch.object(e2e, "check_env", return_value=0):
                    with mock.patch.object(e2e, "telegram_api", side_effect=responses):
                        with mock.patch.object(e2e.time, "sleep") as sleep_mock:
                            out = io.StringIO()
                            with redirect_stdout(out):
                                rc = e2e.cmd_webhook_info(
                                    require_match=True,
                                    attempts=2,
                                    retry_delay_sec=0.01,
                                )
        self.assertEqual(rc, 0)
        sleep_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()

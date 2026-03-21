"""Unit tests for andrea_lockstep_telegram_e2e helpers (no network)."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()

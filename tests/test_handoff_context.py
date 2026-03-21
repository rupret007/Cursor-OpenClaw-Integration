"""Tests for handoff intent templates and triage (no network)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HC = REPO_ROOT / "scripts" / "handoff_context.py"


def _load_handoff_context():
    spec = importlib.util.spec_from_file_location("handoff_context", str(HC))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestHandoffContext(unittest.TestCase):
    def test_expand_intent_known(self) -> None:
        mod = _load_handoff_context()
        text = mod.expand_intent("code-review", "focus on auth")
        self.assertIn("code review", text.lower())
        self.assertIn("focus on auth", text)

    def test_compose_triage_only_tmpdir(self) -> None:
        mod = _load_handoff_context()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "README.md").write_text("x", encoding="utf-8")
            body = mod.compose_handoff_body("", None, p)
            self.assertIn("Pre-handoff repo triage", body)
            self.assertIn("README.md", body)

    def test_compose_intent_without_user(self) -> None:
        mod = _load_handoff_context()
        body = mod.compose_handoff_body("", "brief", None)
        self.assertIn("brief", body.lower())

    def test_unknown_intent_raises(self) -> None:
        mod = _load_handoff_context()
        with self.assertRaises(ValueError):
            mod.expand_intent("not-real", "x")


class TestSloTelegramProbe(unittest.TestCase):
    def test_probe_missing_token_exit_nonzero(self) -> None:
        env = {k: v for k, v in os.environ.items()}
        env.pop("TELEGRAM_BOT_TOKEN", None)
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "andrea_slo_telegram_probe.py")],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()

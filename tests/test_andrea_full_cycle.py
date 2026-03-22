"""Sanity checks for andrea_full_cycle.sh (no live services)."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "andrea_full_cycle.sh"
PREREQ = REPO_ROOT / "scripts" / "andrea_wrap_up_prereqs.sh"
LOGIN_BOOTSTRAP = REPO_ROOT / "scripts" / "macos" / "andrea_post_login_bootstrap.sh"
LOGIN_BOOTSTRAP_PLIST = (
    REPO_ROOT / "scripts" / "macos" / "com.andrea.andrea-post-login-bootstrap.plist.template"
)
LOCALTUNNEL_BOOTSTRAP = REPO_ROOT / "scripts" / "macos" / "andrea_localtunnel.sh"
LOCALTUNNEL_PLIST = (
    REPO_ROOT / "scripts" / "macos" / "com.andrea.andrea-localtunnel.plist.template"
)
OPENCLAW_HYBRID = REPO_ROOT / "scripts" / "andrea_sync_openclaw_hybrid.py"


class TestAndreaFullCycleScript(unittest.TestCase):
    def test_script_exists(self) -> None:
        self.assertTrue(SCRIPT.is_file(), str(SCRIPT))

    def test_bash_syntax_ok(self) -> None:
        subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_script_contains_guardrails(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("ANDREA_SYNC_INTERNAL_TOKEN", text)
        self.assertIn("skill-absence?skill=telegram", text)
        self.assertIn("SKIP_KILL_DRILL", text)
        self.assertIn("503", text)


class TestAndreaWrapUpPrereqsScript(unittest.TestCase):
    def test_prereqs_script_exists(self) -> None:
        self.assertTrue(PREREQ.is_file(), str(PREREQ))

    def test_prereqs_bash_syntax_ok(self) -> None:
        subprocess.run(
            ["bash", "-n", str(PREREQ)],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_prereqs_script_contains_guardrails(self) -> None:
        text = PREREQ.read_text(encoding="utf-8")
        self.assertIn("ANDREA_SYNC_INTERNAL_TOKEN", text)
        self.assertIn("/v1/health", text)
        self.assertIn("python3 scripts/andrea_sync_server.py", text)
        self.assertIn("Ready: bash scripts/andrea_full_cycle.sh", text)


class TestAndreaLoginBootstrap(unittest.TestCase):
    def test_login_bootstrap_script_exists(self) -> None:
        self.assertTrue(LOGIN_BOOTSTRAP.is_file(), str(LOGIN_BOOTSTRAP))

    def test_login_bootstrap_script_bash_syntax_ok(self) -> None:
        subprocess.run(
            ["bash", "-n", str(LOGIN_BOOTSTRAP)],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_login_bootstrap_template_exists(self) -> None:
        self.assertTrue(LOGIN_BOOTSTRAP_PLIST.is_file(), str(LOGIN_BOOTSTRAP_PLIST))

    def test_install_script_mentions_post_login_agent(self) -> None:
        text = (REPO_ROOT / "scripts" / "macos" / "install_andrea_launchagents.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("com.andrea.andrea-post-login-bootstrap.plist", text)
        self.assertIn("--load", text)
        self.assertIn("--with-localtunnel", text)

    def test_localtunnel_script_exists(self) -> None:
        self.assertTrue(LOCALTUNNEL_BOOTSTRAP.is_file(), str(LOCALTUNNEL_BOOTSTRAP))

    def test_localtunnel_script_bash_syntax_ok(self) -> None:
        subprocess.run(
            ["bash", "-n", str(LOCALTUNNEL_BOOTSTRAP)],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_localtunnel_template_exists(self) -> None:
        self.assertTrue(LOCALTUNNEL_PLIST.is_file(), str(LOCALTUNNEL_PLIST))

    def test_openclaw_hybrid_script_exists(self) -> None:
        self.assertTrue(OPENCLAW_HYBRID.is_file(), str(OPENCLAW_HYBRID))


if __name__ == "__main__":
    unittest.main()

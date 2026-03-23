"""Sanity checks for Andrea service-control scripts (no live services)."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES = REPO_ROOT / "scripts" / "andrea_services.sh"
LAUNCH_LIB = REPO_ROOT / "scripts" / "macos" / "andrea_launchagent_lib.sh"
GATEWAY_REFRESH = REPO_ROOT / "scripts" / "macos" / "andrea_openclaw_gateway_refresh.sh"
KILL_SWITCH = REPO_ROOT / "scripts" / "andrea_kill_switch.sh"
INSTALLER = REPO_ROOT / "scripts" / "macos" / "install_andrea_launchagents.sh"
REFRESH_PLIST = REPO_ROOT / "scripts" / "macos" / "com.andrea.openclaw-gateway-refresh.plist.template"


class TestAndreaServicesScript(unittest.TestCase):
    def test_script_exists(self) -> None:
        self.assertTrue(SERVICES.is_file(), str(SERVICES))

    def test_bash_syntax_ok(self) -> None:
        subprocess.run(
            ["bash", "-n", str(SERVICES)],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_labels_command_outputs_managed_labels(self) -> None:
        proc = subprocess.run(
            ["bash", str(SERVICES), "labels"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("com.andrea.andrea-sync", proc.stdout)
        self.assertIn("com.andrea.andrea-post-login-bootstrap", proc.stdout)
        self.assertIn("com.andrea.andrea-localtunnel", proc.stdout)

    def test_service_script_contains_expected_commands(self) -> None:
        text = SERVICES.read_text(encoding="utf-8")
        self.assertIn("status [all|gateway|sync|tunnel|bootstrap]", text)
        self.assertIn("install-launchagents", text)
        self.assertIn("andrea_post_login_bootstrap.sh", text)
        self.assertIn("status_webhook", text)
        self.assertIn("openclaw gateway", text)


class TestAndreaLaunchagentLibrary(unittest.TestCase):
    def test_library_exists(self) -> None:
        self.assertTrue(LAUNCH_LIB.is_file(), str(LAUNCH_LIB))

    def test_library_bash_syntax_ok(self) -> None:
        subprocess.run(
            ["bash", "-n", str(LAUNCH_LIB)],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_library_contains_shared_labels_and_debounce(self) -> None:
        text = LAUNCH_LIB.read_text(encoding="utf-8")
        self.assertIn("ANDREA_SYNC_LABEL", text)
        self.assertIn("ANDREA_BOOTSTRAP_LABEL", text)
        self.assertIn("andrea_default_stop_labels_csv", text)
        self.assertIn("andrea_restart_openclaw_gateway_debounced", text)
        self.assertIn("ANDREA_OPENCLAW_GATEWAY_RESTART_DEBOUNCE_SECONDS", text)


class TestAndreaGatewayRefreshIntegration(unittest.TestCase):
    def test_gateway_refresh_script_exists(self) -> None:
        self.assertTrue(GATEWAY_REFRESH.is_file(), str(GATEWAY_REFRESH))

    def test_gateway_refresh_bash_syntax_ok(self) -> None:
        subprocess.run(
            ["bash", "-n", str(GATEWAY_REFRESH)],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_refresh_plist_uses_repo_refresh_script(self) -> None:
        text = REFRESH_PLIST.read_text(encoding="utf-8")
        self.assertIn("andrea_openclaw_gateway_refresh.sh", text)

    def test_install_and_kill_switch_use_shared_control_surface(self) -> None:
        installer = INSTALLER.read_text(encoding="utf-8")
        kill_switch = KILL_SWITCH.read_text(encoding="utf-8")
        self.assertIn("--with-openclaw-refresh", installer)
        self.assertIn("legacy helper", installer)
        self.assertIn("andrea_default_stop_labels_csv", kill_switch)


if __name__ == "__main__":
    unittest.main()

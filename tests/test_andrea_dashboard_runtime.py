"""Execute the rendered operator dashboard against an offline browser fixture."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

from services.andrea_sync.dashboard import render_dashboard_html


class TestDashboardRuntime(unittest.TestCase):
    def test_rendered_monitor_handles_failure_recovery_and_task_races(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(
            node, "Install Node.js and put node on PATH to run the dashboard runtime gate"
        )
        harness = Path(__file__).with_name("dashboard_monitor_runtime.cjs")
        result = subprocess.run(
            [node, str(harness)],
            input=render_dashboard_html(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("dashboard runtime scenarios passed", result.stdout)


if __name__ == "__main__":
    unittest.main()

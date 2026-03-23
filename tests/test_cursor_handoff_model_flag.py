"""cursor_handoff.py --model and dry-run JSON."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HANDOFF = REPO_ROOT / "skills" / "cursor_handoff" / "scripts" / "cursor_handoff.py"


class CursorHandoffModelFlagTests(unittest.TestCase):
    def test_dry_run_json_includes_explicit_model(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(HANDOFF),
                "--repo",
                str(REPO_ROOT),
                "--prompt",
                "noop",
                "--dry-run",
                "--json",
                "--mode",
                "cli",
                "--model",
                "gpt-4.1",
            ],
            cwd=str(REPO_ROOT),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.strip())
        self.assertTrue(payload.get("dry_run"))
        self.assertEqual(payload.get("model"), "gpt-4.1")

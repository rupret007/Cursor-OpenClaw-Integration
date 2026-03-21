"""Tests for dotenv_set_key merge helper."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dotenv_set_key.py"


class TestDotenvSetKey(unittest.TestCase):
    def test_upsert_preserves_other_keys(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import dotenv_set_key as d  # noqa: E402

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            p.write_text(f'CURSOR_API_KEY={json.dumps("keep-me")}\n# comment\n', encoding="utf-8")
            d.upsert_env_file(p, {"GH_TOKEN": "ghp_x", "GITHUB_TOKEN": "ghp_x"})
            text = p.read_text(encoding="utf-8")
            self.assertIn("CURSOR_API_KEY", text)
            self.assertIn("keep-me", text)
            self.assertIn("# comment", text)
            self.assertIn('GH_TOKEN="ghp_x"', text)
            self.assertIn('GITHUB_TOKEN="ghp_x"', text)

    def test_cli_sets_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "GH_TOKEN",
                    "--no-github-alias",
                    "--value",
                    "tok123",
                    "--env-file",
                    str(env_path),
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("GH_TOKEN", proc.stdout)
            data = env_path.read_text(encoding="utf-8")
            self.assertIn("tok123", data)
            self.assertNotIn("GITHUB_TOKEN", data)

    def test_enable_openai_sets_both(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "OPENAI_API_KEY",
                    "--enable-openai",
                    "--value",
                    "sk-test",
                    "--env-file",
                    str(env_path),
                ],
                cwd=str(REPO_ROOT),
                check=True,
                capture_output=True,
            )
            data = env_path.read_text(encoding="utf-8")
            self.assertIn("OPENAI_API_KEY", data)
            self.assertIn("sk-test", data)
            self.assertIn("OPENAI_API_ENABLED", data)
            self.assertIn('"1"', data)

    def test_github_alias_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "GH_TOKEN",
                    "--value",
                    "abc",
                    "--env-file",
                    str(env_path),
                ],
                cwd=str(REPO_ROOT),
                check=True,
                capture_output=True,
            )
            data = env_path.read_text(encoding="utf-8")
            self.assertIn("GH_TOKEN", data)
            self.assertIn("GITHUB_TOKEN", data)


if __name__ == "__main__":
    unittest.main()

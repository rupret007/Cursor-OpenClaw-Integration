import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class SetupAdminTests(unittest.TestCase):
    def test_batch_brave_answers_falls_back_to_search_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            home_dir = tmp_path / "home"
            repo_dir = tmp_path / "repo"
            scripts_dir = repo_dir / "scripts"
            skill_dir = repo_dir / "skills" / "cursor_handoff"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            skill_dir.mkdir(parents=True, exist_ok=True)
            home_dir.mkdir(parents=True, exist_ok=True)

            # Copy the real setup script under test.
            shutil.copy2(REPO_ROOT / "scripts" / "setup_admin.sh", scripts_dir / "setup_admin.sh")

            # Minimal stub diagnose CLI used by setup_admin in batch mode.
            (scripts_dir / "cursor_openclaw.py").write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "print(json.dumps({'ok': True}))\n",
                encoding="utf-8",
            )
            os.chmod(scripts_dir / "cursor_openclaw.py", 0o755)

            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(home_dir),
                    "PATH": "/usr/bin:/bin",
                    "CURSOR_API_KEY": "dummy_key",
                    "BRAVE_SEARCH_API_KEY": "brave_search_123",
                    "BRAVE_ANSWERS_API_KEY": "",
                }
            )

            subprocess.run(
                ["bash", "scripts/setup_admin.sh", "--batch", "--force"],
                cwd=str(repo_dir),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            env_file = repo_dir / ".env"
            data = env_file.read_text(encoding="utf-8")
            self.assertIn('BRAVE_SEARCH_API_KEY="brave_search_123"', data)
            self.assertIn('BRAVE_ANSWERS_API_KEY="brave_search_123"', data)


if __name__ == "__main__":
    unittest.main()

import importlib.util
import io
import pathlib
import sys
import unittest
from contextlib import redirect_stdout


SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "cursor_handoff.py"
)
SPEC = importlib.util.spec_from_file_location("cursor_handoff", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["cursor_handoff"] = MODULE
SPEC.loader.exec_module(MODULE)  # type: ignore[attr-defined]


class CursorHandoffTests(unittest.TestCase):
    def test_parse_bool_text(self):
        self.assertTrue(MODULE.parse_bool_text("true"))
        self.assertTrue(MODULE.parse_bool_text("YES"))
        self.assertFalse(MODULE.parse_bool_text("false"))
        with self.assertRaises(ValueError):
            MODULE.parse_bool_text("maybe")

    def test_build_handoff_prompt_branch_toggle(self):
        with_branch = MODULE.build_handoff_prompt(
            "Do a review", read_only=True, branch="feature/x", include_branch=True
        )
        without_branch = MODULE.build_handoff_prompt(
            "Do a review", read_only=True, branch="feature/x", include_branch=False
        )
        self.assertIn("Target branch: feature/x", with_branch)
        self.assertNotIn("Target branch: feature/x", without_branch)

    def test_normalize_base_url(self):
        self.assertTrue(MODULE.normalize_base_url("https://api.cursor.com/").startswith("https://"))
        with self.assertRaises(ValueError):
            MODULE.normalize_base_url("file:///etc/passwd")

    def test_normalize_repo_input(self):
        local, url, err = MODULE.normalize_repo_input("owner/repo")
        self.assertIsNone(local)
        self.assertEqual(url, "https://github.com/owner/repo")
        self.assertIsNone(err)

    def test_choose_backend(self):
        backend, err = MODULE.choose_backend(
            requested_mode="auto",
            has_api_creds=True,
            cli_wrapper_path=pathlib.Path("/tmp/missing-wrapper"),
            cli_binary=None,
        )
        self.assertEqual(backend, "api")
        self.assertIsNone(err)

    def test_ssl_hint(self):
        hint = MODULE.build_ssl_hint("CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate")
        self.assertIsNotNone(hint)
        no_hint = MODULE.build_ssl_hint("some other error")
        self.assertIsNone(no_hint)

    def test_emit_text_diagnose(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            MODULE.emit_text(
                {
                    "ok": True,
                    "diagnose": True,
                    "checks": {
                        "api_key_set": False,
                        "api_base_url": "https://api.cursor.com",
                        "requested_mode": "auto",
                        "suggested_backend": "none",
                        "cli_binary": None,
                    },
                }
            )
        out = buf.getvalue()
        self.assertIn("Diagnostics complete", out)
        self.assertNotIn("Handoff submitted successfully", out)

    def test_emit_text_dry_run(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            MODULE.emit_text(
                {
                    "ok": True,
                    "dry_run": True,
                    "backend": "api",
                    "backend_error": None,
                    "mode_requested": "api",
                    "read_only": True,
                    "branch": "b1",
                    "repo_input": "/tmp",
                }
            )
        out = buf.getvalue()
        self.assertIn("Dry run", out)
        self.assertNotIn("Handoff submitted successfully", out)


if __name__ == "__main__":
    unittest.main()

import importlib.util
import pathlib
import sys
import unittest


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


if __name__ == "__main__":
    unittest.main()

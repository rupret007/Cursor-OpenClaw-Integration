import importlib.util
import pathlib
import sys
import types
import unittest


SCRIPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "cursor_openclaw.py"
SPEC = importlib.util.spec_from_file_location("cursor_openclaw", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["cursor_openclaw"] = MODULE
SPEC.loader.exec_module(MODULE)  # type: ignore[attr-defined]


class CursorOpenClawTests(unittest.TestCase):
    def test_parse_bool(self):
        self.assertTrue(MODULE.parse_bool("true"))
        self.assertTrue(MODULE.parse_bool("YES"))
        self.assertFalse(MODULE.parse_bool("false"))
        with self.assertRaises(ValueError):
            MODULE.parse_bool("maybe")

    def test_normalize_base_url(self):
        self.assertEqual(MODULE.normalize_base_url("https://api.cursor.com/"), "https://api.cursor.com")
        self.assertEqual(MODULE.normalize_base_url(""), "https://api.cursor.com")
        self.assertEqual(MODULE.normalize_base_url("http://localhost:8080/"), "http://localhost:8080")
        with self.assertRaises(ValueError):
            MODULE.normalize_base_url("ftp://example.com")
        with self.assertRaises(ValueError):
            MODULE.normalize_base_url("not-a-url")

    def test_validate_command_args_create_poll(self):
        bad = types.SimpleNamespace(
            command="create-agent",
            branch_name="b",
            poll_attempts=-1,
            poll_interval_seconds=1.0,
        )
        with self.assertRaises(ValueError):
            MODULE.validate_command_args(bad)
        bad2 = types.SimpleNamespace(
            command="create-agent",
            branch_name="b",
            poll_attempts=0,
            poll_interval_seconds=-0.5,
        )
        with self.assertRaises(ValueError):
            MODULE.validate_command_args(bad2)
        ok = types.SimpleNamespace(
            command="create-agent",
            branch_name="b",
            poll_attempts=0,
            poll_interval_seconds=0.0,
        )
        MODULE.validate_command_args(ok)
        skip = types.SimpleNamespace(command="whoami")
        MODULE.validate_command_args(skip)

    def test_validate_command_args_branch_newline(self):
        bad = types.SimpleNamespace(
            command="create-agent",
            branch_name="evil\ninj",
            poll_attempts=0,
            poll_interval_seconds=0.0,
        )
        with self.assertRaises(ValueError):
            MODULE.validate_command_args(bad)

    def test_validate_common_args(self):
        ok = types.SimpleNamespace(timeout_seconds=30, retries=2, retry_backoff_seconds=0.5)
        MODULE.validate_common_args(ok)

        bad_timeout = types.SimpleNamespace(timeout_seconds=0, retries=2, retry_backoff_seconds=0.5)
        with self.assertRaises(ValueError):
            MODULE.validate_common_args(bad_timeout)

        bad_retries = types.SimpleNamespace(timeout_seconds=30, retries=-1, retry_backoff_seconds=0.5)
        with self.assertRaises(ValueError):
            MODULE.validate_common_args(bad_retries)

        bad_backoff = types.SimpleNamespace(timeout_seconds=30, retries=2, retry_backoff_seconds=-0.1)
        with self.assertRaises(ValueError):
            MODULE.validate_common_args(bad_backoff)

    def test_build_create_payload_from_repository(self):
        class Args:
            prompt = "hello"
            repository = "https://github.com/foo/bar"
            ref = "main"
            pr_url = ""
            model = "default"
            branch_name = "cursor/test"
            auto_create_pr = False
            open_as_cursor_github_app = False
            skip_reviewer_request = False

        payload = MODULE.build_create_payload(Args())
        self.assertEqual(payload["source"]["repository"], "https://github.com/foo/bar")
        self.assertEqual(payload["source"]["ref"], "main")
        self.assertEqual(payload["target"]["branchName"], "cursor/test")
        self.assertFalse(payload["target"]["autoCreatePr"])

    def test_require_one_of_exclusive(self):
        with self.assertRaises(ValueError) as ctx:
            MODULE.require_one_of("https://github.com/a/b", "https://github.com/a/b/pull/1")
        self.assertIn("only one", str(ctx.exception).lower())
        with self.assertRaises(ValueError):
            MODULE.require_one_of("", "")

    def test_build_create_payload_from_pr(self):
        class Args:
            prompt = "hello"
            repository = ""
            ref = ""
            pr_url = "https://github.com/foo/bar/pull/1"
            model = "default"
            branch_name = "cursor/test"
            auto_create_pr = True
            open_as_cursor_github_app = True
            skip_reviewer_request = True

        payload = MODULE.build_create_payload(Args())
        self.assertEqual(payload["source"]["prUrl"], "https://github.com/foo/bar/pull/1")
        self.assertTrue(payload["target"]["autoCreatePr"])
        self.assertTrue(payload["target"]["openAsCursorGithubApp"])
        self.assertTrue(payload["target"]["skipReviewerRequest"])


if __name__ == "__main__":
    unittest.main()

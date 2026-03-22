import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock


SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "andrea_sync_openclaw_hybrid.py"
)
SPEC = importlib.util.spec_from_file_location("andrea_sync_openclaw_hybrid", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["andrea_sync_openclaw_hybrid"] = MODULE
SPEC.loader.exec_module(MODULE)  # type: ignore[attr-defined]


class AndreaSyncOpenClawHybridTests(unittest.TestCase):
    def test_build_prompt_for_collaboration_mentions_tri_llm_roles(self) -> None:
        prompt = MODULE._build_prompt(
            "tsk_demo",
            "Work together on a one-hour repo sprint.",
            "/tmp/repo",
            "technical_or_repo_request",
            "collaborative",
        )
        self.assertIn("Gemini 2.5", prompt)
        self.assertIn("Minimax 2.7", prompt)
        self.assertIn("OpenAI", prompt)
        self.assertIn("Cursor for the heavy repo execution", prompt)
        self.assertIn("collaboration transcript", prompt)

    def test_build_prompt_for_cursor_primary_still_requires_cursor(self) -> None:
        prompt = MODULE._build_prompt(
            "tsk_demo",
            "@Cursor fix the repo issues",
            "/tmp/repo",
            "technical_or_repo_request",
            "cursor_primary",
        )
        self.assertIn("must involve Cursor", prompt)
        self.assertIn("repo-heavy execution into Cursor", prompt)

    def test_build_prompt_respects_preferred_model_lane(self) -> None:
        prompt = MODULE._build_prompt(
            "tsk_demo",
            "@Gemini review this approach",
            "/tmp/repo",
            "explicit_model_mention",
            "andrea_primary",
            "gemini",
            "Gemini",
        )
        self.assertIn("explicitly addressed the Gemini lane", prompt)
        self.assertIn("Preferred model family: gemini", prompt)
        self.assertIn("fall back", prompt)

    def test_derive_summary_prefers_lockstep_summary(self) -> None:
        s = MODULE._derive_openclaw_summary(
            {"summary": "Shipped the fix."},
            "ignored prose",
            {"summary": "ignored top"},
        )
        self.assertEqual(s, "Shipped the fix.")

    def test_derive_summary_falls_back_to_clean_text(self) -> None:
        s = MODULE._derive_openclaw_summary(
            {"summary": ""},
            "User-visible answer before the marker.",
            {},
        )
        self.assertEqual(s, "User-visible answer before the marker.")

    def test_derive_summary_falls_back_to_payload_message(self) -> None:
        s = MODULE._derive_openclaw_summary(
            {},
            "",
            {"message": "Done via tool output."},
        )
        self.assertEqual(s, "Done via tool output.")

    def test_derive_summary_falls_back_to_result_nested_text(self) -> None:
        s = MODULE._derive_openclaw_summary(
            {},
            "",
            {"result": {"text": "Nested completion text."}},
        )
        self.assertEqual(s, "Nested completion text.")

    def test_derive_summary_empty_returns_empty_string(self) -> None:
        self.assertEqual(MODULE._derive_openclaw_summary({}, "", {}), "")

    def test_run_openclaw_hybrid_uses_generic_when_no_usable_text(self) -> None:
        stdout_obj = {
            "runId": "run-empty",
            "status": "completed",
            "result": {
                "payloads": [
                    {
                        "text": (
                            "LOCKSTEP_JSON: "
                            '{"delegated_to_cursor":false,"summary":"","status":"completed"}'
                        )
                    }
                ],
            },
        }

        def fake_run(*_a, **_k):
            class R:
                returncode = 0
                stdout = json.dumps(stdout_obj)
                stderr = ""

            return R()

        with mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run):
            out = MODULE.run_openclaw_hybrid(
                task_id="tsk_x",
                prompt="do thing",
                repo_path="/tmp/r",
                agent_id="main",
                route_reason="test",
                collaboration_mode="auto",
                preferred_model_family="",
                preferred_model_label="",
                timeout_seconds=60,
                thinking="",
            )
        self.assertEqual(out["summary"], "OpenClaw completed the delegated task.")
        self.assertTrue(out.get("ok"))

    def test_run_openclaw_hybrid_prefers_prose_over_empty_lockstep_summary(self) -> None:
        stdout_obj = {
            "runId": "run-prose",
            "status": "completed",
            "result": {
                "payloads": [
                    {
                        "text": (
                            "Here is the real outcome for the user.\n"
                            "LOCKSTEP_JSON: "
                            '{"delegated_to_cursor":false,"summary":"","status":"completed"}'
                        )
                    }
                ],
            },
        }

        def fake_run(*_a, **_k):
            class R:
                returncode = 0
                stdout = json.dumps(stdout_obj)
                stderr = ""

            return R()

        with mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run):
            out = MODULE.run_openclaw_hybrid(
                task_id="tsk_y",
                prompt="do thing",
                repo_path="/tmp/r",
                agent_id="main",
                route_reason="test",
                collaboration_mode="auto",
                preferred_model_family="",
                preferred_model_label="",
                timeout_seconds=60,
                thinking="",
            )
        self.assertEqual(out["summary"], "Here is the real outcome for the user.")
        self.assertIn("Here is the real outcome", out["raw_text"])


if __name__ == "__main__":
    unittest.main()

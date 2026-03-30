import importlib.util
import json
import pathlib
import re
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
        self.assertIn("cursor_handoff for repo work", prompt)
        self.assertIn("collaboration_trace", prompt)
        self.assertIn("phase_trace", prompt)
        self.assertIn("planner_summary", prompt)
        self.assertIn("collaboration traces sparse and user-safe", prompt)
        self.assertIn("bluebubbles", prompt.lower())
        self.assertIn("iMessage", prompt)

    def test_build_prompt_for_cursor_primary_prefers_openclaw_skills(self) -> None:
        prompt = MODULE._build_prompt(
            "tsk_demo",
            "@Cursor fix the repo issues",
            "/tmp/repo",
            "technical_or_repo_request",
            "cursor_primary",
        )
        self.assertIn("Stay entirely inside OpenClaw", prompt)
        self.assertIn("cursor_handoff", prompt)
        self.assertIn("session keys", prompt)

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

    def test_derive_contract_prefers_lockstep_summary_and_trace(self) -> None:
        contract = MODULE._derive_openclaw_contract(
            {
                "summary": "Shipped the fix.",
                "collaboration_trace": ["OpenClaw triaged the task.", "Cursor applied the repo fix."],
            },
            "ignored prose",
            {"summary": "ignored top"},
        )
        self.assertEqual(contract["summary"], "Shipped the fix.")
        self.assertEqual(
            contract["collaboration_trace"],
            ["OpenClaw triaged the task.", "Cursor applied the repo fix."],
        )
        self.assertEqual(contract["phase_outputs"]["plan"]["summary"], "OpenClaw triaged the task.")

    def test_derive_contract_falls_back_to_clean_text(self) -> None:
        contract = MODULE._derive_openclaw_contract(
            {"summary": ""},
            "User-visible answer before the marker.",
            {},
        )
        self.assertEqual(contract["summary"], "User-visible answer before the marker.")

    def test_derive_contract_falls_back_to_payload_message(self) -> None:
        contract = MODULE._derive_openclaw_contract(
            {},
            "",
            {"message": "Done via tool output."},
        )
        self.assertEqual(contract["summary"], "Done via tool output.")

    def test_derive_contract_falls_back_to_result_nested_text(self) -> None:
        contract = MODULE._derive_openclaw_contract(
            {},
            "",
            {"result": {"text": "Nested completion text."}},
        )
        self.assertEqual(contract["summary"], "Nested completion text.")

    def test_derive_contract_detects_blocked_capability_and_keeps_internal_trace(self) -> None:
        contract = MODULE._derive_openclaw_contract(
            {},
            "I need a sessionKey before I can talk to Cursor.",
            {},
        )
        self.assertIn("OpenClaw handles session routing", contract["summary"])
        self.assertIn("OpenClaw handles session routing", contract["blocked_reason"])
        self.assertIn("sessionKey", contract["internal_trace"])

    def test_derive_contract_builds_machine_trace_and_phase_outputs(self) -> None:
        contract = MODULE._derive_openclaw_contract(
            {"summary": "The fix is ready."},
            "",
            {},
            payloads=[
                {"text": "Plan the repo triage before changing code."},
                {"text": "Critique the risky path before execution."},
                {"text": "Cursor can implement the execution step once the plan is solid."},
            ],
            collaboration_mode="collaborative",
            delegated_to_cursor=True,
            provider="google",
            model="gemini-2.5-flash",
        )
        self.assertTrue(contract["machine_collaboration_trace"])
        phases = {row["phase"] for row in contract["machine_collaboration_trace"]}
        self.assertIn("plan", phases)
        self.assertIn("critique", phases)
        self.assertIn("execution", phases)
        self.assertNotIn("synthesis", contract["phase_outputs"])

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
                session_id="sess-task-x",
                route_reason="test",
                collaboration_mode="auto",
                preferred_model_family="",
                preferred_model_label="",
                timeout_seconds=60,
                thinking="",
            )
        self.assertEqual(out["summary"], "OpenClaw completed the delegated task.")
        self.assertTrue(out.get("ok"))
        self.assertIn("phase_outputs", out)

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
                session_id="sess-task-y",
                route_reason="test",
                collaboration_mode="auto",
                preferred_model_family="",
                preferred_model_label="",
                timeout_seconds=60,
                thinking="",
            )
        self.assertEqual(out["summary"], "Here is the real outcome for the user.")
        self.assertIn("Here is the real outcome", out["raw_text"])
        self.assertIn("phase_outputs", out)

    def test_run_openclaw_hybrid_sanitizes_internal_runtime_leakage(self) -> None:
        stdout_obj = {
            "runId": "run-blocked",
            "status": "completed",
            "result": {
                "payloads": [
                    {
                        "text": (
                            "I need a sessionKey or label before I can talk to Cursor.\n"
                            "sessions_spawn.attachments.enabled is disabled.\n"
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
                task_id="tsk_blocked",
                prompt="do thing",
                repo_path="/tmp/r",
                agent_id="main",
                session_id="sess-task-blocked",
                route_reason="test",
                collaboration_mode="cursor_primary",
                preferred_model_family="",
                preferred_model_label="",
                timeout_seconds=60,
                thinking="",
            )
        self.assertNotIn("sessionKey", out["summary"])
        self.assertIn("internal collaboration limitation", out["summary"])
        self.assertIn("sessionKey", out["internal_trace"])
        self.assertIn("internal collaboration limitation", out["blocked_reason"])

    def test_derive_contract_sanitizes_install_and_config_runtime_jargon(self) -> None:
        contract = MODULE._derive_openclaw_contract(
            {},
            "Run openclaw skills install bluebubbles and set plugins.entries.voice-call.enabled before retrying.",
            {},
        )
        self.assertNotIn("openclaw skills install", contract["summary"].lower())
        self.assertNotIn("plugins.entries", contract["summary"].lower())
        self.assertIn("openclaw skills install", contract["internal_trace"].lower())

    def test_run_openclaw_hybrid_passes_explicit_session_id(self) -> None:
        stdout_obj = {
            "runId": "run-session",
            "status": "completed",
            "result": {"payloads": [{"text": "LOCKSTEP_JSON: {\"summary\":\"done\",\"status\":\"completed\"}"}]},
        }
        seen_argv = []

        def fake_run(argv, *_a, **_k):
            seen_argv.extend(argv)

            class R:
                returncode = 0
                stdout = json.dumps(stdout_obj)
                stderr = ""

            return R()

        with mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run):
            out = MODULE.run_openclaw_hybrid(
                task_id="tsk_session",
                prompt="do thing",
                repo_path="/tmp/r",
                agent_id="main",
                session_id="sess-explicit-1",
                route_reason="test",
                collaboration_mode="auto",
                preferred_model_family="",
                preferred_model_label="",
                timeout_seconds=60,
                thinking="",
            )
        self.assertIn("--session-id", seen_argv)
        self.assertIn("sess-explicit-1", seen_argv)
        self.assertEqual(out["requested_session_id"], "sess-explicit-1")
        self.assertIn("machine_collaboration_trace", out)

    def test_extract_lockstep_json_last_marker_wins(self) -> None:
        text = (
            'First line.\n'
            'LOCKSTEP_JSON: {"summary":"one","status":"completed"}\n'
            'LOCKSTEP_JSON: {"summary":"two","status":"completed","delegated_to_cursor":true}\n'
        )
        cleaned, meta = MODULE._extract_lockstep_json(text)
        self.assertEqual(cleaned, "First line.")
        self.assertEqual(meta.get("summary"), "two")
        self.assertTrue(meta.get("delegated_to_cursor"))

    def test_extract_lockstep_json_invalid_marker_keeps_line_in_prose(self) -> None:
        text = "Note.\nLOCKSTEP_JSON: not-valid-json{\nMore."
        cleaned, meta = MODULE._extract_lockstep_json(text)
        self.assertIn("LOCKSTEP_JSON:", cleaned)
        self.assertEqual(meta, {})

    def test_run_openclaw_hybrid_empty_stdout_raises(self) -> None:
        def fake_run(*_a, **_k):
            class R:
                returncode = 0
                stdout = "   \n"
                stderr = "openclaw died"

            return R()

        with mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                MODULE.run_openclaw_hybrid(
                    task_id="tsk_empty",
                    prompt="x",
                    repo_path="/tmp/r",
                    agent_id="main",
                    session_id="",
                    route_reason="test",
                    collaboration_mode="auto",
                    preferred_model_family="",
                    preferred_model_label="",
                    timeout_seconds=60,
                    thinking="",
                )
        self.assertIn("empty OpenClaw", str(ctx.exception))

    def test_run_openclaw_hybrid_invalid_stdout_json_raises(self) -> None:
        def fake_run(*_a, **_k):
            class R:
                returncode = 0
                stdout = "NOT_JSON"
                stderr = ""

            return R()

        with mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                MODULE.run_openclaw_hybrid(
                    task_id="tsk_badjson",
                    prompt="x",
                    repo_path="/tmp/r",
                    agent_id="main",
                    session_id="",
                    route_reason="test",
                    collaboration_mode="auto",
                    preferred_model_family="",
                    preferred_model_label="",
                    timeout_seconds=60,
                    thinking="",
                )
        self.assertIn("invalid OpenClaw JSON", str(ctx.exception))

    def test_run_openclaw_hybrid_nonzero_exit_raises(self) -> None:
        stdout_obj = {"status": "error", "error": "tool timeout", "result": {}}

        def fake_run(*_a, **_k):
            class R:
                returncode = 2
                stdout = json.dumps(stdout_obj)
                stderr = ""

            return R()

        with mock.patch.object(MODULE.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                MODULE.run_openclaw_hybrid(
                    task_id="tsk_rc",
                    prompt="x",
                    repo_path="/tmp/r",
                    agent_id="main",
                    session_id="",
                    route_reason="test",
                    collaboration_mode="auto",
                    preferred_model_family="",
                    preferred_model_label="",
                    timeout_seconds=60,
                    thinking="",
                )
        self.assertIn("tool timeout", str(ctx.exception))

    def test_run_openclaw_hybrid_cursor_agent_url_sets_delegated_even_if_lockstep_false(self) -> None:
        agent = "https://cursor.com/agents/abc-123"
        stdout_obj = {
            "runId": "run-url",
            "status": "completed",
            "result": {
                "payloads": [
                    {
                        "text": (
                            f"Handed off here: {agent}\n"
                            "LOCKSTEP_JSON: "
                            '{"delegated_to_cursor":false,"summary":"done","status":"completed"}'
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
                task_id="tsk_url",
                prompt="x",
                repo_path="/tmp/r",
                agent_id="main",
                session_id="",
                route_reason="test",
                collaboration_mode="auto",
                preferred_model_family="",
                preferred_model_label="",
                timeout_seconds=60,
                thinking="",
            )
        self.assertTrue(out["delegated_to_cursor"])
        self.assertEqual(out["agent_url"], agent)
        self.assertEqual(out["cursor_agent_id"], "abc-123")

    def test_openclaw_enforce_default_includes_bluebubbles(self) -> None:
        script = (
            pathlib.Path(__file__).resolve().parent.parent / "scripts" / "andrea_openclaw_enforce.sh"
        )
        body = script.read_text()
        m = re.search(r'_DEFAULT_REQUIRED_SKILLS="([^"]+)"', body)
        self.assertIsNotNone(m)
        self.assertIn("bluebubbles", m.group(1))


if __name__ == "__main__":
    unittest.main()

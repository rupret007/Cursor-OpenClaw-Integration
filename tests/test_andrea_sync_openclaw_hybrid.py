import importlib.util
import pathlib
import sys
import unittest


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
            False,
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
            True,
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
            False,
            "gemini",
            "Gemini",
        )
        self.assertIn("explicitly addressed the Gemini lane", prompt)
        self.assertIn("Preferred model family: gemini", prompt)
        self.assertIn("fall back", prompt)


if __name__ == "__main__":
    unittest.main()

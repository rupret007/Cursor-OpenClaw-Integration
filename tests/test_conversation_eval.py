"""Unit tests for conversational self-eval (detectors, clustering, briefs, gates)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.conversation_eval import (  # noqa: E402
    CONVERSATION_SMOKE_CASE_IDS,
    attach_conversation_eval_report,
    build_cursor_fix_brief,
    build_turn_capture,
    cluster_failed_checks,
    conversation_core_scenarios,
    run_deterministic_detectors,
    runtime_adjudication_enabled,
    runtime_adjudication_gate,
    _wait_statuses_for_policy,
)
from services.andrea_sync.schema import TaskStatus  # noqa: E402
from services.andrea_sync.experience_assurance import run_experience_assurance  # noqa: E402
from services.andrea_sync.experience_types import (  # noqa: E402
    ExperienceCheckResult,
    ExperienceObservation,
    ExperienceScenario,
)
from services.andrea_sync.store import connect, get_latest_experience_run, migrate  # noqa: E402


class ConversationEvalDetectorTests(unittest.TestCase):
    def test_detects_text_lane_summarize_carryover_miss(self) -> None:
        cap = {
            "raw_reply_text": "Sure, tell me more about what you need.",
            "user_turn": "Can you summarize my texts too?",
            "turn_plan_domain": "casual_conversation",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(
            cap,
            prior_user_turn="Can you pull text messages from BlueBubbles?",
            expect_tool_carryover=True,
        )
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_followup_carryover_miss", codes)

    def test_detects_internal_runtime_leak(self) -> None:
        cap = {
            "raw_reply_text": "session id lockstep_json",
            "user_turn": "Hi",
            "turn_plan_domain": "casual_conversation",
            "leak_internal_runtime": True,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_metadata_surface_leak", codes)

    def test_runtime_gate_respects_env(self) -> None:
        prev = os.environ.get("ANDREA_RUNTIME_SEMANTIC_ADJUDICATOR")
        try:
            os.environ["ANDREA_RUNTIME_SEMANTIC_ADJUDICATOR"] = "0"
            self.assertFalse(
                runtime_adjudication_gate(
                    user_text="What about that one?",
                    scenario_confidence=0.45,
                    scenario_id="statusFollowupContinue",
                    force_delegate=False,
                )
            )
            os.environ["ANDREA_RUNTIME_SEMANTIC_ADJUDICATOR"] = "1"
            self.assertTrue(runtime_adjudication_enabled())
            self.assertTrue(
                runtime_adjudication_gate(
                    user_text="What about that one?",
                    scenario_confidence=0.45,
                    scenario_id="statusFollowupContinue",
                    force_delegate=False,
                )
            )
        finally:
            if prev is None:
                os.environ.pop("ANDREA_RUNTIME_SEMANTIC_ADJUDICATOR", None)
            else:
                os.environ["ANDREA_RUNTIME_SEMANTIC_ADJUDICATOR"] = prev


class ConversationEvalReportTests(unittest.TestCase):
    def test_cluster_and_brief_shape(self) -> None:
        sc = ExperienceScenario(
            scenario_id="conversation_core::demo",
            title="demo",
            description="d",
            category="conversation",
            runner=lambda h, s: ExperienceCheckResult.from_observations(s, []),
        )
        failed = ExperienceCheckResult.from_observations(
            sc,
            [
                ExperienceObservation(
                    description="x",
                    expected="y",
                    observed="z",
                    passed=False,
                    issue_code="conversation_question_echo",
                )
            ],
            metadata={"failure_families": ["question_echo"]},
        )
        clusters = cluster_failed_checks([failed])
        self.assertTrue(clusters)
        brief = build_cursor_fix_brief(cluster=clusters[0], checks=[failed])
        self.assertIn("failing_prompts", brief)
        self.assertIn("baseline_vs_candidate", brief)

    def test_attach_report_adds_clusters(self) -> None:
        meta: dict = {}
        sc = ExperienceScenario(
            scenario_id="conversation_core::demo2",
            title="demo2",
            description="d",
            category="conversation",
            runner=lambda h, s: ExperienceCheckResult.from_observations(s, []),
        )
        chk = ExperienceCheckResult.from_observations(
            sc,
            [
                ExperienceObservation(
                    description="x",
                    expected="y",
                    observed="z",
                    passed=False,
                )
            ],
            metadata={"failure_families": ["generic_fallback_leak"]},
        )
        attach_conversation_eval_report(meta, [chk], prepare_fix_brief=True)
        self.assertIn("conversation_failure_clusters", meta)
        self.assertIn("cursor_fix_briefs", meta)


class ConversationCoreSuitePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = Path(self._tmp.name)
        self.conn = connect(self.db_path)
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.db_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(self.db_path) + suffix).unlink(missing_ok=True)

    def test_conversation_core_persists_suite_metadata(self) -> None:
        def _stub_scenarios(opts: object) -> list:
            def runner(harness: object, scenario: ExperienceScenario) -> ExperienceCheckResult:
                return ExperienceCheckResult.from_observations(
                    scenario,
                    [
                        ExperienceObservation(
                            description="stub",
                            expected="ok",
                            observed="ok",
                            passed=True,
                        )
                    ],
                    metadata={
                        "failure_families": [],
                        "captures": [{"user_turn": "Hi", "raw_reply_text": "Hello there"}],
                    },
                )

            return [
                ExperienceScenario(
                    scenario_id="conversation_core::stub",
                    title="stub",
                    description="stub",
                    category="conversation",
                    tags=["conversation_eval"],
                    suspected_files=[],
                    runner=runner,
                )
            ]

        with mock.patch(
            "services.andrea_sync.conversation_eval.conversation_core_scenarios",
            side_effect=_stub_scenarios,
        ):
            result = run_experience_assurance(
                self.conn,
                actor="test",
                repo_path=REPO_ROOT,
                suite="conversation_core",
                conversation_eval_options={"prepare_fix_brief": True},
            )
        self.assertTrue(result["ok"])
        latest = get_latest_experience_run(self.conn)
        self.assertEqual(latest["metadata"].get("suite"), "conversation_core")
        self.assertIn("conversation_failure_clusters", latest["metadata"])
        self.assertEqual(len(latest["checks"]), 1)


class ConversationCoreScenariosTests(unittest.TestCase):
    def test_conversation_core_scenario_count(self) -> None:
        rows = conversation_core_scenarios({})
        self.assertGreaterEqual(len(rows), 18)
        ids = {r.scenario_id for r in rows}
        self.assertIn("conversation_core::hi_andrea", ids)
        self.assertIn("conversation_core::news_today", ids)

    def test_conversation_smoke_subset_size(self) -> None:
        rows = conversation_core_scenarios({"smoke": True})
        self.assertEqual(len(rows), len(CONVERSATION_SMOKE_CASE_IDS))
        ids = {r.scenario_id for r in rows}
        for cid in CONVERSATION_SMOKE_CASE_IDS:
            self.assertIn(f"conversation_core::{cid}", ids)

    def test_wait_policy_status_sets(self) -> None:
        terminal = _wait_statuses_for_policy("terminal_reply")
        self.assertIn(TaskStatus.COMPLETED.value, terminal)
        self.assertIn(TaskStatus.FAILED.value, terminal)
        self.assertNotIn(TaskStatus.QUEUED.value, terminal)
        routing = _wait_statuses_for_policy("routing_smoke")
        self.assertIn(TaskStatus.QUEUED.value, routing)
        self.assertIn(TaskStatus.RUNNING.value, routing)


class BuildTurnCaptureTests(unittest.TestCase):
    def test_build_turn_capture_minimal(self) -> None:
        detail = {
            "task": {
                "task_id": "t1",
                "status": "completed",
                "meta": {
                    "assistant": {
                        "last_reply": "Hello!",
                        "route": "direct",
                        "reason": "heuristic_greeting",
                    },
                    "scenario": {"scenario_id": "statusFollowupContinue", "reason": "status"},
                },
            }
        }

        class _H:
            server = None
            conn = object()

        with mock.patch(
            "services.andrea_sync.conversation_eval.project_task_dict",
            return_value={"meta": {}},
        ), mock.patch(
            "services.andrea_sync.conversation_eval.get_task_channel",
            return_value="telegram",
        ):
            cap = build_turn_capture(
                harness=_H(),
                task_id="t1",
                user_text="Hi Andrea",
                detail=detail,
            )
        self.assertEqual(cap["assistant_route"], "direct")
        self.assertIn("turn_plan_domain", cap)

"""Tests for Andrea's deterministic experience assurance loop."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.experience_assurance import (  # noqa: E402
    _find_telegram_task_id_for_message,
    ExperienceHarness,
    run_experience_assurance,
)
from services.andrea_sync.experience_types import (  # noqa: E402
    ExperienceCheckResult,
    ExperienceObservation,
    ExperienceScenario,
)
from services.andrea_sync.repair_detectors import incident_from_verification_report  # noqa: E402
from services.andrea_sync.store import (  # noqa: E402
    connect,
    get_latest_experience_run,
    migrate,
)


class ExperienceHarnessCorrelationTests(unittest.TestCase):
    def test_local_correlation_finds_task_without_http_fanout(self) -> None:
        with ExperienceHarness() as h:
            server = h.server
            assert server is not None
            h.submit_telegram_update(
                {
                    "update_id": 501,
                    "message": {
                        "text": "Is this OpenClaw?",
                        "message_id": 901,
                        "chat": {"id": 601},
                        "from": {"id": 801},
                    },
                }
            )

            def read(c: object) -> str:
                return _find_telegram_task_id_for_message(
                    c, chat_id=601, message_id=901, scan_limit=50
                )

            tid = server.with_lock(read)
            self.assertTrue(str(tid).startswith("tsk_"))


class AndreaExperienceAssuranceTests(unittest.TestCase):
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

    def test_run_experience_assurance_records_successful_run(self) -> None:
        result = run_experience_assurance(
            self.conn,
            actor="test",
            repo_path=REPO_ROOT,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["verification_report"]["passed"])
        latest = get_latest_experience_run(self.conn)
        self.assertEqual(latest["run_id"], result["run"]["run_id"])
        self.assertEqual(latest["failed_checks"], 0)
        self.assertGreaterEqual(latest["total_checks"], 18)
        self.assertEqual(len(latest["checks"]), latest["total_checks"])
        self.assertIn("score_counts", latest)
        check_ids = {row["check_id"] for row in latest["checks"]}
        self.assertIn("experience_what_is_cursor_direct", check_ids)
        self.assertIn("experience_what_llm_is_answering_direct", check_ids)
        self.assertIn("experience_direct_followups_avoid_history_leak", check_ids)
        self.assertIn("experience_recent_text_messages_via_bluebubbles", check_ids)
        self.assertIn("experience_cursor_primary_calm_completion", check_ids)
        self.assertIn("experience_collaborative_full_visibility_curated", check_ids)
        self.assertIn("experience_masterclass_tri_llm_visibility", check_ids)
        self.assertIn("experience_openclaw_repo_triage_stays_bounded", check_ids)
        self.assertIn("experience_text_messages_from_today_via_bluebubbles", check_ids)
        self.assertIn("experience_structured_lookup_rejects_provider_leak_summary", check_ids)
        self.assertIn("experience_recent_text_shorthand_followup_same_thread", check_ids)
        self.assertIn("experience_collaboration_eval_adaptive_shadow_compare", check_ids)
        self.assertIn("experience_collaboration_eval_operator_approved_promotion_overlay", check_ids)

    def test_run_experience_assurance_bridges_failure_into_repair_cycle(self) -> None:
        def failing_runner(_harness: object, scenario: ExperienceScenario) -> ExperienceCheckResult:
            return ExperienceCheckResult.from_observations(
                scenario,
                [
                    ExperienceObservation(
                        description="scenario passes",
                        expected="no regression",
                        observed="delegated",
                        passed=False,
                        issue_code="overdelegated_meta_question",
                    )
                ],
                output_excerpt="Experience regression points at services/andrea_sync/server.py",
            )

        scenario = ExperienceScenario(
            scenario_id="failing_demo",
            title="Failing demo scenario",
            description="Synthetic failure for repair bridge coverage.",
            category="routing",
            tags=["test"],
            suspected_files=["services/andrea_sync/server.py"],
            runner=failing_runner,
        )
        with mock.patch(
            "services.andrea_sync.experience_assurance.run_incident_repair_cycle",
            return_value={
                "ok": True,
                "resolved": True,
                "incident": {"incident_id": "inc_demo", "status": "resolved"},
            },
        ) as repair_mock:
            result = run_experience_assurance(
                self.conn,
                actor="test",
                repo_path=REPO_ROOT,
                scenarios=[scenario],
                repair_on_fail=True,
            )
        self.assertTrue(result["ok"])
        self.assertFalse(result["verification_report"]["passed"])
        repair_mock.assert_called_once()
        self.assertEqual(
            repair_mock.call_args.kwargs["verification_report"]["checks"][0]["check_id"],
            "experience_failing_demo",
        )
        latest = get_latest_experience_run(self.conn)
        self.assertEqual(
            latest["metadata"]["repair"]["incident"]["incident_id"],
            "inc_demo",
        )

    def test_incident_from_experience_report_uses_experience_source(self) -> None:
        report = {
            "passed": False,
            "summary": "Experience scenario failed.",
            "checks": [
                {
                    "check_id": "experience_openclaw_direct",
                    "label": "Experience: Is this OpenClaw stays direct",
                    "passed": False,
                    "required": True,
                    "output_excerpt": "Regression hit routing behavior.",
                    "suspected_files": ["services/andrea_sync/server.py"],
                }
            ],
        }
        incident = incident_from_verification_report(
            repo_path=REPO_ROOT,
            verification_report=report,
            source_task_id="tsk_experience",
        )
        assert incident is not None
        self.assertEqual(incident.source, "experience_regression")
        self.assertEqual(incident.error_type, "experience_regression")
        self.assertIn("services/andrea_sync/server.py", incident.suspected_files)


if __name__ == "__main__":
    unittest.main()

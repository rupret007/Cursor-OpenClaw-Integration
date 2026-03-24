"""Unit tests for scenario contracts, resolver, and policy hooks."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.andrea_router import AndreaRouteDecision, route_message  # noqa: E402
from services.andrea_sync.approval_policy import evaluate_plan_step_approval  # noqa: E402
from services.andrea_sync.plan_runtime import (  # noqa: E402
    finalize_execute_step_verification,
    gate_delegated_job,
)
from services.andrea_sync.plan_schema import StepKind  # noqa: E402
from services.andrea_sync.scenario_registry import get_contract  # noqa: E402
from services.andrea_sync.scenario_runtime import (  # noqa: E402
    build_scenario_receipt,
    delegate_should_be_blocked,
    lane_allowed_for_scenario,
    resolve_scenario,
    trusted_receipt_allowed,
)
from services.andrea_sync.scenario_schema import (  # noqa: E402
    SUPPORTED_APPROVAL,
    merge_scenario_into_plan_summary,
    scenario_blob_for_job_payload,
)
from services.andrea_sync.schema import EventType, TaskStatus, legal_task_transition  # noqa: E402
from services.andrea_sync.store import (  # noqa: E402
    connect,
    create_task,
    get_active_execution_plan_for_task,
    link_task_principal,
    migrate,
)
from services.andrea_sync.verification_policy import verification_method_for_scenario  # noqa: E402


class TestScenarioRuntime(unittest.TestCase):
    def test_repo_delegate_resolution(self) -> None:
        text = "Have Cursor fix the failing unit test in test_foo.py"
        d = route_message(text, history=[], routing_hint="auto")
        self.assertEqual(d.mode, "delegate")
        r, c = resolve_scenario(text, route_decision=d)
        self.assertEqual(r.scenario_id, "repoHelpVerified")
        self.assertEqual(c.support_level, "supported_auto")

    def test_merge_scenario_into_plan_summary(self) -> None:
        r, c = resolve_scenario(
            "fix repo",
            route_decision=AndreaRouteDecision(
                mode="delegate",
                reason="x",
                delegate_target="cursor",
            ),
        )
        blob = scenario_blob_for_job_payload(r, c)
        merged = merge_scenario_into_plan_summary({"source": "t"}, blob)
        self.assertEqual(merged["source"], "t")
        self.assertEqual(merged["scenario_id"], "repoHelpVerified")
        self.assertEqual(merged["proof_class"], "repo_checks")

    def test_verification_method_for_scenario_human_confirm(self) -> None:
        m = verification_method_for_scenario(
            "cursor",
            StepKind.EXECUTE_DELEGATED.value,
            "human_confirm",
        )
        self.assertEqual(m, "human_confirm")

    def test_approval_policy_scenario_requires_approval(self) -> None:
        pol = evaluate_plan_step_approval(
            lane="cursor",
            step_kind=StepKind.EXECUTE_DELEGATED.value,
            command_type="delegate",
            force_approval=False,
            scenario={
                "support_level": SUPPORTED_APPROVAL,
                "approval_mode": "required",
                "scenario_id": "verificationSensitiveAction",
            },
        )
        self.assertTrue(pol.get("needs_approval"))

    def test_trusted_receipt_gate(self) -> None:
        c = get_contract("verificationSensitiveAction")
        assert c is not None
        self.assertFalse(
            trusted_receipt_allowed(
                c,
                verification_verdict="pass",
                has_required_proof=False,
            )
        )
        self.assertTrue(
            trusted_receipt_allowed(
                c,
                verification_verdict="pass",
                has_required_proof=True,
            )
        )

    def test_build_scenario_receipt_shape(self) -> None:
        rcpt = build_scenario_receipt(
            plan_id="pl_1",
            scenario_id="repoHelpVerified",
            verified=True,
            proof_summary="PR URL recorded; checks passed.",
            next_safe_action="Review the diff.",
        )
        self.assertTrue(rcpt.receipt_id.startswith("rcpt_"))
        self.assertTrue(rcpt.verified)

    def test_legal_transition_scenario_resolved(self) -> None:
        ok, st = legal_task_transition(TaskStatus.COMPLETED, EventType.SCENARIO_RESOLVED)
        self.assertTrue(ok)
        self.assertIsNone(st)

    def test_delegate_block_unsupported(self) -> None:
        c = get_contract("unsupportedOrUnsafeRequest")
        assert c is not None
        self.assertTrue(delegate_should_be_blocked(c, route_mode="delegate"))

    def test_lane_allowed_normalizes_cursor_aliases(self) -> None:
        c = get_contract("repoHelpVerified")
        assert c is not None
        self.assertTrue(lane_allowed_for_scenario(c, "cursor"))
        self.assertTrue(lane_allowed_for_scenario(c, "direct_cursor"))
        self.assertFalse(lane_allowed_for_scenario(c, "direct_assistant"))

    def test_lane_empty_allowed_blocks_all(self) -> None:
        c = get_contract("approvalRequiredOutboundAction")
        assert c is not None
        self.assertFalse(lane_allowed_for_scenario(c, "openclaw_hybrid"))

    def test_status_followup_matches_approval_queue_language(self) -> None:
        text = "What still needs my approval right now?"
        d = route_message(text, history=[], routing_hint="auto")
        r, c = resolve_scenario(text, route_decision=d)
        self.assertEqual(r.scenario_id, "statusFollowupContinue")
        self.assertEqual(r.reason, "status_or_followup_language")

    def test_status_followup_matches_working_on_language(self) -> None:
        for text in (
            "What are we working on right now?",
            "What are we working on with Andrea?",
        ):
            d = route_message(text, history=[], routing_hint="auto")
            r, _c = resolve_scenario(text, route_decision=d)
            self.assertEqual(r.scenario_id, "statusFollowupContinue")
            self.assertEqual(r.reason, "status_or_followup_language")


class TestScenarioPlanPersistence(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = Path(self._tmp.name)
        self.conn = connect(self.db_path)
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_plan_summary_includes_scenario_metadata(self) -> None:
        create_task(self.conn, "tsk_scen_meta", "cli")
        link_task_principal(self.conn, "tsk_scen_meta", "p_x", channel="cli")
        text = "debug the service.py error"
        d = route_message(text, history=[], routing_hint="auto")
        r, c = resolve_scenario(text, route_decision=d)
        job = {
            "kind": "cursor",
            "runner": "cursor",
            "execution_lane": "cursor",
            "scenario": scenario_blob_for_job_payload(r, c),
        }
        gate = gate_delegated_job(
            self.conn,
            "tsk_scen_meta",
            "",
            "p_x",
            text,
            "cursor",
            job,
            [],
        )
        self.assertEqual(gate.mode, "proceed")
        plan = get_active_execution_plan_for_task(self.conn, "tsk_scen_meta")
        assert plan is not None
        summ = plan.get("summary") or {}
        self.assertEqual(summ.get("scenario_id"), "repoHelpVerified")
        self.assertEqual(summ.get("proof_class"), "repo_checks")

    def test_finalize_respects_scenario_proof_class(self) -> None:
        create_task(self.conn, "tsk_scen_ver", "cli")
        link_task_principal(self.conn, "tsk_scen_ver", "p_x", channel="cli")
        text = "verify before done — patch README"
        d = route_message(text, history=[], routing_hint="auto")
        r, c = resolve_scenario(text, route_decision=d)
        job = {
            "kind": "cursor",
            "runner": "cursor",
            "execution_lane": "cursor",
            "scenario": scenario_blob_for_job_payload(r, c),
        }
        gate = gate_delegated_job(
            self.conn,
            "tsk_scen_ver",
            "",
            "p_x",
            text,
            "cursor",
            job,
            [],
        )
        self.assertEqual(gate.mode, "await_approval")
        fv = finalize_execute_step_verification(
            self.conn,
            task_id="tsk_scen_ver",
            plan_id=gate.plan_id,
            execute_step_id=gate.execute_step_id,
            terminal_status="FINISHED",
            pr_url="",
            agent_url="https://agent.example/a",
            lane="cursor",
        )
        self.assertEqual(fv.get("verdict"), "needs_human")
        self.assertFalse(fv.get("should_complete_job", True))
        self.assertEqual(fv.get("receipt_state"), "blocked_trust")

    def test_finalize_repo_weak_pass_still_completes(self) -> None:
        create_task(self.conn, "tsk_scen_repo_weak", "cli")
        link_task_principal(self.conn, "tsk_scen_repo_weak", "p_x", channel="cli")
        text = "fix the flaky test in test_bar.py"
        d = route_message(text, history=[], routing_hint="auto")
        r, c = resolve_scenario(text, route_decision=d)
        job = {
            "kind": "cursor",
            "runner": "cursor",
            "execution_lane": "cursor",
            "scenario": scenario_blob_for_job_payload(r, c),
        }
        gate = gate_delegated_job(
            self.conn,
            "tsk_scen_repo_weak",
            "",
            "p_x",
            text,
            "cursor",
            job,
            [],
        )
        self.assertEqual(gate.mode, "proceed")
        fv = finalize_execute_step_verification(
            self.conn,
            task_id="tsk_scen_repo_weak",
            plan_id=gate.plan_id,
            execute_step_id=gate.execute_step_id,
            terminal_status="FINISHED",
            pr_url="",
            agent_url="https://agent.example/a",
            lane="cursor",
        )
        self.assertEqual(fv.get("verdict"), "needs_human")
        self.assertTrue(fv.get("should_complete_job", False))
        self.assertEqual(fv.get("receipt_state"), "verified_weak")


if __name__ == "__main__":
    unittest.main()

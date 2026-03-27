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

    def test_detects_cursor_recall_continuation_family_leak(self) -> None:
        cap = {
            "raw_reply_text": (
                "I’m not finding a recent Cursor workstream with enough context to safely continue."
            ),
            "user_turn": "What did Cursor say?",
            "turn_plan_domain": "project_status",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap, expect_cursor_substance=True)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_cursor_recall_continuation_family_leak", codes)

    def test_detects_cursor_recall_metadata_led(self) -> None:
        cap = {
            "raw_reply_text": (
                "Delegated execution (tracked): status **running**, lane `direct_cursor`.\n"
                "Where things stand: task status **created**; result: **queued**."
            ),
            "user_turn": "What did Cursor say?",
            "turn_plan_domain": "project_status",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap, expect_cursor_substance=True)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_cursor_recall_metadata_led", codes)

    def test_cursor_recap_lead_does_not_trip_metadata_led_detector(self) -> None:
        cap = {
            "raw_reply_text": (
                "Cursor recap: Drafted the implementation recap and queued the tests.\n"
                "Where things stand: task status **running**; result: **in_progress**."
            ),
            "user_turn": "What did Cursor say?",
            "turn_plan_domain": "project_status",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap, expect_cursor_substance=True)
        codes = {h["issue_code"] for h in hits}
        self.assertNotIn("conversation_cursor_recall_metadata_led", codes)
        self.assertNotIn("conversation_cursor_recall_thin", codes)

    def test_detects_read_summarize_followup_vs_outbound_capability_copy(self) -> None:
        cap = {
            "raw_reply_text": (
                "Yes. BlueBubbles is verified and available here. "
                "For personal outreach, I will draft the message first and wait for your confirmation before sending it."
            ),
            "user_turn": "Can you summarize my texts too?",
            "turn_plan_domain": "personal_agenda",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(
            cap,
            prior_user_turn="Can you pull text messages from BlueBubbles?",
            expect_tool_carryover=True,
        )
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_messaging_read_send_capability_mismatch", codes)

    def test_does_not_flag_followup_miss_when_reason_stays_in_text_lane(self) -> None:
        cap = {
            "raw_reply_text": "I couldn't retrieve messages right now, but I stayed on the recent-text lookup lane.",
            "assistant_reason": "recent_text_messages_unavailable",
            "user_turn": "Can you summarize my texts too?",
            "turn_plan_domain": "personal_agenda",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(
            cap,
            prior_user_turn="Can you pull text messages from BlueBubbles?",
            expect_tool_carryover=True,
        )
        codes = {h["issue_code"] for h in hits}
        self.assertNotIn("conversation_followup_carryover_miss", codes)

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

    def test_detects_recursive_cursor_recap_label(self) -> None:
        cap = {
            "raw_reply_text": "Cursor recap: Cursor recap: fixed the issue cleanly.",
            "user_turn": "What did Cursor say?",
            "turn_plan_domain": "project_status",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap, expect_cursor_substance=True)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_recap_recursion", codes)

    def test_detects_derived_surface_led_cursor_recall(self) -> None:
        cap = {
            "raw_reply_text": (
                "Last assistant update on this task: Cursor shipped a tweak.\n"
                "Where things stand: task status **running**."
            ),
            "user_turn": "What did Cursor say?",
            "turn_plan_domain": "project_status",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap, expect_cursor_substance=True)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_cursor_recall_derived_surface_led", codes)

    def test_detects_cursor_recall_approval_domain_contamination(self) -> None:
        cap = {
            "raw_reply_text": (
                "Cursor recap: I'm not seeing any approval requests waiting on you right now.\n"
                "Recent receipt (status_followup): Status / follow-up reply (goal_runtime_status)."
            ),
            "user_turn": "What did Cursor say?",
            "turn_plan_domain": "project_status",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_cursor_recall_approval_domain_contamination", codes)

    def test_detects_primary_finding_not_surfaced_under_strong_contract(self) -> None:
        cap = {
            "raw_reply_text": "The task is blocked right now and waiting.",
            "user_turn": "What is OpenClaw blocked on?",
            "turn_plan_domain": "project_status",
            "assistant_reason": "semantic_state_blocked_state_reply",
            "semantic_turn_contract": {
                "family": "blocked_state",
                "source": "blocked_state_reply",
                "evidence_strength": 6,
                "primary_finding": "Staging credentials are missing for deploy validation.",
            },
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_primary_finding_not_surfaced", codes)
        self.assertIn("conversation_openclaw_blocker_detail_miss_under_state", codes)

    def test_detects_recap_fallback_under_source_truth_support_lines(self) -> None:
        cap = {
            "raw_reply_text": "I’m not finding a recent clean Cursor result to recap from this thread.",
            "user_turn": "What did Cursor say?",
            "turn_plan_domain": "project_status",
            "assistant_reason": "semantic_state_cursor_continuity_recall",
            "semantic_turn_contract": {
                "family": "cursor_recall",
                "source": "cursor_continuity_recall",
                "evidence_strength": 5,
                "supporting_evidence_lines": ["Recent receipt excerpt: cursor fixed retries and passed smoke checks."],
            },
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_recap_fallback_under_source_truth", codes)

    def test_detects_delegated_outcome_wrapper_over_substance(self) -> None:
        cap = {
            "raw_reply_text": (
                "Andrea:\nI finished your request.\n\nWhat happened:\n- OpenClaw finished processing this task."
            ),
            "rendered_reply_sanitized": (
                "Andrea: I finished your request. What happened: OpenClaw finished processing this task."
            ),
            "user_turn": "what happened in the cursor thread?",
            "turn_plan_domain": "project_status",
            "assistant_reason": "semantic_state_cursor_continuity_recall",
            "semantic_turn_contract": {
                "family": "cursor_recall",
                "source": "cursor_continuity_recall",
                "evidence_strength": 6,
                "primary_finding": "Cursor fixed the failing tests and prepared a PR.",
            },
            "delegated_to_cursor": True,
            "meta_cursor_present": True,
            "meta_openclaw_present": True,
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap, expect_cursor_substance=True)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_delegated_outcome_wrapper_over_substance", codes)

    def test_detects_delegated_role_confusion_when_cursor_omitted(self) -> None:
        cap = {
            "raw_reply_text": (
                "Andrea:\nI finished your request.\n\nWhat happened:\n- OpenClaw finished processing this task."
            ),
            "user_turn": "continue that cursor task",
            "turn_plan_domain": "project_status",
            "delegated_to_cursor": True,
            "meta_cursor_present": True,
            "meta_openclaw_present": True,
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_delegated_role_confusion", codes)

    def test_detects_simple_utility_overcomplicated_surface(self) -> None:
        cap = {
            "raw_reply_text": (
                "I couldn't verify live lookup capability right now, so I can only give a general answer.\n\n"
                "Next options:\n- Retry grounded lookup in a moment when connectivity is stable."
            ),
            "user_turn": "How many gigs are in 1024 mb?",
            "turn_plan_domain": "casual_conversation",
            "assistant_reason": "grounded_research_unavailable",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_simple_utility_overcomplicated_surface", codes)

    def test_detects_agenda_day_plan_lane_miss(self) -> None:
        cap = {
            "raw_reply_text": "I can help with that.",
            "user_turn": "What are my plans today?",
            "turn_plan_domain": "casual_conversation",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_agenda_day_plan_lane_miss", codes)

    def test_detects_stateful_domain_hijack_outside_status_domains(self) -> None:
        cap = {
            "raw_reply_text": "I’m not finding a recent clean Cursor result to recap from this thread.",
            "rendered_reply_sanitized": "Andrea:\nI’m not finding a recent clean Cursor result to recap from this thread.",
            "user_turn": "What's on the agenda today?",
            "turn_plan_domain": "personal_agenda",
            "turn_plan_continuity_focus": "none",
            "assistant_reason": "semantic_state_cursor_continuity_recall",
            "assistant_semantic_selection": {"source": "cursor_continuity_recall"},
            "expected_answer_family": "general_status",
            "expected_answer_sources": ["goal_status", "goal_continuity"],
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_stateful_domain_hijack", codes)
        self.assertIn("conversation_semantic_source_family_mismatch", codes)
        self.assertIn("conversation_semantic_contract_missing", codes)

    def test_detects_goal_runtime_status_hijack_on_opinion_turn(self) -> None:
        cap = {
            "raw_reply_text": "Goal `g1` status: running with pending checks.",
            "rendered_reply_sanitized": "Andrea:\nGoal `g1` status: running with pending checks.",
            "user_turn": "What do you think about that?",
            "turn_plan_domain": "opinion_reflection",
            "assistant_reason": "goal_runtime_status",
            "assistant_semantic_selection": {},
            "expected_answer_family": "general_status",
            "expected_answer_sources": ["goal_status", "goal_continuity"],
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_stateful_domain_hijack", codes)

    def test_detects_openclaw_role_carryover_hijack_on_casual_turn(self) -> None:
        cap = {
            "raw_reply_text": (
                "The main blocker right now is: I hit an internal collaboration limitation while trying to pass work between reasoning lanes."
            ),
            "rendered_reply_sanitized": (
                "Andrea: The main blocker right now is: I hit an internal collaboration limitation while trying to pass work between reasoning lanes."
            ),
            "user_turn": "Hi",
            "turn_plan_domain": "project_status",
            "assistant_reason": "semantic_state_blocked_state_reply",
            "meta_openclaw_present": True,
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_openclaw_role_carryover_hijack", codes)

    def test_detects_openclaw_identity_state_hijack(self) -> None:
        cap = {
            "raw_reply_text": (
                "The main blocker right now is: I hit an internal collaboration limitation while trying to pass work between reasoning lanes."
            ),
            "rendered_reply_sanitized": (
                "Andrea: The main blocker right now is: I hit an internal collaboration limitation while trying to pass work between reasoning lanes."
            ),
            "user_turn": "Is this OpenClaw?",
            "turn_plan_domain": "project_status",
            "assistant_reason": "semantic_state_blocked_state_reply",
            "meta_openclaw_present": True,
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_openclaw_identity_state_hijack", codes)

    def test_detects_openclaw_collab_scaffold_on_continue_turn(self) -> None:
        cap = {
            "raw_reply_text": (
                "The main blocker right now is: I hit an internal collaboration limitation while trying to pass work between reasoning lanes."
            ),
            "rendered_reply_sanitized": (
                "Andrea: The main blocker right now is: I hit an internal collaboration limitation while trying to pass work between reasoning lanes."
            ),
            "user_turn": "continue that Cursor task",
            "turn_plan_domain": "project_status",
            "assistant_reason": "semantic_state_blocked_state_reply",
            "meta_openclaw_present": True,
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_openclaw_extraneous_collab_scaffold", codes)

    def test_detects_stateful_hijack_on_technical_guidance_turn(self) -> None:
        cap = {
            "raw_reply_text": "The main blocker right now is: cross-model handoff stalled.",
            "rendered_reply_sanitized": "Andrea: The main blocker right now is: cross-model handoff stalled.",
            "user_turn": "What does this timeout error usually mean?",
            "turn_plan_domain": "technical_guidance",
            "assistant_reason": "semantic_state_blocked_state_reply",
            "assistant_semantic_selection": {"source": "blocked_state_reply"},
            "expected_answer_family": "grounded_research",
            "expected_answer_sources": ["grounded_research_lookup"],
            "meta_openclaw_present": True,
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_stateful_domain_hijack", codes)
        self.assertIn("conversation_semantic_source_family_mismatch", codes)

    def test_detects_openclaw_blocker_fallback_under_state(self) -> None:
        cap = {
            "raw_reply_text": (
                "I’m not finding a recent Cursor workstream with enough context to safely continue, "
                "so I’d need to start a new one from your latest instruction."
            ),
            "rendered_reply_sanitized": (
                "I’m not finding a recent Cursor workstream with enough context to safely continue, "
                "so I’d need to start a new one from your latest instruction."
            ),
            "user_turn": "What is OpenClaw blocked on?",
            "turn_plan_domain": "project_status",
            "assistant_reason": "semantic_state_cursor_heavy_lift_context",
            "meta_openclaw_present": True,
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_openclaw_blocker_fallback_under_state", codes)

    def test_detects_openclaw_blocker_vague_under_state(self) -> None:
        cap = {
            "raw_reply_text": "OpenClaw is in a waiting state right now.",
            "rendered_reply_sanitized": "OpenClaw is in a waiting state right now.",
            "user_turn": "What is OpenClaw blocked on?",
            "turn_plan_domain": "project_status",
            "assistant_reason": "semantic_state_goal_status",
            "meta_openclaw_present": True,
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_openclaw_blocker_vague_under_state", codes)

    def test_detects_anaphoric_continue_thin(self) -> None:
        cap = {
            "raw_reply_text": "I do not see active tracked work right now.",
            "rendered_reply_sanitized": "I do not see active tracked work right now.",
            "user_turn": "continue that",
            "turn_plan_domain": "project_status",
            "assistant_reason": "semantic_state_cursor_heavy_lift_context",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_cursor_continue_anaphoric_thin", codes)

    def test_detects_grounded_research_contract_missing_evidence(self) -> None:
        cap = {
            "raw_reply_text": "Timeout errors are often transient; retries can help.",
            "rendered_reply_sanitized": "Andrea: Timeout errors are often transient; retries can help.",
            "user_turn": "What does this timeout error usually mean?",
            "turn_plan_domain": "technical_guidance",
            "assistant_reason": "grounded_research_lookup",
            "assistant_grounded_research_selection": {
                "source": "grounded_research_lookup",
                "family": "grounded_research",
                "evidence_strength": 5,
                "required_anchors": ["timeout", "retries"],
                "evidence_lines": [],
            },
            "expected_answer_family": "grounded_research",
            "expected_answer_sources": ["grounded_research_lookup"],
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_research_contract_missing", codes)

    def test_detects_generic_fallback_despite_grounded_lookup(self) -> None:
        cap = {
            "raw_reply_text": "I can help with that. Tell me more and I can refine it.",
            "rendered_reply_sanitized": "Andrea: I can help with that. Tell me more and I can refine it.",
            "user_turn": "How should I configure retries for timeout failures?",
            "turn_plan_domain": "technical_guidance",
            "assistant_reason": "grounded_research_lookup",
            "assistant_grounded_research_selection": {
                "source": "grounded_research_lookup",
                "family": "grounded_research",
                "evidence_strength": 6,
                "required_anchors": [],
                "evidence_lines": [
                    "Timeout failures are often transient.",
                    "Retries with bounded backoff are a common mitigation.",
                ],
            },
            "expected_answer_family": "grounded_research",
            "expected_answer_sources": ["grounded_research_lookup"],
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_generic_fallback_despite_lookup", codes)

    def test_detects_substantive_turn_social_collapse(self) -> None:
        cap = {
            "raw_reply_text": "Pretty good, thanks for asking. How are you doing?",
            "rendered_reply_sanitized": "Andrea: Pretty good, thanks for asking. How are you doing?",
            "user_turn": "What does this timeout error usually mean?",
            "turn_plan_domain": "casual_conversation",
            "assistant_route": "direct",
            "assistant_reason": "greeting_or_social",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_substantive_turn_social_collapse", codes)

    def test_detects_unnecessary_heavy_lift_escalation(self) -> None:
        cap = {
            "raw_reply_text": "Queued for Cursor execution.",
            "user_turn": "What does this timeout error usually mean?",
            "turn_plan_domain": "technical_guidance",
            "assistant_route": "delegate",
            "assistant_reason": "technical_or_repo_request",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap, forbid_unnecessary_delegate=True)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_unnecessary_heavy_lift_escalation", codes)

    def test_detects_fallback_shaped_rendered_reply_under_strong_contract_evidence(self) -> None:
        cap = {
            "raw_reply_text": "I’m not finding a recent clean Cursor result to recap from this thread.",
            "rendered_reply_sanitized": "Andrea:\nI’m not finding a recent clean Cursor result to recap from this thread.",
            "user_turn": "What did Cursor say?",
            "turn_plan_domain": "project_status",
            "assistant_reason": "semantic_state_cursor_continuity_recall",
            "assistant_semantic_selection": {"source": "cursor_continuity_recall"},
            "semantic_turn_contract": {
                "family": "cursor_recall",
                "source": "cursor_continuity_recall",
                "allowed_sources": ["cursor_continuity_recall"],
                "evidence_strength": 6,
            },
            "expected_answer_family": "cursor_recall",
            "expected_answer_sources": ["cursor_continuity_recall"],
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap, expect_cursor_substance=True)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_fallback_shaped_under_contract_evidence", codes)

    def test_detects_missing_next_step_guidance_for_semantic_contract(self) -> None:
        cap = {
            "raw_reply_text": "Adjusted the handler path for retries only.",
            "rendered_reply_sanitized": "Andrea:\nAdjusted the handler path for retries only.",
            "user_turn": "What did Cursor say?",
            "turn_plan_domain": "project_status",
            "assistant_reason": "semantic_state_cursor_continuity_recall",
            "assistant_semantic_selection": {"source": "cursor_continuity_recall"},
            "semantic_turn_contract": {
                "family": "cursor_recall",
                "source": "cursor_continuity_recall",
                "allowed_sources": ["cursor_continuity_recall"],
                "evidence_strength": 3,
                "answer_mode": "partial_evidence_helpful_answer",
                "next_step_options": [
                    "Re-send your latest instruction so I can run a fresh Cursor pass with clean tracked context.",
                    "Name a rough time window if you need a tighter recap anchor.",
                ],
            },
            "expected_answer_family": "cursor_recall",
            "expected_answer_sources": ["cursor_continuity_recall"],
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_missing_next_step_guidance", codes)

    def test_detects_partial_evidence_not_exploited(self) -> None:
        cap = {
            "raw_reply_text": "I’m not finding a recent clean Cursor result to recap from this thread.",
            "rendered_reply_sanitized": "Andrea:\nI’m not finding a recent clean Cursor result to recap from this thread.",
            "user_turn": "What did Cursor say?",
            "turn_plan_domain": "project_status",
            "assistant_reason": "semantic_state_cursor_continuity_recall",
            "assistant_semantic_selection": {"source": "cursor_continuity_recall"},
            "semantic_turn_contract": {
                "family": "cursor_recall",
                "source": "cursor_continuity_recall",
                "evidence_strength": 4,
                "answer_mode": "partial_evidence_helpful_answer",
            },
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_partial_evidence_not_exploited", codes)

    def test_detects_stateful_brevity_contract_violation(self) -> None:
        long_body = " ".join([f"word{i}" for i in range(220)])
        cap = {
            "raw_reply_text": long_body,
            "rendered_reply_sanitized": long_body,
            "user_turn": "What did Cursor say?",
            "turn_plan_domain": "project_status",
            "assistant_reason": "semantic_state_cursor_continuity_recall",
            "assistant_semantic_selection": {"source": "cursor_continuity_recall"},
            "semantic_turn_contract": {
                "family": "cursor_recall",
                "source": "cursor_continuity_recall",
                "answer_mode": "strong_evidence_answer",
                "brevity_max_words_soft": 115,
                "evidence_strength": 7,
            },
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_stateful_exceeds_brevity_contract", codes)

    def test_detects_redundant_where_block_on_cursor_recall(self) -> None:
        cap = {
            "raw_reply_text": "Cursor recap: fixed retries.\n\nWhere things stand: task status **running**.",
            "rendered_reply_sanitized": "Cursor recap: fixed retries.\n\nWhere things stand: task status **running**.",
            "user_turn": "What did Cursor say?",
            "turn_plan_domain": "project_status",
            "assistant_reason": "semantic_state_cursor_continuity_recall",
            "assistant_semantic_selection": {"source": "cursor_continuity_recall"},
            "semantic_turn_contract": {
                "family": "cursor_recall",
                "source": "cursor_continuity_recall",
                "utility_goal": "concise_grounded_summary",
                "answer_mode": "strong_evidence_answer",
                "evidence_strength": 6,
            },
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap, expect_cursor_substance=True)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_cursor_recall_redundant_status_scaffold", codes)

    def test_detects_grounded_error_query_with_generic_next_steps(self) -> None:
        cap = {
            "raw_reply_text": "Partial note about errors.\n\nNext options:\n• Retry grounded lookup.",
            "rendered_reply_sanitized": "Partial note about errors.\n\nNext options:\n• Retry grounded lookup.",
            "user_turn": "What does this error mean?",
            "assistant_reason": "grounded_research_lookup",
            "assistant_grounded_research_selection": {
                "source": "grounded_research_lookup",
                "family": "grounded_research",
                "query": "What does this error mean?",
                "answer_mode": "partial_evidence_helpful_answer",
                "next_step_options": [
                    "Retry grounded lookup if you need fresher or broader evidence.",
                    "Narrow to the exact tool, error string, or version you care about.",
                ],
                "evidence_lines": ["Errors often need the exact message to diagnose."],
                "evidence_strength": 3,
            },
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_grounded_error_query_generic_next_steps", codes)

    def test_skips_grounded_error_detector_when_options_are_error_specific(self) -> None:
        cap = {
            "raw_reply_text": "Partial note.\n\nNext options:\n• Paste the full error text.",
            "rendered_reply_sanitized": "Partial note.\n\nNext options:\n• Paste the full error text.",
            "user_turn": "What does this error mean?",
            "assistant_reason": "grounded_research_lookup",
            "assistant_grounded_research_selection": {
                "source": "grounded_research_lookup",
                "family": "grounded_research",
                "query": "What does this error mean?",
                "answer_mode": "partial_evidence_helpful_answer",
                "next_step_options": [
                    "Paste the full error text or traceback you’re seeing.",
                    "Narrow to the exact tool, error string, or version you care about.",
                ],
                "evidence_lines": ["Errors often need the exact message to diagnose."],
                "evidence_strength": 3,
            },
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertNotIn("conversation_grounded_error_query_generic_next_steps", codes)

    def test_detects_stateful_specific_guidance_miss(self) -> None:
        cap = {
            "raw_reply_text": "Cursor recap: partial result.\n\nNext options:\n• Retry later.\n• Ask a narrower question.",
            "rendered_reply_sanitized": "Cursor recap: partial result.\n\nNext options:\n• Retry later.\n• Ask a narrower question.",
            "user_turn": "What did Cursor say about the traceback?",
            "turn_plan_domain": "project_status",
            "assistant_reason": "semantic_state_cursor_continuity_recall",
            "assistant_semantic_selection": {"source": "cursor_continuity_recall"},
            "semantic_turn_contract": {
                "family": "cursor_recall",
                "source": "cursor_continuity_recall",
                "answer_mode": "partial_evidence_helpful_answer",
                "next_step_options": [
                    "Paste the full error text or traceback you’re seeing.",
                    "Include the exact command or action that triggers it.",
                ],
                "evidence_strength": 4,
            },
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_stateful_specific_guidance_miss", codes)

    def test_detects_grounded_specific_guidance_miss(self) -> None:
        cap = {
            "raw_reply_text": "Partial setup note.\n\nNext options:\n• Retry grounded lookup.\n• Ask a narrower question.",
            "rendered_reply_sanitized": "Partial setup note.\n\nNext options:\n• Retry grounded lookup.\n• Ask a narrower question.",
            "user_turn": "Why is this config failing?",
            "assistant_reason": "grounded_research_lookup",
            "assistant_grounded_research_selection": {
                "source": "grounded_research_lookup",
                "family": "grounded_research",
                "query": "Why is this config failing?",
                "guidance_class": "configuration_setup",
                "answer_mode": "partial_evidence_helpful_answer",
                "next_step_options": [
                    "Share the relevant config snippet or command flags (redact secrets).",
                    "Name the environment where this runs (local, container, CI, or cloud).",
                ],
                "evidence_lines": ["Config mismatch can fail when env variables differ by runtime."],
                "evidence_strength": 4,
            },
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_grounded_specific_guidance_miss", codes)

    def test_detects_unavailable_technical_generic_retry_guidance(self) -> None:
        cap = {
            "raw_reply_text": (
                "Lookup is unavailable right now.\n\nNext options:\n"
                "• Retry grounded lookup in a moment when connectivity is stable.\n"
                "• Paste the warning details."
            ),
            "rendered_reply_sanitized": (
                "Lookup is unavailable right now.\n\nNext options:\n"
                "• Retry grounded lookup in a moment when connectivity is stable.\n"
                "• Paste the warning details."
            ),
            "user_turn": "What does this SSL certificate error mean?",
            "assistant_reason": "grounded_research_unavailable",
            "assistant_grounded_research_selection": {
                "source": "grounded_research_lookup",
                "family": "grounded_research",
                "query": "What does this SSL certificate error mean?",
                "answer_mode": "truthful_fallback_with_next_steps",
                "fallback_policy": "truthful_unavailable_lookup_fallback",
                "guidance_class": "certificate_tls",
                "next_step_options": [
                    "Retry grounded lookup in a moment when connectivity is stable.",
                    "Paste the exact warning details.",
                ],
                "evidence_lines": [],
                "evidence_strength": 0,
            },
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_grounded_unavailable_generic_retry_guidance", codes)

    def test_detects_grounded_multiline_raw_carrythrough(self) -> None:
        raw = (
            "Hostname mismatches commonly trigger certificate warnings in browsers. "
            "Incomplete intermediate chains can produce trust errors until the full chain is installed. "
            "Expiry warnings appear when the leaf certificate is past its notAfter date."
        )
        cap = {
            "raw_reply_text": raw,
            "rendered_reply_sanitized": raw,
            "user_turn": "Why am I seeing an SSL certificate warning in the browser?",
            "assistant_reason": "grounded_research_lookup",
            "assistant_grounded_research_selection": {
                "source": "grounded_research_lookup",
                "family": "grounded_research",
                "query": "Why am I seeing an SSL certificate warning in the browser?",
                "answer_mode": "strong_evidence_answer",
                "guidance_class": "certificate_tls",
                "next_step_options": [],
                "evidence_lines": [
                    "Hostname mismatches commonly trigger certificate warnings in browsers.",
                    "Incomplete intermediate chains can produce trust errors until the full chain is installed.",
                    "Expiry warnings appear when the leaf certificate is past its notAfter date.",
                ],
                "evidence_strength": 9,
            },
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_grounded_multiline_raw_carrythrough", codes)

    def test_detects_grounded_strong_brevity_violation(self) -> None:
        long_body = " ".join([f"fact{i}" for i in range(200)])
        cap = {
            "raw_reply_text": long_body,
            "rendered_reply_sanitized": long_body,
            "user_turn": "What does PEP 8 recommend for line length?",
            "turn_plan_domain": "technical_guidance",
            "assistant_reason": "grounded_research_lookup",
            "assistant_grounded_research_selection": {
                "source": "grounded_research_lookup",
                "family": "grounded_research",
                "answer_mode": "strong_evidence_answer",
                "brevity_max_words_soft": 115,
                "evidence_strength": 8,
                "evidence_lines": ["PEP 8 suggests 79 characters for code."],
            },
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertIn("conversation_grounded_lookup_exceeds_brevity_contract", codes)

    def test_grounded_unavailable_contract_skips_missing_evidence_line_failure(self) -> None:
        cap = {
            "raw_reply_text": "Could not verify lookup.",
            "rendered_reply_sanitized": "Andrea: Could not verify lookup.",
            "user_turn": "What does this error mean?",
            "assistant_reason": "grounded_research_unavailable",
            "assistant_grounded_research_selection": {
                "source": "grounded_research_lookup",
                "family": "grounded_research",
                "evidence_lines": [],
                "fallback_policy": "truthful_unavailable_lookup_fallback",
            },
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertNotIn("conversation_research_contract_missing", codes)

    def test_clean_cursor_recall_fallback_not_flagged_as_approval_contamination(self) -> None:
        cap = {
            "raw_reply_text": "I'm not finding a recent clean Cursor result to recap from this thread.",
            "user_turn": "What did Cursor say?",
            "turn_plan_domain": "project_status",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertNotIn("conversation_cursor_recall_approval_domain_contamination", codes)

    def test_no_derived_surface_flag_when_latest_useful_result_present(self) -> None:
        cap = {
            "raw_reply_text": (
                "Cursor recap: shipped the fix.\n"
                "Latest useful result: shipped the fix with tests."
            ),
            "user_turn": "What did Cursor say?",
            "turn_plan_domain": "project_status",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap, expect_cursor_substance=True)
        codes = {h["issue_code"] for h in hits}
        self.assertNotIn("conversation_cursor_recall_derived_surface_led", codes)

    def test_approval_inventory_reply_does_not_trip_false_completion(self) -> None:
        cap = {
            "raw_reply_text": "I'm not seeing any approval requests waiting on you right now.",
            "user_turn": "What still needs my approval?",
            "turn_plan_domain": "approval_state",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertNotIn("conversation_false_completion", codes)

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

    def test_attach_report_clusters_weak_pass_quality_checks(self) -> None:
        meta: dict = {}
        sc = ExperienceScenario(
            scenario_id="conversation_core::weak",
            title="weak",
            description="d",
            category="conversation",
            runner=lambda h, s: ExperienceCheckResult.from_observations(s, []),
        )
        weak = ExperienceCheckResult.from_observations(
            sc,
            [
                ExperienceObservation(
                    description="warn",
                    expected="no medium warnings",
                    observed="cursor_recall_thin",
                    passed=True,
                    severity="medium",
                )
            ],
            metadata={
                "failure_families": ["cursor_recall_failure"],
                "quality_state": "weak_pass",
            },
        )
        attach_conversation_eval_report(meta, [weak], prepare_fix_brief=True)
        clusters = meta.get("conversation_failure_clusters") or []
        self.assertTrue(clusters)


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
        self.assertIn("conversation_core::simple_math_direct", ids)
        self.assertIn("conversation_core::simple_conversion_direct", ids)
        self.assertIn("conversation_core::plans_today", ids)
        self.assertIn("conversation_core::weather_current_conditions", ids)
        self.assertIn("conversation_core::technical_guidance_timeout", ids)
        self.assertIn("conversation_core::short_technical_question_not_social", ids)
        self.assertIn("conversation_core::technical_guidance_lookup_unavailable_next_steps", ids)
        self.assertIn("conversation_core::technical_guidance_multiline_partial_evidence", ids)
        self.assertIn("conversation_core::cursor_recall_thin_thread_next_steps", ids)

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

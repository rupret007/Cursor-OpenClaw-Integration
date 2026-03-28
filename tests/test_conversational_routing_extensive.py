"""Extensive unit tests for conversational / anaphoric routing (direct assistant path)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.andrea_router import (  # noqa: E402
    build_direct_reply,
    classify_route,
    route_message,
)
from services.andrea_sync.conversation_eval import run_deterministic_detectors  # noqa: E402
from services.andrea_sync.turn_intelligence import (  # noqa: E402
    arbitrate_answer_lane,
    build_direct_answer_policy,
    build_turn_plan,
    is_lightweight_conversational_question,
    is_substantive_non_social_question,
)

# Phrases that must match _BARE_DIALOGUE_CLARIFICATION_RE (full-line).
_BARE_CLARIFY_PHRASES: tuple[str, ...] = (
    "Which is what?",
    "which is what",
    "What is that?",
    "what's that?",
    "What’s that?",  # smart apostrophe
    "What does that mean?",
    "What do you mean?",
    "which one?",
    "huh?",
    "Huh!",
    "come again?",
    "  Come again?  ",
)


class TestBareDialogueRoutingMatrix(unittest.TestCase):
    """Cycle 1: bare anaphoric clarifications stay direct and avoid generic fallback."""

    def test_all_bare_phrases_are_lightweight_not_substantive(self) -> None:
        for text in _BARE_CLARIFY_PHRASES:
            with self.subTest(text=text):
                self.assertTrue(
                    is_lightweight_conversational_question(text),
                    msg=f"expected lightweight: {text!r}",
                )
                self.assertFalse(
                    is_substantive_non_social_question(text),
                    msg=f"expected not substantive: {text!r}",
                )

    def test_classify_route_bare_clarification_reason(self) -> None:
        for text in ("Which is what?", "what's that?", "huh?"):
            with self.subTest(text=text):
                mode, reason, _target, _collab = classify_route(text)
                self.assertEqual(mode, "direct")
                self.assertEqual(reason, "lightweight_followup_direct")

    def test_route_message_carries_numeric_history(self) -> None:
        history = [{"role": "assistant", "content": "88."}]
        d = route_message("Which is what?", history=history)
        self.assertEqual(d.mode, "direct")
        self.assertEqual(d.reason, "lightweight_followup_direct")
        self.assertIn("88", d.reply_text)
        self.assertNotIn("say a bit more about what you want", d.reply_text.lower())

    def test_build_direct_reply_no_generic_fallback_for_bare_clarify(self) -> None:
        reply = build_direct_reply(
            "What's that?",
            [{"role": "assistant", "content": "That was the deploy checklist summary."}],
        )
        low = reply.lower()
        self.assertNotIn("say a bit more about what you want", low)
        self.assertIn("deploy checklist", low)


class TestBareDialogueTurnPlanAndLane(unittest.TestCase):
    """Cycle 2: turn plan + answer lane + policy for bare clarifications."""

    def test_turn_plan_lane_lightweight_direct_lookup_ineligible(self) -> None:
        text = "Which is what?"
        plan = build_turn_plan(
            text,
            scenario_id="mixedResourceGoal",
            projection_has_continuity_state=True,
        )
        policy = build_direct_answer_policy(
            text,
            scenario_id="mixedResourceGoal",
            turn_plan=plan,
        )
        self.assertFalse(policy.lookup_eligible)
        lane = arbitrate_answer_lane(text=text, turn_plan=plan, direct_policy=policy)
        self.assertEqual(lane.lane, "lightweight_direct")
        self.assertEqual(lane.reason, "lightweight_conversational")

    def test_bare_clarify_subtests_match_per_phrase(self) -> None:
        for text in _BARE_CLARIFY_PHRASES:
            with self.subTest(text=text):
                plan = build_turn_plan(
                    text.strip(),
                    scenario_id="mixedResourceGoal",
                    projection_has_continuity_state=True,
                )
                policy = build_direct_answer_policy(
                    text.strip(),
                    scenario_id="mixedResourceGoal",
                    turn_plan=plan,
                )
                self.assertFalse(policy.lookup_eligible)


class TestLightweightFollowupClassifyRoute(unittest.TestCase):
    """Cycle 3: other lightweight conversational questions get lightweight_followup_direct."""

    def test_meaning_of_life_classifies_lightweight_followup(self) -> None:
        mode, reason, _t, _c = classify_route("What is the meaning of life?")
        self.assertEqual(mode, "direct")
        self.assertEqual(reason, "lightweight_followup_direct")

    def test_opinion_short_classifies_lightweight_followup(self) -> None:
        mode, reason, _t, _c = classify_route("What do you think about that?")
        self.assertEqual(mode, "direct")
        self.assertEqual(reason, "lightweight_followup_direct")

    def test_andrea_hint_still_explicit_mention_for_bare_clarify(self) -> None:
        mode, reason, _t, _c = classify_route(
            "Which is what?",
            routing_hint="andrea",
        )
        self.assertEqual(mode, "direct")
        self.assertEqual(reason, "explicit_andrea_mention")


class TestBareDialoguePunctuationAndNegatives(unittest.TestCase):
    """Cycle 2: punctuation variants and non-matching substantive phrasing."""

    def test_which_is_what_without_question_mark(self) -> None:
        self.assertTrue(is_lightweight_conversational_question("which is what"))

    def test_huh_with_ellipsis_unicode(self) -> None:
        self.assertTrue(is_lightweight_conversational_question("huh…"))

    def test_which_library_is_not_bare_clarification(self) -> None:
        text = "Which library should I use for async Python?"
        self.assertFalse(
            is_lightweight_conversational_question(text),
            msg="substantive technical question must not collapse to lightweight",
        )
        self.assertTrue(is_substantive_non_social_question(text))

    def test_route_message_prefers_latest_substantive_assistant_line(self) -> None:
        history = [
            {"role": "user", "content": "What is 6*7?"},
            {"role": "assistant", "content": "42."},
        ]
        d = route_message("Come again?", history=history)
        self.assertIn("42", d.reply_text)


class TestBareClarificationDetectorHygiene(unittest.TestCase):
    """Cycle 4: good bare-clarification surfaces should not trip generic-fallback detector."""

    def test_heuristic_clarification_reply_not_generic_fallback_leak(self) -> None:
        reply = build_direct_reply(
            "Which is what?",
            [{"role": "assistant", "content": "42."}],
        )
        cap = {
            "raw_reply_text": reply,
            "rendered_reply_sanitized": reply,
            "user_turn": "Which is what?",
            "turn_plan_domain": "casual_conversation",
            "leak_internal_runtime": False,
            "leak_sanitized_empty": False,
        }
        hits = run_deterministic_detectors(cap)
        codes = {h["issue_code"] for h in hits}
        self.assertNotIn("conversation_generic_fallback_leak", codes)

    def test_empty_history_still_avoids_say_more_fallback(self) -> None:
        reply = build_direct_reply("huh?", history=[])
        low = reply.lower()
        self.assertNotIn("say a bit more about what you want", low)
        self.assertIn("just above", low)

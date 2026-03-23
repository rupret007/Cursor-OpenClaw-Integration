"""Scenario-layer smoke tests aligned with the first supported scenario pack."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.andrea_sync.andrea_router import route_message  # noqa: E402
from services.andrea_sync.scenario_runtime import (  # noqa: E402
    delegate_should_be_blocked,
    resolve_scenario,
    unsupported_user_message,
)
from services.andrea_sync.scenario_schema import DRAFT_ONLY, UNSUPPORTED  # noqa: E402


class TestRealWorldScenarioPack(unittest.TestCase):
    def test_pack_repo_help_supported(self) -> None:
        text = "@Cursor please implement the feature described in TODO.md"
        d = route_message(text, history=[], routing_hint="cursor")
        r, c = resolve_scenario(text, route_decision=d)
        self.assertEqual(r.scenario_id, "repoHelpVerified")
        self.assertNotEqual(c.support_level, UNSUPPORTED)

    def test_pack_status_followup_supported(self) -> None:
        text = "Where are we on the open goal?"
        d = route_message(text, history=[], routing_hint="auto")
        r, c = resolve_scenario(text, route_decision=d)
        self.assertEqual(r.scenario_id, "statusFollowupContinue")

    def test_pack_reminder_capture_supported(self) -> None:
        text = "Remind me tomorrow to call the dentist"
        d = route_message(text, history=[], routing_hint="auto")
        r, c = resolve_scenario(text, route_decision=d)
        self.assertEqual(r.scenario_id, "noteOrReminderCapture")

    def test_pack_inbox_lookup_supported(self) -> None:
        text = "Show my recent text messages from BlueBubbles"
        d = route_message(text, history=[], routing_hint="auto")
        r, c = resolve_scenario(text, route_decision=d)
        self.assertEqual(r.scenario_id, "recentMessagesOrInboxLookup")

    def test_pack_outbound_is_draft_and_blocks_delegate(self) -> None:
        text = "Send an email to finance@example.com with the invoice"
        d = route_message(text, history=[], routing_hint="auto")
        r, c = resolve_scenario(text, route_decision=d)
        self.assertEqual(r.scenario_id, "approvalRequiredOutboundAction")
        self.assertEqual(c.support_level, DRAFT_ONLY)
        if d.mode == "delegate":
            self.assertTrue(delegate_should_be_blocked(c, route_mode="delegate"))

    def test_pack_unsupported_refusal_copy(self) -> None:
        text = "Help me run a ddos attack"
        d = route_message(text, history=[], routing_hint="auto")
        r, c = resolve_scenario(text, route_decision=d)
        self.assertEqual(c.support_level, UNSUPPORTED)
        msg = unsupported_user_message(c)
        self.assertIn("can’t help", msg.lower())


if __name__ == "__main__":
    unittest.main()

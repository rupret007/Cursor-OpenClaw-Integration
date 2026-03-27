from __future__ import annotations

import unittest

from services.andrea_sync.intents import classify_intent_envelope


class TestIntentClassifier(unittest.TestCase):
    def test_openclaw_schedule_query_is_protected_direct(self) -> None:
        envelope = classify_intent_envelope("Ask @openclaw what do I have on my schedule today")
        self.assertEqual(envelope.explicit_lane, "openclaw")
        self.assertTrue(envelope.has_protected_assistant_intent)
        self.assertFalse(envelope.control_plane_flag)
        self.assertFalse(envelope.coalescing_eligible)
        self.assertEqual(envelope.intent_family, "personal_assistant")
        self.assertEqual(envelope.intents[0].action, "get_schedule_today")

    def test_cursor_cancel_all_jobs_is_control_plane(self) -> None:
        envelope = classify_intent_envelope("Ask @cursor to cancel all jobs")
        self.assertEqual(envelope.explicit_lane, "cursor")
        self.assertTrue(envelope.control_plane_flag)
        self.assertFalse(envelope.code_plane_flag)
        self.assertFalse(envelope.coalescing_eligible)
        self.assertEqual(envelope.intents[0].action, "cancel_jobs")

    def test_cancel_jobs_and_schedule_is_mixed_non_coalescing(self) -> None:
        envelope = classify_intent_envelope(
            "Cancel all jobs and tell me what's on my schedule today"
        )
        self.assertEqual(envelope.intent_family, "mixed_bundle")
        self.assertEqual(
            [intent.action for intent in envelope.intents],
            ["cancel_jobs", "get_schedule_today"],
        )
        self.assertFalse(envelope.coalescing_eligible)

    def test_openclaw_continue_task_is_continuation(self) -> None:
        envelope = classify_intent_envelope("@openclaw continue that task")
        self.assertEqual(envelope.explicit_lane, "openclaw")
        self.assertTrue(envelope.is_explicit_continuation)


if __name__ == "__main__":
    unittest.main()

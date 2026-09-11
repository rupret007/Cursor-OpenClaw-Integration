import importlib.util
import pathlib
import unittest


COMMON_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "cursor_api_common.py"
SPEC = importlib.util.spec_from_file_location("cursor_api_common", COMMON_PATH)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MOD)  # type: ignore[attr-defined]


class CursorApiCommonTests(unittest.TestCase):
    def test_validate_agent_id_ok(self):
        MOD.validate_agent_id("bc-abc123")
        MOD.validate_agent_id("agent_01")

    def test_validate_agent_id_rejects_pathy(self):
        with self.assertRaises(ValueError):
            MOD.validate_agent_id("../v0/me")
        with self.assertRaises(ValueError):
            MOD.validate_agent_id("")

    def test_parse_json_response_body_object(self):
        d = MOD.parse_json_response_body('{"a": 1}')
        self.assertEqual(d.get("a"), 1)

    def test_parse_json_response_body_non_json(self):
        d = MOD.parse_json_response_body("<html>oops</html>")
        self.assertTrue(d.get("_non_json_response"))

    def test_argv_has_json_flag(self):
        self.assertTrue(MOD.argv_has_json_flag(["x", "y", "--json"]))
        self.assertFalse(MOD.argv_has_json_flag(["x", "y"]))

    def test_encode_request_json_unicode(self):
        raw = MOD.encode_request_json({"prompt": {"text": "café 日本語"}})
        self.assertIn("café".encode("utf-8"), raw)

    def test_encode_request_json_rejects_non_serializable(self):
        with self.assertRaises(ValueError):
            MOD.encode_request_json({"x": object()})  # type: ignore[arg-type]

    def test_assert_no_newlines_or_nul(self):
        MOD.assert_no_newlines_or_nul("feature/ok", "--branch")
        with self.assertRaises(ValueError):
            MOD.assert_no_newlines_or_nul("a\nb", "--branch")
        with self.assertRaises(ValueError):
            MOD.assert_no_newlines_or_nul("a\rb", "--branch")
        with self.assertRaises(ValueError):
            MOD.assert_no_newlines_or_nul("a\x00b", "--branch")

    def test_redact_secret(self):
        self.assertEqual(MOD.redact_secret(""), "***")
        self.assertEqual(MOD.redact_secret("abcd"), "***")
        self.assertEqual(MOD.redact_secret("key_12345678"), "ke***78")

    def test_parse_openai_enabled(self):
        self.assertFalse(MOD.parse_openai_enabled(""))
        self.assertFalse(MOD.parse_openai_enabled("0"))
        self.assertFalse(MOD.parse_openai_enabled("false"))
        self.assertTrue(MOD.parse_openai_enabled("1"))
        self.assertTrue(MOD.parse_openai_enabled("TRUE"))
        self.assertTrue(MOD.parse_openai_enabled("Yes"))

    def test_classify_followup_agent_running(self):
        state, reason, snapshot = MOD.classify_followup_agent(
            200,
            {"id": "bc-abc123", "status": "RUNNING", "conversation": ["secret"]},
            expected_id="bc-abc123",
        )
        self.assertEqual(state, "running")
        self.assertIsNone(reason)
        self.assertEqual(snapshot, {"id": "bc-abc123", "status": "RUNNING"})
        self.assertNotIn("conversation", snapshot)

    def test_classify_followup_agent_missing_and_stale(self):
        missing_state, missing_reason, missing_snap = MOD.classify_followup_agent(
            404, {}, expected_id="bc-gone"
        )
        self.assertEqual(missing_state, "missing")
        self.assertIn("not found", missing_reason or "")
        self.assertEqual(missing_snap["id"], "bc-gone")

        stale_state, stale_reason, stale_snap = MOD.classify_followup_agent(
            200,
            {"id": "bc-done", "status": "FINISHED"},
            expected_id="bc-done",
        )
        self.assertEqual(stale_state, "stale")
        self.assertIn("FINISHED", stale_reason or "")
        self.assertEqual(stale_snap["status"], "FINISHED")

        empty_state, empty_reason, _ = MOD.classify_followup_agent(
            200, {"id": "bc-empty"}, expected_id="bc-empty"
        )
        self.assertEqual(empty_state, "missing")
        self.assertIn("no status", empty_reason or "")

    def test_classify_followup_agent_unknown_failures(self):
        unknown_state, unknown_reason, _ = MOD.classify_followup_agent(
            500, {"error": "boom"}, expected_id="bc-1"
        )
        self.assertEqual(unknown_state, "unknown")
        self.assertIn("HTTP 500", unknown_reason or "")

        mismatch_state, mismatch_reason, _ = MOD.classify_followup_agent(
            200,
            {"id": "bc-other", "status": "RUNNING"},
            expected_id="bc-wanted",
        )
        self.assertEqual(mismatch_state, "unknown")
        self.assertIn("did not match", mismatch_reason or "")

    def test_followup_dry_run_fields_never_claim_ready(self):
        receipt_block = "Live Cursor API handoff is blocked."
        blocked = MOD.followup_dry_run_fields(receipt_block)
        self.assertEqual(blocked["agent_state"], "not_checked")
        self.assertEqual(blocked["receipt_would_block"], receipt_block)
        self.assertEqual(blocked["followup_would_block"], receipt_block)
        self.assertFalse(blocked["followup_ready"])

        clear_receipt = MOD.followup_dry_run_fields(None)
        self.assertEqual(clear_receipt["agent_state"], "not_checked")
        self.assertIsNone(clear_receipt["receipt_would_block"])
        self.assertEqual(
            clear_receipt["followup_would_block"],
            MOD.FOLLOWUP_AGENT_NOT_CHECKED,
        )
        self.assertFalse(clear_receipt["followup_ready"])
        self.assertIn("did not check", clear_receipt["followup_would_block"])

    def test_agent_status_readout_recognizes_every_known_state(self):
        for state in ["CREATING", "PENDING", "RUNNING", "FINISHED", "FAILED", "CANCELLED", "STOPPED", "EXPIRED"]:
            with self.subTest(state=state):
                readout = MOD.agent_status_readout({"id": "bc-1", "status": state}, "bc-1")
                self.assertEqual(readout["agent_status"], state)
                self.assertTrue(readout["status_verified"])
                self.assertTrue(readout["next_action"])

    def test_agent_status_readout_normalizes_case_and_whitespace(self):
        readout = MOD.agent_status_readout({"id": "bc-1", "status": " failed "}, "bc-1")
        self.assertEqual(readout["agent_status"], "FAILED")
        self.assertIn("before deciding whether to retry", readout["next_action"])

    def test_agent_status_readout_never_trusts_missing_or_mismatched_evidence(self):
        for response in [
            {},
            {"status": "FINISHED"},
            {"id": "bc-other", "status": "FINISHED"},
            {"id": "bc-1"},
            {"id": "bc-1", "status": "SOME_NEW_STATE"},
            {"id": "bc-1", "status": ["FINISHED"]},
            {"id": "bc-1", "status": "FINISHED", "_non_json_response": True},
            "not a dict",
            None,
        ]:
            with self.subTest(response=response):
                readout = MOD.agent_status_readout(response, "bc-1")
                self.assertEqual(readout["agent_status"], "UNKNOWN")
                self.assertFalse(readout["status_verified"])
                self.assertIn("unverified", readout["next_action"])

    def test_agent_status_readout_finished_does_not_claim_verified_work(self):
        readout = MOD.agent_status_readout({"id": "bc-1", "status": "FINISHED"}, "bc-1")
        self.assertEqual(readout["agent_status"], "FINISHED")
        self.assertIn("not verified", readout["next_action"])


if __name__ == "__main__":
    unittest.main()

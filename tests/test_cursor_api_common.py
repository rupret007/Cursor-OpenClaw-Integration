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


if __name__ == "__main__":
    unittest.main()

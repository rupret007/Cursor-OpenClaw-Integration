import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock


SCRIPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "alexa_edge_lambda.py"
SPEC = importlib.util.spec_from_file_location("alexa_edge_lambda", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["alexa_edge_lambda"] = MODULE
SPEC.loader.exec_module(MODULE)  # type: ignore[attr-defined]


class AlexaEdgeLambdaTests(unittest.TestCase):
    def test_decode_event_body_supports_base64(self) -> None:
        raw = json.dumps({"hello": "world"}).encode("utf-8")
        event = {
            "body": MODULE.base64.b64encode(raw).decode("utf-8"),
            "isBase64Encoded": True,
        }
        self.assertEqual(MODULE.decode_event_body(event), raw.decode("utf-8"))
        self.assertEqual(MODULE.decode_event_body_bytes(event), raw)

    def test_extract_application_id_prefers_session_then_context(self) -> None:
        body = {"session": {"application": {"applicationId": "amzn1.ask.skill.demo"}}}
        self.assertEqual(MODULE.extract_application_id(body), "amzn1.ask.skill.demo")

    def test_verify_allowed_application_id_rejects_unexpected_id(self) -> None:
        body = {"session": {"application": {"applicationId": "wrong"}}}
        with self.assertRaises(PermissionError):
            MODULE.verify_allowed_application_id(
                body,
                allowed_ids=["amzn1.ask.skill.expected"],
            )

    def test_backend_error_response_for_401_returns_alexa_payload(self) -> None:
        payload = MODULE.backend_error_response(401)
        self.assertEqual(payload["version"], "1.0")
        self.assertIn("secure edge connection", payload["response"]["outputSpeech"]["text"].lower())

    def test_handle_edge_event_rejects_invalid_json_body(self) -> None:
        response = MODULE.handle_edge_event(
            {"body": "not-json"},
            andrea_sync_url="https://andrea.example.com",
            edge_token="secret",
        )
        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 200)
        self.assertIn("could not read", body["response"]["outputSpeech"]["text"].lower())

    def test_handle_edge_event_maps_backend_http_error(self) -> None:
        event = {
            "body": json.dumps(
                {
                    "session": {"application": {"applicationId": "amzn1.ask.skill.demo"}},
                    "request": {"type": "LaunchRequest"},
                }
            )
        }
        fake_error = MODULE.urllib.error.HTTPError(
            url="https://example.invalid",
            code=503,
            msg="unavailable",
            hdrs=None,
            fp=None,
        )
        fake_error.read = lambda: b'{"error":"unavailable"}'  # type: ignore[assignment]
        with mock.patch.object(MODULE, "forward_to_andrea", side_effect=fake_error):
            response = MODULE.handle_edge_event(
                event,
                andrea_sync_url="https://andrea.example.com",
                edge_token="secret",
                allowed_application_ids=["amzn1.ask.skill.demo"],
            )
        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 200)
        self.assertIn("temporarily unavailable", body["response"]["outputSpeech"]["text"].lower())

    def test_handle_edge_event_maps_timeout_url_error(self) -> None:
        event = {
            "body": json.dumps(
                {
                    "session": {"application": {"applicationId": "amzn1.ask.skill.demo"}},
                    "request": {"type": "LaunchRequest"},
                }
            )
        }
        timeout_error = MODULE.urllib.error.URLError(MODULE.socket.timeout("timed out"))
        with mock.patch.object(MODULE, "forward_to_andrea", side_effect=timeout_error):
            response = MODULE.handle_edge_event(
                event,
                andrea_sync_url="https://andrea.example.com",
                edge_token="secret",
                allowed_application_ids=["amzn1.ask.skill.demo"],
            )
        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 200)
        self.assertIn("timed out", body["response"]["outputSpeech"]["text"].lower())

    def test_handle_edge_event_success_forwards_backend_payload(self) -> None:
        event = {
            "body": json.dumps(
                {
                    "session": {"application": {"applicationId": "amzn1.ask.skill.demo"}},
                    "request": {"type": "LaunchRequest"},
                }
            )
        }
        backend_payload = MODULE.build_alexa_response("AndreaBot is ready.")
        with mock.patch.object(MODULE, "forward_to_andrea", return_value=backend_payload) as forward:
            response = MODULE.handle_edge_event(
                event,
                andrea_sync_url="https://andrea.example.com",
                edge_token="secret",
                allowed_application_ids=["amzn1.ask.skill.demo"],
            )
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["response"]["outputSpeech"]["text"], "AndreaBot is ready.")
        forward.assert_called_once()

    def test_handle_edge_event_forwards_raw_body_and_signature_headers(self) -> None:
        raw = b'{"session":{"application":{"applicationId":"amzn1.ask.skill.demo"}},"request":{"type":"LaunchRequest"}}'
        event = {
            "body": raw.decode("utf-8"),
            "headers": {
                "Signature": "sig-value",
                "SignatureCertChainUrl": "https://s3.amazonaws.com/echo.api/demo.pem",
                "X-Ignored": "nope",
            },
        }
        backend_payload = MODULE.build_alexa_response("AndreaBot is ready.")
        with mock.patch.object(MODULE, "forward_to_andrea", return_value=backend_payload) as forward:
            MODULE.handle_edge_event(
                event,
                andrea_sync_url="https://andrea.example.com",
                edge_token="secret",
                allowed_application_ids=["amzn1.ask.skill.demo"],
            )
        args, kwargs = forward.call_args
        self.assertEqual(args[0], raw)
        self.assertEqual(kwargs["passthrough_headers"]["Signature"], "sig-value")
        self.assertEqual(
            kwargs["passthrough_headers"]["SignatureCertChainUrl"],
            "https://s3.amazonaws.com/echo.api/demo.pem",
        )
        self.assertNotIn("X-Ignored", kwargs["passthrough_headers"])

    def test_handler_returns_alexa_payload_for_bad_env(self) -> None:
        with mock.patch.dict(MODULE.os.environ, {"ANDREA_SYNC_URL": "https://andrea.example.com"}, clear=True):
            response = MODULE.handler({"body": "{}"}, None)
        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 200)
        self.assertIn("configuration is incomplete", body["response"]["outputSpeech"]["text"].lower())


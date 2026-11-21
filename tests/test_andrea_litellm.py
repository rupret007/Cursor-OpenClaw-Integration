"""Verify each Andrea model caller through a local LiteLLM protocol fixture."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from services.andrea_sync.andrea_router import _openai_direct_reply
from services.andrea_sync.conversation_eval import (
    run_llm_evaluator,
    run_semantic_adjudicator,
)
from services.andrea_sync.stateful_answer_realization import _openai_json_chat


def test_andrea_model_requests_use_the_lane_key(monkeypatch):
    calls = []

    class Proxy(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, self.headers.get("Authorization"), request))
            content = '{"grounded":true}' if "response_format" in request else "A synthetic reply."
            body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("localhost", 0), Proxy) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.setenv("OPENAI_API_ENABLED", "1")
            monkeypatch.setenv("OPENAI_API_KEY", "unused-provider-credential")
            monkeypatch.setenv("ANDREA_LITELLM_BASE_URL", f"http://localhost:{server.server_port}/v1")
            monkeypatch.setenv("ANDREA_LITELLM_API_KEY", "fixture-lane-key")
            monkeypatch.setenv("ANDREA_LITELLM_MODEL", "test-andrea-model")
            monkeypatch.delenv("ANDREA_MODEL_ROUTER", raising=False)
            monkeypatch.delenv("ANDREA_MODEL_VERIFIER", raising=False)

            assert _openai_direct_reply("Synthetic test request") == "A synthetic reply."
            assert _openai_json_chat(
                system="Synthetic test rules", user="Synthetic test request",
                model="test-andrea-model", timeout_seconds=5,
            ) == {"grounded": True}
            assert run_llm_evaluator({}, weak_or_failed=True)["verdict"] == {"grounded": True}
            assert run_semantic_adjudicator(
                user_text="Synthetic test request", scenario_id="test",
                turn_domain="casual_conversation", continuity_focus="", confidence=0.5,
            )["adjudication"] == {"grounded": True}

            monkeypatch.delenv("ANDREA_LITELLM_API_KEY")
            with pytest.raises(RuntimeError, match="openai_direct_disabled"):
                _openai_direct_reply("Synthetic test request")
            assert run_llm_evaluator({}, weak_or_failed=True) is None
        finally:
            server.shutdown()
            thread.join()

    assert [(path, auth, request["model"]) for path, auth, request in calls] == [
        ("/v1/chat/completions", "Bearer fixture-lane-key", "test-andrea-model"),
    ] * 4
    assert ["response_format" in request for _, _, request in calls] == [False, True, True, True]

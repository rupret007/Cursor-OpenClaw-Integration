"""Verify each Andrea model caller through a local LiteLLM protocol fixture."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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


def test_llm_eval_keeps_the_litellm_lane_and_runs_the_evaluator(tmp_path, monkeypatch):
    """The --llm-eval CLI must keep the lane configuration and call the verifier."""
    calls = []
    key = "fixture-lane-key"
    model = "test-andrea-model"

    class Proxy(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, self.headers.get("Authorization"), request.get("model"),
                          "response_format" in request))
            content = json.dumps({
                "satisfied_request": True,
                "right_domain": True,
                "useful": True,
                "too_thin": False,
                "too_mechanical": False,
                "contaminated_memory": False,
                "notes": "synthetic",
            })
            body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    repo = Path(__file__).resolve().parents[1]
    with ThreadingHTTPServer(("localhost", 0), Proxy) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.setenv("OPENAI_API_ENABLED", "0")
            monkeypatch.setenv("ANDREA_LITELLM_BASE_URL", f"http://localhost:{server.server_port}/v1")
            monkeypatch.setenv("ANDREA_LITELLM_API_KEY", key)
            monkeypatch.setenv("ANDREA_LITELLM_MODEL", model)
            monkeypatch.setenv("ANDREA_EXPERIENCE_PROGRESS_ENABLED", "0")
            monkeypatch.delenv("ANDREA_MODEL_VERIFIER", raising=False)
            from services.andrea_sync.litellm_api import litellm_configured
            assert litellm_configured() is True

            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/andrea_experience_cycle.py",
                    "--db", str(tmp_path / "experience.db"),
                    "--repo", str(tmp_path),
                    "--suite", "conversation_core",
                    "--scenario-ids", "hi_andrea",
                    "--llm-eval",
                    "--sample-pass-evals", "1",
                    "--no-save",
                    "--no-write-report",
                ],
                cwd=repo,
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)

    assert key not in completed.stdout and key not in completed.stderr
    payload = json.loads(completed.stdout)
    checks = payload["verification_report"]["checks"]
    assert [row["scenario_id"] for row in checks] == ["conversation_core::hi_andrea"]
    llm_eval = checks[0]["metadata"]["llm_eval"]
    assert isinstance(llm_eval, dict) and isinstance(llm_eval.get("verdict"), dict)
    assert llm_eval["verdict"]["useful"] is True
    assert "llm_evaluator aggregate" in [row["description"] for row in checks[0]["observations"]]
    assert calls == [("/v1/chat/completions", f"Bearer {key}", model, True)]

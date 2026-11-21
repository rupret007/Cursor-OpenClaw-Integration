import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("lane_queue", ROOT / "first-officer/lanes/queue.py")
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)

HEAD = "3d350baac7636fe068d5be6299991f5e776259b5"
OTHER = "3821febcecd9444392e34886383c24e694bc561e"
REQUEST = {"lane": "review", "head": HEAD, "pull_request": 36}
NOW = 1_763_700_000
HATCHET_TOKEN = "hatchet-lane-token"
RESTATE_TOKEN = "restate-lane-token"
TENANT = "b7e1c0de4a114c0e9a110c0ffee00001"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        spec = self.server.routes.get(self.path, {"status": 404, "body": {}})
        self.server.seen.append({
            "path": self.path,
            "body": raw,
            "authorization": self.headers.get("Authorization"),
            "idempotency": self.headers.get("idempotency-key"),
        })
        if spec.get("delay"):
            time.sleep(spec["delay"])
        status = spec.get("status", 200)
        try:
            if status in (301, 302, 303, 307, 308):
                self.send_response(status)
                self.send_header("Location", spec["location"])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            data = spec.get("raw", json.dumps(spec.get("body", {})).encode())
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, *_args):
        return


def serve(routes):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.routes = routes
    server.seen = []
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop(server):
    server.shutdown()
    server.server_close()


def policy(hatchet_url, restate_url):
    return {
        "hatchet": {"base_url": hatchet_url, "tenant": TENANT, "workflow": "review-lane",
                    "token_env": "FO_HATCHET_LANE_TOKEN"},
        "restate": {"base_url": restate_url, "service": "LaneQueue", "handler": "submit",
                    "token_env": "FO_RESTATE_LANE_TOKEN"},
    }


def urls(server):
    return f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def tokens(monkeypatch):
    monkeypatch.setenv("FO_HATCHET_LANE_TOKEN", HATCHET_TOKEN)
    monkeypatch.setenv("FO_RESTATE_LANE_TOKEN", RESTATE_TOKEN)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")


def dispatch(config, ledger, request=None, timeout=15):
    return queue.dispatch(config, request or REQUEST, ledger, NOW, timeout)


HATCHET_PATH = f"/api/v1/stable/tenants/{TENANT}/workflow-runs/trigger"
RESTATE_PATH = "/restate/send/LaneQueue/submit"


def phases(ledger):
    return [json.loads(line)["phase"] for line in Path(ledger).read_text().splitlines() if line.strip()]


def test_primary_success_never_calls_restate_and_a_repeat_is_free(tmp_path, tokens):
    hatchet = serve({HATCHET_PATH: {"body": {"run": {"metadata": {"id": "run_primary"}}}}})
    restate = serve({RESTATE_PATH: {"body": {"invocationId": "inv_unused", "status": "Accepted"}}})
    try:
        config = policy(urls(hatchet), urls(restate))
        ledger = tmp_path / "lanes.jsonl"
        first = dispatch(config, ledger)
        second = dispatch(config, ledger)
    finally:
        stop(hatchet)
        stop(restate)

    assert first == {**first, "backend": "hatchet", "run": "run_primary", "reused": False, "phase": "queued"}
    assert second["reused"] is True and second["run"] == "run_primary"
    assert [row["path"] for row in hatchet.seen] == [HATCHET_PATH]
    assert restate.seen == []
    body = json.loads(hatchet.seen[0]["body"])
    assert body["workflowName"] == "review-lane"
    assert body["input"] == REQUEST
    assert body["additionalMetadata"] == {"idempotencyKey": queue.idempotency_key(REQUEST)}
    assert hatchet.seen[0]["authorization"] == f"Bearer {HATCHET_TOKEN}"
    text = ledger.read_text()
    assert HATCHET_TOKEN not in text and RESTATE_TOKEN not in text
    assert oct(ledger.stat().st_mode & 0o777) == "0o600"


def test_primary_429_fails_over_to_restate_and_the_repeat_does_not_send(tmp_path, tokens):
    hatchet = serve({HATCHET_PATH: {"status": 429, "body": {"echo": HATCHET_TOKEN}}})
    restate = serve({RESTATE_PATH: {"body": {"invocationId": "inv_fallback", "status": "Accepted"}}})
    try:
        config = policy(urls(hatchet), urls(restate))
        ledger = tmp_path / "lanes.jsonl"
        queued = dispatch(config, ledger)
        again = dispatch(config, ledger)
    finally:
        stop(hatchet)
        stop(restate)

    key = queue.idempotency_key(REQUEST)
    assert queued["backend"] == "restate" and queued["run"] == "inv_fallback" and queued["reused"] is False
    assert again["reused"] is True and again["run"] == "inv_fallback"
    assert [row["path"] for row in hatchet.seen] == [HATCHET_PATH]
    assert [row["path"] for row in restate.seen] == [RESTATE_PATH]
    assert json.loads(restate.seen[0]["body"]) == REQUEST
    assert restate.seen[0]["idempotency"] == key
    assert restate.seen[0]["authorization"] == f"Bearer {RESTATE_TOKEN}"
    assert phases(ledger) == ["pending", "refused", "pending", "queued"]
    text = ledger.read_text()
    assert HATCHET_TOKEN not in text and "echo" not in text and key in text


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
def test_hatchet_server_error_or_request_timeout_does_not_reach_restate(tmp_path, tokens, status):
    hatchet = serve({HATCHET_PATH: {"status": status, "body": {}}})
    restate = serve({RESTATE_PATH: {"body": {"invocationId": "inv_no", "status": "Accepted"}}})
    ledger = tmp_path / "lanes.jsonl"
    config = policy(urls(hatchet), urls(restate))
    try:
        with pytest.raises(ValueError, match="not failed over"):
            dispatch(config, ledger)
        with pytest.raises(ValueError, match="not sent again"):
            dispatch(config, ledger)
    finally:
        stop(hatchet)
        stop(restate)
    assert [row["path"] for row in hatchet.seen] == [HATCHET_PATH]
    assert restate.seen == []
    assert phases(ledger)[-1] == "uncertain"


def test_previously_accepted_restate_run_is_the_same_lane(tmp_path, tokens):
    hatchet = serve({HATCHET_PATH: {"status": 429, "body": {}}})
    restate = serve({RESTATE_PATH: {"body": {"invocationId": "inv_same", "status": "PreviouslyAccepted"}}})
    try:
        result = dispatch(policy(urls(hatchet), urls(restate)), tmp_path / "lanes.jsonl")
    finally:
        stop(hatchet)
        stop(restate)
    assert result["backend"] == "restate" and result["run"] == "inv_same"


def test_metadata_id_without_a_run_wrapper_is_accepted(tmp_path, tokens):
    hatchet = serve({HATCHET_PATH: {"body": {"metadata": {"id": "run_flat"}}}})
    restate = serve({})
    try:
        result = dispatch(policy(urls(hatchet), urls(restate)), tmp_path / "lanes.jsonl")
    finally:
        stop(hatchet)
        stop(restate)
    assert result["backend"] == "hatchet" and result["run"] == "run_flat"
    assert restate.seen == []


def test_timeout_does_not_fail_over_and_a_retry_sends_nothing(tmp_path, tokens):
    hatchet = serve({HATCHET_PATH: {"delay": 1, "body": {"run": {"metadata": {"id": "run_late"}}}}})
    restate = serve({RESTATE_PATH: {"body": {"invocationId": "inv_no", "status": "Accepted"}}})
    ledger = tmp_path / "lanes.jsonl"
    config = policy(urls(hatchet), urls(restate))
    try:
        with pytest.raises(ValueError, match="not failed over"):
            dispatch(config, ledger, timeout=0.2)
        with pytest.raises(ValueError, match="not sent again"):
            dispatch(config, ledger, timeout=0.2)
    finally:
        stop(hatchet)
        stop(restate)
    assert [row["path"] for row in hatchet.seen] == [HATCHET_PATH]
    assert restate.seen == []
    assert phases(ledger)[-1] == "uncertain"


def test_redirect_is_not_followed_and_does_not_fail_over(tmp_path, tokens):
    restate = serve({RESTATE_PATH: {"body": {"invocationId": "inv_no", "status": "Accepted"}},
                     "/stolen": {"body": {"invocationId": "inv_stolen", "status": "Accepted"}}})
    hatchet = serve({HATCHET_PATH: {"status": 302, "location": urls(restate) + "/stolen"}})
    try:
        with pytest.raises(ValueError, match="not failed over"):
            dispatch(policy(urls(hatchet), urls(restate)), tmp_path / "lanes.jsonl")
    finally:
        stop(hatchet)
        stop(restate)
    assert [row["path"] for row in hatchet.seen] == [HATCHET_PATH]
    assert restate.seen == []


def test_authentication_failure_does_not_fail_over(tmp_path, tokens):
    hatchet = serve({HATCHET_PATH: {"status": 401, "body": {"error": "denied"}}})
    restate = serve({RESTATE_PATH: {"body": {"invocationId": "inv_no", "status": "Accepted"}}})
    try:
        with pytest.raises(ValueError, match="not failed over"):
            dispatch(policy(urls(hatchet), urls(restate)), tmp_path / "lanes.jsonl")
    finally:
        stop(hatchet)
        stop(restate)
    assert restate.seen == []


def test_closed_port_is_a_definite_refusal_and_restate_answers(tmp_path, tokens):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed = probe.getsockname()[1]
    probe.close()
    restate = serve({RESTATE_PATH: {"body": {"invocationId": "inv_closed", "status": "Accepted"}}})
    try:
        result = dispatch(policy(f"http://127.0.0.1:{closed}", urls(restate)), tmp_path / "lanes.jsonl")
    finally:
        stop(restate)
    assert result["backend"] == "restate" and result["run"] == "inv_closed"
    assert [row["path"] for row in restate.seen] == [RESTATE_PATH]


def test_both_backends_refusing_is_retryable_and_queues_nothing(tmp_path, tokens):
    hatchet = serve({HATCHET_PATH: {"status": 429, "body": {}}})
    restate = serve({RESTATE_PATH: {"status": 429, "body": {}}})
    ledger = tmp_path / "lanes.jsonl"
    config = policy(urls(hatchet), urls(restate))
    try:
        with pytest.raises(ValueError, match="was not queued"):
            dispatch(config, ledger)
        with pytest.raises(ValueError, match="was not queued"):
            dispatch(config, ledger)
    finally:
        stop(hatchet)
        stop(restate)
    assert len(hatchet.seen) == 2 and len(restate.seen) == 2
    assert phases(ledger).count("queued") == 0


def test_restate_uncertainty_after_a_refusal_is_not_retried(tmp_path, tokens):
    hatchet = serve({HATCHET_PATH: {"status": 429, "body": {}}})
    restate = serve({RESTATE_PATH: {"raw": b"not-json"}})
    ledger = tmp_path / "lanes.jsonl"
    config = policy(urls(hatchet), urls(restate))
    try:
        with pytest.raises(ValueError, match="Restate outcome is unknown"):
            dispatch(config, ledger)
        with pytest.raises(ValueError, match="not sent again"):
            dispatch(config, ledger)
    finally:
        stop(hatchet)
        stop(restate)
    assert len(hatchet.seen) == 1 and len(restate.seen) == 1


def test_missing_primary_credential_does_not_call_either_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("FO_HATCHET_LANE_TOKEN", raising=False)
    monkeypatch.setenv("FO_RESTATE_LANE_TOKEN", RESTATE_TOKEN)
    hatchet = serve({HATCHET_PATH: {"body": {"run": {"metadata": {"id": "run_no"}}}}})
    restate = serve({RESTATE_PATH: {"body": {"invocationId": "inv_no", "status": "Accepted"}}})
    ledger = tmp_path / "lanes.jsonl"
    try:
        with pytest.raises(ValueError, match="missing from the environment"):
            dispatch(policy(urls(hatchet), urls(restate)), ledger)
    finally:
        stop(hatchet)
        stop(restate)
    assert hatchet.seen == [] and restate.seen == []
    assert not ledger.exists() or ledger.read_text() == ""


def test_a_pending_row_blocks_a_second_send(tmp_path, tokens):
    hatchet = serve({HATCHET_PATH: {"body": {"run": {"metadata": {"id": "run_no"}}}}})
    restate = serve({})
    ledger = tmp_path / "lanes.jsonl"
    key = queue.idempotency_key(REQUEST)
    ledger.write_text(json.dumps({"at": NOW, "backend": "hatchet", "head": HEAD, "key": key,
                                  "lane": "review", "phase": "pending", "run": None}) + "\n")
    ledger.chmod(0o600)
    try:
        with pytest.raises(ValueError, match="not sent again"):
            dispatch(policy(urls(hatchet), urls(restate)), ledger)
    finally:
        stop(hatchet)
        stop(restate)
    assert hatchet.seen == [] and restate.seen == []


def test_concurrent_dispatch_of_one_lane_sends_once(tmp_path, tokens):
    hatchet = serve({HATCHET_PATH: {"delay": 0.3, "body": {"run": {"metadata": {"id": "run_once"}}}}})
    restate = serve({RESTATE_PATH: {"body": {"invocationId": "inv_no", "status": "Accepted"}}})
    ledger = tmp_path / "lanes.jsonl"
    config = policy(urls(hatchet), urls(restate))
    results = []

    def run():
        try:
            results.append(dispatch(config, ledger))
        except Exception as error:
            results.append(error)

    try:
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
    finally:
        stop(hatchet)
        stop(restate)
    assert len(results) == 2 and all(isinstance(row, dict) for row in results)
    assert sorted(row["reused"] for row in results) == [False, True]
    assert {row["run"] for row in results} == {"run_once"}
    assert len(hatchet.seen) == 1 and restate.seen == []


def test_private_ledger_is_required_and_a_different_head_is_a_different_lane(tmp_path, tokens):
    hatchet = serve({HATCHET_PATH: {"body": {"run": {"metadata": {"id": "run_other"}}}}})
    restate = serve({})
    config = policy(urls(hatchet), urls(restate))
    loose = tmp_path / "loose.jsonl"
    loose.write_text("{}\n")
    loose.chmod(0o644)
    link = tmp_path / "link.jsonl"
    link.symlink_to(loose)
    try:
        with pytest.raises(ValueError, match="private owner file"):
            dispatch(config, loose)
        with pytest.raises(ValueError, match="private owner file"):
            dispatch(config, link)
        other = dispatch(config, tmp_path / "other.jsonl", {**REQUEST, "head": OTHER})
    finally:
        stop(hatchet)
        stop(restate)
    assert other["run"] == "run_other"
    assert queue.idempotency_key(REQUEST) != queue.idempotency_key({**REQUEST, "head": OTHER})
    assert len(hatchet.seen) == 1


PUBLIC = "8.8.8.8"
PRIVATE = "10." + "0.0.8"


def _answers(address):
    def resolve(_host, _port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 0))]
    return resolve


def test_named_https_queue_needs_an_allowlist_and_private_resolution(tmp_path, monkeypatch):
    monkeypatch.delenv("FO_QUEUE_HOSTS", raising=False)
    with pytest.raises(ValueError, match="loopback"):
        queue.endpoint("https://queue.example.com")
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy("https://queue.example.com", "http://127.0.0.1:9")))
    with pytest.raises(ValueError, match="loopback"):
        queue.load_config(path)
    base, _opener = queue.endpoint("http://127.0.0.1:9")
    assert base == "http://127.0.0.1:9"

    monkeypatch.setenv("FO_QUEUE_HOSTS", "queue.internal")
    with pytest.raises(ValueError, match="loopback"):
        queue.endpoint("https://queue.internal", resolve=_answers(PUBLIC))
    with pytest.raises(ValueError, match="loopback"):
        queue.endpoint("https://other.internal", resolve=_answers(PRIVATE))
    accepted, _opener = queue.endpoint("https://queue.internal", resolve=_answers(PRIVATE))
    assert accepted == "https://queue.internal"


def test_policy_rejects_a_public_address_an_incomplete_backend_and_a_bad_request(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy("http://example.com", "http://127.0.0.1:9")))
    with pytest.raises(ValueError, match="loopback"):
        queue.load_config(path)
    path.write_text(json.dumps(policy("https://8.8.8.8", "http://127.0.0.1:9")))
    with pytest.raises(ValueError, match="loopback"):
        queue.load_config(path)
    incomplete = policy("http://127.0.0.1:9", "http://127.0.0.1:9")
    del incomplete["restate"]["token_env"]
    path.write_text(json.dumps(incomplete))
    with pytest.raises(ValueError, match="incomplete"):
        queue.load_config(path)
    with pytest.raises(ValueError, match="pull request"):
        queue.idempotency_key({**REQUEST, "pull_request": True})
    with pytest.raises(ValueError, match="head"):
        queue.idempotency_key({**REQUEST, "head": "abc"})


def test_cli_prints_the_fallback_and_refuses_a_protected_outcome(tmp_path, tokens, capsys):
    hatchet = serve({HATCHET_PATH: {"status": 429, "body": {}}})
    restate = serve({RESTATE_PATH: {"body": {"invocationId": "inv_cli", "status": "Accepted"}}})
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy(urls(hatchet), urls(restate))))
    ledger = tmp_path / "lanes.jsonl"
    argv = [str(policy_path), str(ledger), "--lane", "review", "--head", HEAD, "--pull-request", "36"]
    try:
        assert queue.main(argv) == 0
        first = capsys.readouterr()
        assert first.out == "Lane review is queued on restate as inv_cli.\n"
        assert first.err == ""
        assert HATCHET_TOKEN not in first.out
        assert queue.main(argv) == 0
        assert capsys.readouterr().out == "Lane review was already queued on restate as inv_cli.\n"
    finally:
        stop(hatchet)
        stop(restate)

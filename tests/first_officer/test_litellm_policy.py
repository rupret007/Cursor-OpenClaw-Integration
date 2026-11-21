import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import ssl
import subprocess
import threading

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "fo_litellm_configure", ROOT / "first-officer/litellm/configure.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)
api_spec = importlib.util.spec_from_file_location(
    "fo_litellm_api", ROOT / "services/andrea_sync/litellm_api.py")
api = importlib.util.module_from_spec(api_spec)
api_spec.loader.exec_module(api)

KEY = "sk-" + "abcd1234efgh5678"
PUBLIC = "8.8.8.8"
PRIVATE = "10." + "0.0.5"


PLAN = {
    "fallbacks": {"primary": ["fallback"]},
    "lanes": {"review": {
        "models": ["primary", "fallback"], "max_budget": 1, "budget_duration": "1d",
    }},
}


def catalog(*rows):
    return {
        "model_list": list(rows),
        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY",
            "database_url": "os.environ/DATABASE_URL",
        },
    }


def row(alias, input_price, output_price, *, where="litellm_params"):
    prices = {}
    if input_price is not None:
        prices["input_cost_per_token"] = input_price
    if output_price is not None:
        prices["output_cost_per_token"] = output_price
    return {"model_name": alias, where: prices}


def test_a_routed_model_needs_finite_nonnegative_prices_and_one_charge():
    rendered = configure.candidate_config(catalog(
        row("primary", 0.001, 0.002),
        row("fallback", 0, 0.002, where="model_info"),
    ), PLAN)
    assert rendered["litellm_settings"]["block_requests_for_models_without_pricing"] is True

    agreed = row("primary", 0.001, 0.002)
    agreed["model_info"] = {"input_cost_per_token": 0.001, "output_cost_per_token": 0.002}
    configure.candidate_config(catalog(agreed, row("fallback", 0.1, 0.1)), PLAN)
    conflict = row("primary", 0.001, 0.002)
    conflict["model_info"] = {"input_cost_per_token": 0.003, "output_cost_per_token": 0.002}
    with pytest.raises(ValueError, match="pricing"):
        configure.candidate_config(catalog(conflict, row("fallback", 0.1, 0.1)), PLAN)

    for primary in (
        row("primary", -1, 0.1),
        row("primary", float("nan"), 0.1),
        row("primary", float("inf"), 0.1),
        row("primary", "0.1", 0.1),
        row("primary", True, 0.1),
        row("primary", 0, 0),
        row("primary", 0.1, None),
    ):
        with pytest.raises(ValueError, match="pricing"):
            configure.candidate_config(catalog(primary, row("fallback", 0.1, 0.1)), PLAN)


def _answers(address):
    def resolve(_host, _port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 0))]
    return resolve


def test_https_stays_on_an_allowlisted_private_endpoint(monkeypatch):
    opened = []

    def explode(*_args, **_kwargs):
        opened.append(True)
        raise AssertionError("credentials were sent")

    monkeypatch.setattr(configure, "build_opener", explode)
    monkeypatch.delenv("FO_LITELLM_HOSTS", raising=False)
    with pytest.raises(ValueError, match="private"):
        configure.proxy_request("https://litellm.invalid", KEY, "/health")
    assert opened == []

    monkeypatch.setenv("FO_LITELLM_HOSTS", "litellm.internal")
    with pytest.raises(ValueError, match="private"):
        configure.proxy_request("https://litellm.internal", KEY, "/health", resolve=_answers(PUBLIC))
    with pytest.raises(ValueError, match="private"):
        configure.proxy_request("https://other.internal", KEY, "/health", resolve=_answers(PRIVATE))
    assert opened == []

    with pytest.raises(AssertionError, match="credentials were sent"):
        configure.proxy_request("https://litellm.internal", KEY, "/health", resolve=_answers(PRIVATE))
    with pytest.raises(AssertionError, match="credentials were sent"):
        configure.proxy_request("http://127.0.0.1:4000", KEY, "/health")
    assert opened == [True, True]

    monkeypatch.delenv("FO_LITELLM_HOSTS", raising=False)
    monkeypatch.setenv("ANDREA_LITELLM_API_KEY", KEY)
    monkeypatch.setenv("ANDREA_LITELLM_MODEL", "alias")
    monkeypatch.setenv("ANDREA_LITELLM_BASE_URL", "https://litellm.invalid/v1")
    assert api._credentials() is None
    with pytest.raises(RuntimeError, match="proxy_unconfigured"):
        api.litellm_chat({}, timeout_seconds=1, error_prefix="lane")

    monkeypatch.setenv("FO_LITELLM_HOSTS", "litellm.internal")
    monkeypatch.setenv("ANDREA_LITELLM_BASE_URL", "https://litellm.internal/v1")
    assert api._credentials(resolve=_answers(PUBLIC)) is None
    endpoint, key = api._credentials(resolve=_answers(PRIVATE))
    assert endpoint == "https://litellm.internal/v1/chat/completions"
    assert key == KEY

    monkeypatch.setenv("FO_LITELLM_HOSTS", PUBLIC)
    monkeypatch.setenv("ANDREA_LITELLM_BASE_URL", f"https://{PUBLIC}/v1")
    assert api._credentials() is None
    monkeypatch.setenv("FO_LITELLM_HOSTS", PRIVATE)
    monkeypatch.setenv("ANDREA_LITELLM_BASE_URL", f"https://{PRIVATE}/v1")
    assert api._credentials()[0] == f"https://{PRIVATE}/v1/chat/completions"

    monkeypatch.delenv("FO_LITELLM_HOSTS", raising=False)
    monkeypatch.setenv("ANDREA_LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")
    assert api._credentials()[0] == "http://127.0.0.1:4000/v1/chat/completions"


def test_litellm_connects_to_the_checked_address(tmp_path, monkeypatch):
    """A second DNS answer must not move the bearer request off the checked address."""
    host = "litellm.internal"
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key), "-out", str(cert),
         "-days", "1", "-nodes", "-subj", f"/CN={host}", "-addext", f"subjectAltName=DNS:{host}"],
        check=True, capture_output=True,
    )
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or "0")
            self.rfile.read(length)
            seen.append((self.path, self.headers.get("Authorization")))
            body = b'{"id":"pinned"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    lookups = []
    real = socket.getaddrinfo

    def resolve(name, answered_port, *args, **kwargs):
        if name != host:
            return real(name, answered_port, *args, **kwargs)
        lookups.append((name, answered_port))
        if len(lookups) > 1:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 1))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setenv("FO_LITELLM_HOSTS", host)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    try:
        assert configure.proxy_request(f"https://{host}:{port}", KEY, "/health") == {"ok": True}
        assert lookups == [(host, None)]
        assert seen == [("/health", f"Bearer {KEY}")]
        lookups.clear()
        seen.clear()
        monkeypatch.setenv("ANDREA_LITELLM_API_KEY", KEY)
        monkeypatch.setenv("ANDREA_LITELLM_MODEL", "alias")
        monkeypatch.setenv("ANDREA_LITELLM_BASE_URL", f"https://{host}:{port}/v1")
        assert api.litellm_chat({}, timeout_seconds=5, error_prefix="lane") == {"id": "pinned"}
    finally:
        server.shutdown()
        server.server_close()
    assert lookups == [(host, None)]
    assert seen == [("/v1/chat/completions", f"Bearer {KEY}")]

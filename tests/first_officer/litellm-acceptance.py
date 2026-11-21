"""Prove native LiteLLM fallback and lane budgets with isolated local services."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import runpy
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import psycopg
import yaml

ROOT = Path(__file__).resolve().parents[2]
LOOPBACK = "127.0.0.1"
TEST_USER = "fo_acceptance"
PRIMARY = "acceptance-primary"
FALLBACK = "acceptance-fallback"
LANE_A = "acceptance-a"
LANE_B = "acceptance-b"
PROVIDER_KEY = "local-provider-fixture"
TOKEN_COST = 0.01
CALL_COST = 0.10
BUDGET = 0.20
OPENER = build_opener(ProxyHandler({}))


def allocated_ports():
    with socket.socket() as database, socket.socket() as proxy:
        database.bind((LOOPBACK, 0))
        proxy.bind((LOOPBACK, 0))
        return database.getsockname()[1], proxy.getsockname()[1]


def wait_for(check, timeout, failure, process=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and (process is None or process.poll() is None):
        if check():
            return
        time.sleep(0.1)
    raise AssertionError(failure)


@contextmanager
def fixture_process(args, sandbox, env, name):
    with (sandbox / f"{name}.log").open("w") as log:
        process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, cwd=sandbox, env=env)
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def request(base, path, key=None, payload=None):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(payload).encode() if payload is not None else None
    try:
        response = OPENER.open(Request(base + path, data=data, headers=headers), timeout=5)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


@contextmanager
def provider(name, calls, status, upstream_model):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            valid = (
                self.path == "/v1/chat/completions"
                and self.headers.get("Authorization") == f"Bearer {PROVIDER_KEY}"
                and payload["model"] == upstream_model
            )
            status_code = status if valid else 400
            calls.append((name, status_code))
            if status_code == 200:
                body = {
                    "id": "fixture-completion",
                    "object": "chat.completion",
                    "created": 1,
                    "model": upstream_model,
                    "choices": [{
                        "index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "served by fallback"},
                    }],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
                }
            else:
                body = {"error": {"message": "synthetic provider failure", "type": "server_error"}}
            encoded = json.dumps(body).encode()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    with ThreadingHTTPServer((LOOPBACK, 0), Handler) as server, ThreadPoolExecutor(1) as worker:
        task = worker.submit(server.serve_forever)
        try:
            yield f"http://{LOOPBACK}:{server.server_port}/v1"
        finally:
            server.shutdown()
            task.result()


def model(alias, model_name, base):
    return {
        "model_name": alias,
        "litellm_params": {
            "model": f"openai/{model_name}", "api_base": base, "api_key": PROVIDER_KEY,
            "input_cost_per_token": TOKEN_COST, "output_cost_per_token": TOKEN_COST,
        },
    }


def spend(db, lane):
    with db.cursor() as cursor:
        cursor.execute('SELECT spend FROM "LiteLLM_VerificationToken" WHERE key_alias = %s',
                       (f"first-officer-{lane}",))
        row = cursor.fetchone()
    assert row is not None, "Lane key missing from Postgres"
    return row[0]


def verify(base, admin_key, dsn, calls, configure, plan):
    lane_a = configure["provision_lane"](base, admin_key, LANE_A, plan["lanes"][LANE_A])
    lane_b = configure["provision_lane"](base, admin_key, LANE_B, plan["lanes"][LANE_B])
    assert lane_a != lane_b, "Lanes must have separate credentials"
    payload = {"model": PRIMARY, "messages": [{"role": "user", "content": "fixture"}], "max_tokens": 1}

    def complete(key, alias=PRIMARY):
        return request(base, "/v1/chat/completions", key, {**payload, "model": alias})

    status, _ = complete(None)
    assert status == 401, f"Unauthenticated inference returned {status}"
    assert not calls, "Unauthenticated inference reached a provider"

    with psycopg.connect(dsn, autocommit=True) as db:
        assert spend(db, LANE_A) == spend(db, LANE_B) == 0, "Lane spend must start at zero"
        status, result = complete(lane_a)
        assert status == 200, f"Failover completion returned {status}"
        assert result["choices"][0]["message"]["content"] == "served by fallback"
        assert calls == [(PRIMARY, 503), (FALLBACK, 200)], "Unexpected failover provider sequence"
        wait_for(lambda: math.isclose(spend(db, LANE_A), CALL_COST), 12,
                 "Fallback completion did not incur the expected persisted charge")

        status, result = complete(lane_a, FALLBACK)
        assert status == 200, f"Second completion before cumulative cap returned {status}"
        assert result["choices"][0]["message"]["content"] == "served by fallback"
        wait_for(lambda: math.isclose(spend(db, LANE_A), 2 * CALL_COST), 12,
                 "Cumulative spend did not persist")

        status, result = complete(lane_a)
        assert status == 422, f"Exhausted lane returned {status}"
        assert result["error"]["type"] == "budget_exceeded", "Refusal did not identify the budget"
        assert calls == [(PRIMARY, 503), (FALLBACK, 200), (FALLBACK, 200)], (
            "Exhausted lane reached a provider"
        )
        assert spend(db, LANE_B) == 0, "Lane A was charged against lane B"
        status, result = complete(lane_b, FALLBACK)
        assert status == 200, f"Independent lane returned {status}"
        assert result["choices"][0]["message"]["content"] == "served by fallback"
        assert calls == [(PRIMARY, 503), (FALLBACK, 200), (FALLBACK, 200), (FALLBACK, 200)]
        wait_for(lambda: math.isclose(spend(db, LANE_B), CALL_COST), 12,
                 "Independent lane charge did not persist")


def run(sandbox, admin_key, db_password):
    configure = runpy.run_path(str(ROOT / "first-officer/litellm/configure.py"))
    pg_bin = os.environ.get("FO_TEST_POSTGRES_BIN")
    if not pg_bin:
        pg_bin = subprocess.check_output(["pg_config", "--bindir"], text=True).strip()
    pg = Path(pg_bin)
    pg_port, proxy_port = allocated_ports()
    dsn = f"postgresql://{TEST_USER}:{db_password}@{LOOPBACK}:{pg_port}/postgres"
    proxy_base = f"http://{LOOPBACK}:{proxy_port}"
    env = {name: os.environ[name] for name in ("PATH", "LANG", "LD_LIBRARY_PATH") if name in os.environ}
    runtime_bin = Path(sys.executable).parent
    env.update({
        "PATH": f"{runtime_bin}:{pg}:{env.get('PATH', os.defpath)}",
        "DATABASE_URL": dsn,
        "LITELLM_MASTER_KEY": admin_key,
        "LITELLM_OFFLINE_MODE": "true",
        "LITELLM_TELEMETRY_ENABLED": "false",
    })
    password_file = sandbox / "database-password"
    password_file.write_text(db_password + "\n")
    with (sandbox / "initdb.log").open("w") as log:
        result = subprocess.run([
            pg / "initdb", "--no-instructions", "-D", sandbox / "data", "--username", TEST_USER,
            "--auth", "scram-sha-256", "--pwfile", password_file, "--no-locale", "--encoding", "UTF8",
        ], cwd=sandbox, env=env, stdout=log, stderr=subprocess.STDOUT)
    password_file.unlink()
    assert result.returncode == 0, f"Postgres initialization exited {result.returncode}"

    with ExitStack() as stack:
        calls = []
        primary = stack.enter_context(provider(PRIMARY, calls, 503, "fo-primary-fixture"))
        fallback = stack.enter_context(provider(FALLBACK, calls, 200, "fo-fallback-fixture"))
        original = {
            "model_list": [model(PRIMARY, "fo-primary-fixture", primary),
                           model(FALLBACK, "fo-fallback-fixture", fallback)],
            "general_settings": {"proxy_batch_write_at": 1},
            "router_settings": {"num_retries": 0},
        }
        plan = {
            "fallbacks": {PRIMARY: [FALLBACK]},
            "lanes": {lane: {"models": [PRIMARY, FALLBACK], "max_budget": BUDGET,
                             "budget_duration": "7d"} for lane in (LANE_A, LANE_B)},
        }
        config_path = sandbox / "litellm.yaml"
        config_path.write_text(yaml.safe_dump(configure["candidate_config"](original, plan)))
        database = stack.enter_context(fixture_process([
            pg / "postgres", "-D", sandbox / "data", "-h", LOOPBACK, "-p", str(pg_port), "-k", "",
        ], sandbox, env, "postgres"))

        def database_ready():
            result = subprocess.run([pg / "pg_isready", "-h", LOOPBACK, "-p", str(pg_port),
                                     "-U", TEST_USER, "-d", "postgres", "-q"], cwd=sandbox, env=env)
            return result.returncode == 0

        wait_for(database_ready, 10, "Postgres fixture did not start", database)
        proxy = stack.enter_context(fixture_process([
            runtime_bin / "litellm", "--config", config_path,
            "--host", LOOPBACK, "--port", str(proxy_port),
        ], sandbox, env, "litellm"))

        def proxy_ready():
            try:
                return request(proxy_base, "/health/readiness", admin_key)[0] == 200
            except (URLError, TimeoutError):
                return False

        wait_for(proxy_ready, 90, "LiteLLM fixture did not become ready", proxy)
        verify(proxy_base, admin_key, dsn, calls, configure, plan)


def print_fixture_failure(sandbox, admin_key, db_password):
    def redact(message):
        message = message.replace(admin_key, "[fixture-key]").replace(db_password, "[fixture-password]")
        message = re.sub(r"sk-[A-Za-z0-9_-]+", "[fixture-key]", message)
        return re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[loopback]", message)

    for path in sorted(sandbox.glob("*.log")):
        print(f"Redacted {path.name} tail", file=sys.stderr)
        tail = "\n".join(path.read_text(errors="replace").splitlines()[-20:])
        print(redact(tail), file=sys.stderr)
    print(redact(traceback.format_exc()), file=sys.stderr)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="fo-litellm-") as directory:
        sandbox = Path(directory)
        admin_key = "sk-" + secrets.token_hex(24)
        db_password = secrets.token_hex(24)
        try:
            run(sandbox, admin_key, db_password)
        except Exception:
            print_fixture_failure(sandbox, admin_key, db_password)
            sys.exit(1)
    print("LiteLLM acceptance passed. Primary HTTP 503 once, fallback HTTP 200 three times.")
    print("Two fixture charges took lane A to its 0.20 budget. Its next request")
    print("returned HTTP 422 with zero upstream calls. Lane B completed with 0.10 tracked spend.")

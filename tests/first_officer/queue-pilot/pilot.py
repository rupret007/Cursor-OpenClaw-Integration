"""Check the isolated Hatchet and Restate servers with synthetic local effects."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import multiprocessing
import os
import runpy
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
from threading import Event, Thread
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

QUEUE_DIR = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("queue", QUEUE_DIR / "first-officer/lanes/queue.py")
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)

HATCHET = "http://localhost:58888"
RESTATE = "http://localhost:8080"
ADMIN = "http://localhost:9070"
SCOPE = "queue-pilot"
LEDGER = Path(os.environ["PILOT_LEDGER"])
EFFECTS = Path(os.environ["PILOT_EFFECTS"])
OPENER = build_opener(ProxyHandler({}))


def http(base, path, payload=None, headers=None, timeout=3):
    request = Request(base + path, data=payload, headers=headers or {})
    try:
        response = OPENER.open(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, response.read()


def api(base, path, payload=None, headers=None, timeout=3):
    request_body = None if payload is None else json.dumps(payload).encode()
    code, body = http(base, path, request_body, {"Content-Type": "application/json", **(headers or {})}, timeout)
    return code, json.loads(body) if code < 300 else None


def eventually(probe, label, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(.15)
    raise AssertionError(f"Timed out checking {label}")


def request(lane="pilot"):
    return {"lane": lane, "head": secrets.token_hex(20), "pull_request": 1}


def state(job):
    with queue.locked_ledger(LEDGER, read_only=True) as file:
        return queue.queue_state(file, job, queue.idempotency_key(job))


def effect(job):
    with sqlite3.connect(EFFECTS) as db:
        rows = db.execute("SELECT started, finished FROM effects WHERE lane=? AND head=? AND pr=?",
                          (job["lane"], job["head"], job["pull_request"])).fetchall()
    assert len(rows) < 2
    return rows[0] if rows else None


def expect_failure(call, detail):
    try:
        call()
    except ValueError as exc:
        assert detail in str(exc), str(exc)
    else:
        raise AssertionError(f"Expected {detail}")


def log(message):
    print(message, flush=True)


@contextmanager
def relay(target, mode):
    """Inject loss before send or hold/drop the real backend reply."""
    received = Event()
    release = Event()
    replies = []
    requests = []
    errors = []

    class Relay(BaseHTTPRequestHandler):
        def do_GET(self):
            self.do_POST()

        def do_POST(self):
            requests.append(self.path)
            try:
                if mode == "drop-before" and "/send/" in self.path:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                length = int(self.headers.get("Content-Length", 0))
                code, data = http(
                    target, self.path, self.rfile.read(length) if length else None,
                    {"Content-Type": "application/json", "Authorization": self.headers["Authorization"],
                     **({"Idempotency-Key": self.headers["Idempotency-Key"]} if "Idempotency-Key" in self.headers else {})},
                )
                replies.append((code, json.loads(data) if code < 300 else {}))
                received.set()
                if mode == "drop" and ("/send/" in self.path or "/trigger" in self.path):
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if mode in ("hold", "hold-drop") and not release.wait(12):
                    raise AssertionError("Real reply was never released")
                if mode == "hold-drop" and self.command == "POST":
                    self.close_connection = True
                    return
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                errors.append(type(exc).__name__)
                received.set()
                self.send_error(502)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("localhost", 0), Relay) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://localhost:{server.server_port}", received, release, replies, requests
        finally:
            release.set()
            server.shutdown()
            thread.join()
            assert not errors, errors


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)


def launch(stack, label, *args, env=None):
    with (LEDGER.parent / label).open("a") as out:
        process = subprocess.Popen(args, stdout=out, stderr=out, start_new_session=True, env=env)
    stack.callback(stop, process)
    return process


def worker_done(backend, run, headers):
    if backend == "hatchet":
        _, result = api(HATCHET, f"/api/v1/stable/workflow-runs/{run}/status", headers=headers)
        assert result not in ("FAILED", "CANCELLED"), result
        return result == "COMPLETED"
    try:
        _, result = api(RESTATE, f"/restate/output/{run}")
    except TimeoutError:
        return False
    return result == {"done": True}


def acceptance(config, job):
    started = time.monotonic()
    receipt = queue.dispatch(config, job, LEDGER, timeout=5)
    return receipt, round((time.monotonic() - started) * 1000)


def check_workers(config, refused, headers):
    for backend, policy in (("hatchet", config), ("restate", refused)):
        jobs = [request("slow") for _ in range(3)]
        admitted = []
        for job in jobs:
            before = time.time_ns() // 1_000_000
            receipt, latency = acceptance(policy, job)
            assert receipt["backend"] == backend
            admitted.append((before, receipt["run"], latency))
        intervals = []
        for job, (before, run, latency) in zip(jobs, admitted, strict=True):
            eventually(lambda: worker_done(backend, run, headers), f"{backend} worker completion")
            started, finished = effect(job)
            intervals.append((started, finished, before, latency))
        intervals.sort()
        assert all(a[1] <= b[0] for a, b in zip(intervals, intervals[1:])), intervals
        log(f"{backend} slots=1: acceptance ms {[x[3] for x in intervals]}, submit-to-start ms "
            f"{[max(0, x[0] - x[2]) for x in intervals]}, max observed concurrency 1")


def dispatch_project(config, job, ledger):
    queue.dispatch(config, job, ledger)


def check_project_restart(config, headers):
    identity = {"repository": "example/integration", "issue": 40, "head": secrets.token_hex(20), "epoch": 2}
    job = {**identity, "lane": queue.project_lane(**identity)}
    with relay(RESTATE, "pass") as (fallback, _, _, _, fallback_calls):
        with relay(HATCHET, "hold-drop") as (url, received, release, real, calls):
            coding = deepcopy(config)
            coding["hatchet"].update(base_url=url, workflow=queue.PROJECT_WORKFLOW)
            coding["restate"]["base_url"] = fallback
            submitter = multiprocessing.get_context("spawn").Process(target=dispatch_project,
                                                                     args=(coding, job, LEDGER))
            submitter.start()
            try:
                assert received.wait(10), "Hatchet did not accept the coding submission"
                run = queue.accepted_run("hatchet", real[0][1])
                assert state(job)["phase"] == "pending"
                submitter.kill()
                submitter.join(timeout=5)
                assert submitter.exitcode == -9
                assert effect({**job, "pull_request": job["issue"]}) is None
            finally:
                EFFECTS.with_name("release-" + job["lane"]).touch()
                release.set()
                if submitter.is_alive():
                    submitter.kill()
                    submitter.join(timeout=5)
            expect_failure(lambda: queue.dispatch(coding, job, LEDGER), "was not sent again")
            assert len(calls) == 1 and not fallback_calls
            eventually(lambda: worker_done("hatchet", run, headers), "coding worker completed after submitter crash")
            assert effect({**job, "pull_request": job["issue"]})
            recovered = queue.reconcile_project(coding, job, LEDGER, run)
            assert recovered["run"] == run and recovered["phase"] == "queued"
            assert queue.dispatch(coding, job, LEDGER)["reused"] is True
            assert len(calls) == 2 and not fallback_calls
            log("A killed coding submitter recovered one verified Hatchet effect using one trigger and one lookup")


def joined_project_rehearsal(stack, home, authority, identity, job, coding, headers):
    """Exercise real native gateway admission and exact verification with fixture evidence."""
    assert not Path("/usr/bin/gh").exists(), "The joined fixture requires its private GitHub adapter"
    wire = runpy.run_path(str(QUEUE_DIR / "tests/first_officer/project-telegram-wire.py"))
    release, node, cli = (Path(os.environ[name]).resolve(strict=True) for name in
                          ("FO_PROJECT_WIRE_RELEASE", "FO_PROJECT_WIRE_NODE", "FO_PROJECT_WIRE_CLI"))
    gh = home / ".local/bin/gh"
    gh.parent.mkdir(parents=True)
    gh.write_text("""#!/usr/bin/python3
import json
from pathlib import Path
import sys
home = Path.home()
data = json.loads((home / "github-fixture.json").read_text())
base, candidate = data["base"], data["candidate"]
route = sys.argv[4]
root = "repos/example/integration/"
if route == root + "issues/41":
    response = {"number": 41, "state": "open", "repository_url": "https://api.github.com/" + root[:-1]}
elif route == root + "git/ref/heads/main":
    response = {"ref": "refs/heads/main", "object": {"sha": base, "type": "commit"}}
elif candidate and route == root + "pulls/13":
    response = {"number": 13, "state": "open", "head": {"sha": candidate},
                "base": {"ref": "main", "repo": {"full_name": "example/integration"}}}
elif candidate and route == root + "compare/" + base + "..." + candidate:
    response = {"status": "ahead", "merge_base_commit": {"sha": base}}
elif candidate and route == root + "commits/" + candidate + "/check-runs?per_page=100&filter=latest":
    response = {"total_count": 1, "check_runs": [{"name": "independent-wire", "status": "completed",
                 "conclusion": "success", "head_sha": candidate, "app": {"slug": "github-actions"}}]}
else:
    sys.exit(2)
print(json.dumps(response))
""")
    gh.chmod(0o700)
    save = wire["write"]
    save(home / "github-fixture.json", {"base": identity["head"], "candidate": None})
    gate = home / "gate"
    gate.mkdir()
    (gate / "fullgate.lock").touch()
    save(authority / "gate.json", {"directory": str(gate), "lock": "fullgate.lock", "hold": "HOLD"})
    save(authority / "capacity.json", {"commands": ["fixture-coding-capacity"], "limit": 1})
    save(authority / "project-queue.json", coding)
    ledger = authority / "project-queue-receipts.jsonl"
    roster = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), wire["BotApi"]))
    config = wire["configure"](roster, home, release)
    thread = Thread(target=roster.serve_forever)
    thread.start()
    stack.callback(thread.join)
    stack.callback(roster.shutdown)
    env = {name: os.environ[name] for name in ("FO_HATCHET_PILOT", "FO_RESTATE_PILOT")}
    command = "captain this: advance example/integration #41"
    candidate_state = authority / "reviews/state.json"

    def inspect_saved():
        data = json.loads(candidate_state.read_text())
        return data["projectJobs"][0] if data.get("projectJobs") else {}

    def inspect_now():
        env_state = {**env, "HOME": str(home), "PATH": "/usr/bin:/bin", "FO_LOCAL_NODE": "nucbox"}
        result = subprocess.check_output(
            [sys.executable, "-I", "-B", str(release / "round6/runtime/segment1/captain-state.py"), "inspect-project"],
            input=json.dumps(identity).encode(), env=env_state, stderr=subprocess.DEVNULL, timeout=24,
        )
        return json.loads(result)["result"]

    def project_deliveries_settled(expected):
        journal = json.loads(candidate_state.read_text()).get("projectTelegram", [])
        return len(journal) == expected and all(row["state"] == "delivered" for row in journal)

    def outbox_settled():
        state = list((home / "gateway-state").rglob("openclaw.sqlite"))
        if len(state) != 1:
            return False
        db = sqlite3.connect(state[0].as_uri() + "?mode=ro", uri=True, timeout=.2)
        try:
            return db.execute("SELECT COUNT(*) FROM delivery_queue_entries").fetchone()[0] == 0
        except sqlite3.OperationalError:
            return False
        finally:
            db.close()

    with wire["gateway"](node, cli, home, config, roster, env):
        roster.admission_bridge = os.environ.get("FO_PROJECT_BOT_SOCKET")
        try:
            assert wire["exercise"](roster, "Admitted coding", 100, command, "Hatchet accepted the coding job", 30)
        finally:
            roster.admission_bridge = None
        with queue.locked_ledger(ledger, read_only=True) as stored:
            accepted = queue.queue_state(stored, job, queue.idempotency_key(job))
        assert accepted and accepted["phase"] == "queued"
        eventually(lambda: worker_done("hatchet", accepted["run"], headers), "joined Hatchet worker")
        output = queue.read_project_run(coding, job, accepted, accepted["run"], 3)["output"]
        eventually(lambda: project_deliveries_settled(1), "confirmed admission reply")
        eventually(lambda: inspect_saved().get("state") == "patch-ready",
                   "native monitor observed the candidate", timeout=40)
        assert inspect_saved()["commit"] == output["commit"]
        save(home / "github-fixture.json", {"base": identity["head"], "candidate": output["commit"]})
        save(authority / "project-verification.json",
             {"repository": identity["repository"], "issue": identity["issue"],
              "commit": output["commit"], "pr": 13, "checks": ["independent-wire"]})
        eventually(lambda: inspect_saved().get("state") == "verified",
                   "native monitor verified the exact candidate", timeout=55)
        assert wire["exercise"](roster, "Verified handoff", 101, "handoff", "all required CI checks", 30)
        delivered = len(roster.replies)
        eventually(lambda: project_deliveries_settled(2), "confirmed Captain handoff receipt")
        eventually(outbox_settled, "verified native Telegram delivery checkpoint")
        assert len(roster.replies) == delivered, "The first gateway sent an extra reply"
    with wire["gateway"](node, cli, home, config, roster, env):
        assert len(roster.replies) == delivered, "Restart replayed a confirmed Telegram reply"
        assert wire["exercise"](roster, "Duplicate after restart", 102, command, "already has a receipt", 30)
        eventually(lambda: project_deliveries_settled(3), "confirmed post-restart receipt")
        save(home / "github-fixture.json", {"base": identity["head"], "candidate": "f" * 40})
        assert inspect_now() == "stale-candidate"
    entries = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [entry["phase"] for entry in entries] == ["pending", "queued"]
    assert roster.model_calls == 0, "The chat model was outside the coding path"
    log("Native telegram admission, Hatchet, scoped edit, verification, replay and stale-head checks passed")
    return accepted


def check_project_worker(stack, config, headers):
    home = LEDGER.parent / "project-worker-home"
    authority = home / ".local/state/openclaw-captain"
    model = home / ".local/state/openclaw-nuc/litellm"
    checkout = home / "source"
    (authority / "reviews").mkdir(parents=True)
    model.mkdir(parents=True)
    checkout.mkdir()
    worker_source = QUEUE_DIR / "first-officer/workers"
    worker = runpy.run_path(str(worker_source / "project-worker.py"))
    git, save = worker["git"], worker["save"]
    git(checkout, "init", "--quiet")
    git(checkout, "remote", "add", "origin", "https://github.com/example/integration.git")
    (checkout / "review.mjs").write_text('const delivery = "old";\n')
    git(checkout, "add", "review.mjs")
    git(checkout, "commit", "-qm", "Fixture base")
    head = git(checkout, "rev-parse", "HEAD").decode().strip()
    identity = {"repository": "example/integration", "issue": 41, "head": head, "epoch": 2}
    job = {**identity, "lane": queue.project_lane(**identity)}
    save(authority / "lease.json", {"node": "nucbox", "active": True, "epoch": 2})
    save(authority / "project-worker.json", {"request": identity, "checkout": str(checkout),
                                            "file": "review.mjs", "before": '"old"', "prefix": "",
                                            "suffix": "", "prompt": "Draft a neutral delivery status."})
    fingerprint = worker["digest"]((authority / "project-worker.json").read_bytes())
    save(authority / "project-grant.json", {
        **identity, "branch": "main", "worker": "local-qwen", "expires": int(time.time()) + 120,
        "planSha256": fingerprint,
    })
    joined = bool(os.environ.get("FO_PROJECT_WIRE_RELEASE"))
    if not joined:
        save(authority / "reviews/state.json", {"projectJobs": [
            {"request": job, "branch": "main", "worker": "local-qwen", "state": "reserved", "planSha256": fingerprint},
        ]})
    save(model / "litellm.yaml", {
        "model_list": [{"model_name": "qwen-local-fast",
                        "litellm_params": {"model": "openai/qwen-local-fast", "api_base": "http://127.0.0.1:41501/v1"}}],
        "router_settings": {"fallbacks": [], "num_retries": 0},
    })
    save(model / "lane-policy.json", {"lanes": {"gateway": {"models": ["qwen-local-fast"]}}})
    (model / "gateway-lane.key").write_text("fixture-model-key")
    calls = []

    class ModelStub(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/v1/chat/completions" and payload["model"] == "qwen-local-fast"
            calls.append(True)
            response = json.dumps({"choices": [{"message": {"content": '{"replacement":"Delivery is unconfirmed."}'}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_):
            pass

    server = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 41502), ModelStub))
    thread = Thread(target=server.serve_forever)
    thread.start()
    stack.callback(thread.join)
    stack.callback(server.shutdown)
    python = Path("hatchet-worker/bin/python").absolute()
    launch(stack, "hatchet-project-worker.log", str(python), str(worker_source / "hatchet-project.py"),
           env={**os.environ, "HOME": str(home), "FO_PROJECT_PYTHON": str(python)})
    time.sleep(3)
    coding = deepcopy(config)
    coding["hatchet"]["workflow"] = queue.PROJECT_WORKFLOW
    receipt = (joined_project_rehearsal(stack, home, authority, identity, job, coding, headers)
               if joined else queue.dispatch(coding, job, LEDGER))
    eventually(lambda: worker_done("hatchet", receipt["run"], headers), "scoped Hatchet coding worker")
    row = queue.read_project_run(coding, job, receipt, receipt["run"], 3)
    result = row["output"]
    assert set(result) == {"lane", "head", "commit"} and result["lane"] == job["lane"] and result["head"] == head
    assert git(checkout, "rev-parse", result["commit"] + "^").decode().strip() == head
    assert git(checkout, "show", result["commit"] + ":review.mjs") == b'const delivery = "Delivery is unconfirmed.";\n'
    assert (checkout / "review.mjs").read_text() == 'const delivery = "old";\n'
    if not joined:
        assert queue.dispatch(coding, job, LEDGER)["reused"]
    assert len(calls) == 1
    log("The dedicated Hatchet coding worker made one isolated candidate commit from one loopback model call")


def main():
    token = Path("hatchet-token").read_text().strip()
    part = token.split(".")[1]
    tenant = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["sub"]
    headers = {"Authorization": "Bearer " + token}
    os.environ.update(HATCHET_CLIENT_TOKEN=token, HATCHET_CLIENT_TLS_STRATEGY="none",
                      HATCHET_CLIENT_HOST_PORT="localhost:57070",
                      FO_HATCHET_PILOT=token, FO_RESTATE_PILOT="synthetic")
    config = {
        "hatchet": {"base_url": HATCHET, "tenant": tenant, "workflow": "queue-pilot", "token_env": "FO_HATCHET_PILOT"},
        "restate": {"base_url": RESTATE, "service": "QueuePilot", "handler": "record",
                    "scope": SCOPE, "token_env": "FO_RESTATE_PILOT"},
    }
    with sqlite3.connect(EFFECTS) as db:
        db.execute("""CREATE TABLE effects (
            lane TEXT, head TEXT, pr INTEGER, started INTEGER, finished INTEGER,
            PRIMARY KEY(lane, head, pr)
        )""")
    with ExitStack() as stack:
        # A bound non-listening loopback port guarantees the negative connect result.
        listener = stack.enter_context(socket.socket())
        listener.bind(("localhost", 0))
        refused = deepcopy(config)
        refused["hatchet"]["base_url"] = f"http://localhost:{listener.getsockname()[1]}"
        hatchet_worker = launch(stack, "hatchet-worker.log", "hatchet-worker/bin/python", str(Path(__file__).with_name("hatchet_task.py")))
        restate_worker = launch(stack, "restate-worker.log", "node", "restate-worker/restate-worker.mjs")
        eventually(lambda: api(ADMIN, "/deployments", {"uri": "http://localhost:59080"})[0] < 300,
                   "Restate registration")
        restate_cli = next(Path.cwd().glob("restate-cli-*/restate"))
        with (LEDGER.parent / "restate-rule.log").open("w") as logfile:
            subprocess.run([restate_cli, "rules", "set", SCOPE, "--concurrency", "1"],
                           check=True, stdout=logfile, stderr=logfile, timeout=15)
        time.sleep(3)
        assert hatchet_worker.poll() is None and restate_worker.poll() is None
        check_workers(config, refused, headers)
        check_project_restart(config, headers)

        job = request()
        with relay(RESTATE, "pass") as (unused_restate, _, _, _, fallback_calls):
            with relay(HATCHET, "drop") as (url, _, _, real, calls):
                lost = deepcopy(config)
                lost["hatchet"]["base_url"] = url
                lost["restate"]["base_url"] = unused_restate
                expect_failure(lambda: acceptance(lost, job), "Hatchet outcome is unknown")
                run = queue.accepted_run("hatchet", real[0][1])
                assert len(calls) == 1 and state(job)["backend"] == "hatchet" and state(job)["phase"] == "uncertain"
                expect_failure(lambda: acceptance(lost, job), "was not sent again")
                assert len(calls) == 1 and not fallback_calls
                eventually(lambda: worker_done("hatchet", run, headers), "Hatchet accepted before lost reply")
        trigger = f"/api/v1/stable/tenants/{tenant}/workflow-runs/trigger"
        engine_log = Path("logs/hatchet")
        collision_marker = "idempotency key collision"
        previous_collisions = engine_log.read_text().lower().count(collision_marker)
        collision, _ = api(HATCHET, trigger, {"workflowName": "queue-pilot", "input": job, "returnOnlyId": True,
                                              "additionalMetadata": {"idempotencyKey": queue.idempotency_key(job)}}, headers)
        assert collision == 500 and effect(job)
        eventually(lambda: engine_log.read_text().lower().count(collision_marker) > previous_collisions,
                   "Hatchet recorded the idempotency collision")
        log(f"Hatchet dropped reply stayed on Hatchet and worker completed; duplicate key HTTP {collision}")

        job = request()
        with relay(RESTATE, "drop") as (url, _, _, real, calls):
            lost = deepcopy(refused)
            lost["restate"]["base_url"] = url
            expect_failure(lambda: acceptance(lost, job), "Restate outcome is unknown")
            assert state(job)["backend"] == "restate" and state(job)["phase"] == "uncertain"
            alternate = deepcopy(lost)
            alternate["restate"]["scope"] = "other-scope"
            body = {"target": "idempotentInvocation", "scope": "other-scope", "service": "QueuePilot",
                    "handler": "record", "idempotencyKey": queue.idempotency_key(job)}
            nonexistent = api(RESTATE, "/restate/lookup", body)[1]["invocationId"]
            assert nonexistent != queue.accepted_run("restate", real[0][1])
            assert api(RESTATE, f"/restate/status/{nonexistent}")[0] == 404
            expect_failure(lambda: queue.reconcile_restate(alternate, job, LEDGER), "original Restate destination")
            assert len(calls) == 1
            # Reconcile goes through the same relay URL; only the original send response is dropped.
            receipt = queue.reconcile_restate(lost, job, LEDGER)
            assert receipt["run"] == queue.accepted_run("restate", real[0][1])
            assert calls[1:] == ["/restate/lookup", f"/restate/status/{receipt['run']}"]
        send_path, _ = queue.restate_route(config["restate"])
        code, duplicate = api(RESTATE, send_path, job, {"Idempotency-Key": queue.idempotency_key(job)})
        assert code == 202 and duplicate["status"] == "PreviouslyAccepted"
        assert duplicate["invocationId"] == receipt["run"]
        log("Restate recovered the real invocation by scoped lookup and status; duplicate reused ID; other scope status HTTP 404")

        job = request()
        with relay(RESTATE, "drop-before") as (url, _, _, _, calls):
            lost["restate"]["base_url"] = url
            expect_failure(lambda: acceptance(lost, job), "Restate outcome is unknown")
            expect_failure(lambda: queue.reconcile_restate(lost, job, LEDGER), "remains unresolved")
            assert state(job)["phase"] == "uncertain" and len(calls) == 3
        log("Restate returned a deterministic lookup ID even for a missing invocation; no receipt was recorded")

        job = request()
        with relay(RESTATE, "drop") as (url, _, _, real, _):
            lost["restate"]["base_url"] = url
            expect_failure(lambda: acceptance(lost, job), "Restate outcome is unknown")
            run = queue.accepted_run("restate", real[0][1])
            eventually(lambda: worker_done("restate", run, {}), "Restate completion before expiration")
            key = queue.idempotency_key(job)
            body.update(scope=SCOPE, idempotencyKey=key)
            assert api(RESTATE, "/restate/lookup", body)[1]["invocationId"] == run
            eventually(lambda: api(RESTATE, f"/restate/status/{run}")[0] == 404, "Restate receipt expiry", timeout=25)
            expect_failure(lambda: queue.reconcile_restate(lost, job, LEDGER), "remains unresolved")
            assert state(job)["phase"] == "uncertain"
            expect_failure(lambda: acceptance(lost, job), "was not sent again")
        log("Expired real Restate receipt left the original submission unresolved")

        crash = request("crash")
        receipt, _ = acceptance(refused, crash)
        before = eventually(lambda: effect(crash), "commit before killing Restate worker")
        restate_worker.kill()
        restate_worker.wait(timeout=5)
        launch(stack, "restate-worker.log", "node", "restate-worker/restate-worker.mjs")
        eventually(lambda: worker_done("restate", receipt["run"], {}), "Restate replay after worker kill")
        assert effect(crash) == before
        log("Restate completed after worker restart with the original single committed effect")

        job = request()
        other = request("parallel")
        while other["head"] == job["head"] or queue.idempotency_key(other)[-1] == queue.idempotency_key(job)[-1]:
            other = request("parallel")
        with relay(HATCHET, "hold") as (url, received, release, _, calls):
            slow = deepcopy(config)
            slow["hatchet"]["base_url"] = url
            with ThreadPoolExecutor(max_workers=3) as workers:
                first = workers.submit(acceptance, slow, job)
                assert received.wait(5) and state(job)["phase"] == "pending"
                duplicate = workers.submit(acceptance, slow, job)
                receipt, elapsed = workers.submit(acceptance, config, other).result(timeout=4)
                assert not first.done() and not duplicate.done()
                release.set()
                initial, _ = first.result(timeout=5)
                repeat, _ = duplicate.result(timeout=5)
            assert len(calls) == 1 and repeat["reused"] and initial["run"] == repeat["run"]
            for run in (initial["run"], receipt["run"]):
                eventually(lambda: worker_done("hatchet", run, headers), "Hatchet completion after overlapping dispatch")
            assert effect(job) and effect(other)
            log(f"Concurrent duplicate posted once; an unrelated admission finished in {elapsed} ms while the reply was held")

        job = request()
        with relay(HATCHET, "hold") as (url, received, release, real, _):
            slow["hatchet"]["base_url"] = url
            with ThreadPoolExecutor(max_workers=1) as workers:
                sending = workers.submit(acceptance, slow, job)
                assert received.wait(5)
                pending = state(job)
                run = queue.accepted_run("hatchet", real[0][1])
                reconciled = {**pending, "run": run, "phase": "queued", "at": pending["at"] + 1}
                queue.transition(LEDGER, job, pending["key"], pending, reconciled)
                release.set()
                expect_failure(lambda: sending.result(timeout=5), "ledger changed")
            assert state(job) == reconciled
            log("Late real Hatchet reply could not overwrite a newer durable receipt")

        stop(hatchet_worker)
        check_project_worker(stack, config, headers)


if __name__ == "__main__":
    main()

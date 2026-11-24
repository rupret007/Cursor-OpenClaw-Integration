from concurrent.futures import Future
from datetime import datetime, timezone
import http.client
import importlib
import json
from pathlib import Path
import runpy
import ssl
import subprocess
import sys
from types import SimpleNamespace
import threading
import time

import pytest


from standby_fixture import certificates


def test_daily_brief_survives_restart_and_fences_the_old_holder(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "first-officer"))
    queue = importlib.import_module("standby_queue")
    lease = importlib.import_module("standby")
    wall = datetime(2026, 11, 23, 12, 59, tzinfo=timezone.utc).timestamp()
    monotonic = 0
    policy = tmp_path / "project-daily.json"
    policy.touch(mode=0o600)
    policy.write_text('{"enabled":true}')
    first = {"repository": "fixture/integration", "issue": 40, "action": "investigate"}
    second = {"repository": "fixture/dashboard", "issue": 18, "action": "investigate"}
    snapshot = {"projects": [first, second], "recommended": first}
    with lease.StandbyAuthority(tmp_path, clock=lambda: monotonic) as authority:
        primary = authority.claim(lease.PRIMARY)
        daily = queue.DailyBriefing(tmp_path, clock=lambda: wall)
        assert daily.due(authority, primary) is None
        wall += 60
        due = daily.due(authority, primary)
        assert due == {"epoch": 1, "date": "2026-11-23"}
        incomplete = {"projects": [first, {**second, "action": "refresh"}], "recommended": first}
        assert daily.save(authority, primary, due["date"], incomplete) == {"result": "evidence-unavailable"}
        assert daily.latest() is None
        assert daily.save(authority, primary, due["date"], snapshot) == {"result": "saved"}
        monotonic += lease.LEASE_SECONDS
        mac = authority.claim(lease.STANDBY)
        assert daily.due(authority, mac) is None
        with pytest.raises(lease.LeaseRejected):
            daily.save(authority, primary, due["date"], snapshot)
        assert daily.save(authority, mac, due["date"], snapshot) == {"result": "duplicate"}
    reopened = queue.DailyBriefing(tmp_path, clock=lambda: wall)
    assert reopened.latest()["epoch"] == 1 and reopened.latest()["recommended"] == first
    assert reopened.latest()["projects"] == [first, second]


def test_reviewed_project_releases_only_its_reservation_and_cannot_be_requeued(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "first-officer"))
    queue_module = importlib.import_module("standby_queue")
    lease = importlib.import_module("standby")
    profile = {
        "hatchet": {"base_url": "http://127.0.0.1:1", "tenant": "01010101",
                    "workflow": "fo-captain-project", "token_env": "FO_FIXTURE_HATCHET"},
        "restate": {"base_url": "http://127.0.0.1:2", "service": "CaptainReview", "handler": "verify",
                    "scope": "fixture", "token_env": "FO_FIXTURE_RESTATE"},
    }
    monkeypatch.setenv("FO_FIXTURE_HATCHET", "fixture-token")
    monkeypatch.setenv("FO_FIXTURE_RESTATE", "fixture-token")
    project = queue_module.ProjectQueue(tmp_path, profile, clock=lambda: 100)
    base = {"repository": "example/integration", "head": "a" * 40, "epoch": 1}

    def request(issue):
        identity = {**base, "issue": issue}
        return {**identity, "lane": queue_module.queue.project_lane(**identity)}

    def private(path, data):
        path.touch(mode=0o600, exist_ok=True)
        path.write_text(json.dumps(data))

    first, next_job = request(40), request(41)
    commit = "b" * 40
    run = "fixture-reviewed-run-1"
    submissions = []

    def post(*_args):
        submissions.append(True)
        return {"metadata": {"id": f"fixture-reviewed-run-{len(submissions)}"}}

    monkeypatch.setattr(queue_module.queue, "post", post)
    status = "RUNNING"

    def read_run(*_args):
        return {"status": status, "output": {"lane": first["lane"], "head": first["head"], "commit": commit}}

    monkeypatch.setattr(queue_module.queue, "read_project_run", read_run)
    with lease.StandbyAuthority(tmp_path, clock=lambda: 100) as authority:
        grant = authority.claim(lease.PRIMARY)

        def scope(item):
            private(project.scope, {"request": item, "epoch": 1, "expires": 150,
                                    "worker": "local-qwen", "planSha256": "c" * 64})

        scope(first)
        assert project.submit(authority, grant, first)["run"] == run
        with pytest.raises(queue_module.ProjectScopeRejected):
            project.retire(authority, grant, first)
        approval = {"request": first, "epoch": 1, "expires": 150, "run": run, "commit": commit}
        private(tmp_path / "project-complete.json", approval)
        with pytest.raises(queue_module.ProjectScopeRejected):
            project.retire(authority, grant, first)
        assert len(project.active()) == 1
        status = "COMPLETED"
        private(tmp_path / "project-complete.json", {**approval, "commit": "d" * 40})
        with pytest.raises(queue_module.ProjectScopeRejected):
            project.retire(authority, grant, first)
        private(tmp_path / "project-complete.json", approval)
        assert project.retire(authority, grant, first)["commit"] == commit
        assert project.active() == []
        project = queue_module.ProjectQueue(tmp_path, profile, clock=lambda: 100)
        assert project.submit(authority, grant, first)["retired"] is True
        assert project.receipt(first)["run"] == run
        assert len(submissions) == 1
        scope(next_job)
        queued = project.submit(authority, grant, next_job)
        assert queued["phase"] == "queued" and queued["run"] != run
        assert project.active()[0]["request"] == next_job
        assert len(submissions) == 2


def test_broker_entrypoint_loads_release_modules_in_isolated_python(tmp_path):
    broker = Path(__file__).resolve().parents[2] / "first-officer/standby_broker.py"
    result = subprocess.run([sys.executable, "-I", "-B", str(broker), "--help"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "--telegram-config" in result.stdout


def test_portable_queue_environment_is_private_and_limited(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "first-officer"))
    transport = importlib.import_module("standby_broker")
    name = "FO_HATCHET_LANE_TOKEN"
    monkeypatch.setenv(name, "existing-fixture")
    config = tmp_path / "queue-environment.json"
    config.write_text(json.dumps({name: "fixture-token", "PATH": "unexpected"}))
    config.chmod(0o600)
    with pytest.raises(ValueError, match="private broker queue environment"):
        transport.load_queue_environment(config)
    assert transport.os.environ[name] == "existing-fixture"
    config.write_text(json.dumps({name: "fixture-token"}))
    config.chmod(0o644)
    with pytest.raises(ValueError, match="private authority file"):
        transport.load_queue_environment(config)
    config.chmod(0o600)
    transport.load_queue_environment(config)
    assert transport.os.environ[name] == "fixture-token"


def telegram_update(update_id, message_id, author=101):
    return {"update_id": update_id, "message": {"message_id": message_id, "date": 1, "text": "Fixture command",
            "chat": {"id": author, "type": "private"}, "from": {"id": author, "is_bot": False}}}


def test_broker_authenticates_each_host_and_fences_partitioned_primary(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "first-officer"))
    transport = importlib.import_module("standby_broker")
    host = importlib.import_module("standby_host")
    channel_module = importlib.import_module("standby_channel")
    lease = importlib.import_module("standby")
    certificates(tmp_path, ("broker", "nuc", "mac", "unregistered"))

    ca = tmp_path / "ca.pem"
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    peers = {transport.client_fingerprint(tmp_path / (name + ".pem")): node
             for name, node in (("nuc", transport.PRIMARY), ("mac", transport.STANDBY))}
    telegram = importlib.import_module("standby_telegram")
    inbox = telegram.TelegramInbox(state, 101, 202)
    poll = telegram.TelegramPoll(inbox, lambda _offset: [telegram_update(1, 11), telegram_update(2, 12),
                                                        telegram_update(3, 13)])
    poll.step()
    now = 0
    sends = []
    typing = []
    queue_calls = []
    queue_reads = []
    result_head = "a" * 40
    profile = {
        "hatchet": {"base_url": "http://127.0.0.1:1", "tenant": "01010101",
                    "workflow": "fo-captain-review", "token_env": "FO_FIXTURE_HATCHET"},
        "restate": {"base_url": "http://127.0.0.1:2", "service": "CaptainReview",
                    "handler": "verify", "token_env": "FO_FIXTURE_RESTATE"},
    }
    review = transport.ReviewQueue(state, profile, clock=lambda: now)
    project_profile = {**profile, "hatchet": {**profile["hatchet"], "workflow": "fo-captain-project"}}
    project = transport.ProjectQueue(state, project_profile, clock=lambda: now)
    approved = {"lane": "fixture-review", "head": "a" * 40, "pull_request": 123}
    uncertain = {**approved, "lane": "uncertain-review"}

    def scope(epoch, request):
        review.scope.touch(mode=0o600, exist_ok=True)
        review.scope.write_text(json.dumps({"epoch": epoch, "request": request, "expires": 300}))

    queue = importlib.import_module("lanes.queue")

    worker = runpy.run_path(str(Path(__file__).resolve().parents[2] / "first-officer/workers/project-worker.py"))
    checkout = tmp_path / "repo"
    checkout.mkdir(mode=0o700)
    git = worker["git"]
    git(checkout, "init", "--quiet")
    git(checkout, "remote", "add", "origin", "https://github.com/example/integration.git")
    source = checkout / "review.mjs"
    source.write_text('const status = "old";\n')
    git(checkout, "add", source.name)
    git(checkout, "commit", "-qm", "Approved base")
    base = git(checkout, "rev-parse", "HEAD").decode().strip()

    def coding_request(issue):
        request = {"repository": "example/integration", "issue": issue, "head": base, "epoch": 1}
        return {**request, "lane": queue.project_lane(**request)}

    coding = coding_request(40)
    candidate = coding_request(41)
    exact_project_readback = False
    project_commit = "b" * 40
    hatchet_available = True

    def coding_scope(request, plan=None):
        project.scope.touch(mode=0o600, exist_ok=True)
        fields = {"epoch": 1, "request": request, "expires": 300, "worker": "local-qwen"}
        if plan is not None:
            fields["planSha256"] = plan
        project.scope.write_text(json.dumps(fields))

    monkeypatch.setenv("FO_FIXTURE_HATCHET", "fixture-token")
    monkeypatch.setenv("FO_FIXTURE_RESTATE", "fixture-token")

    def queue_post(_opener, url, _headers, payload, _timeout):
        queue_calls.append((url, payload))
        if payload.get("input") in (uncertain, coding):
            raise queue.Uncertain("Fixture dropped acceptance reply")
        return {"metadata": {"id": "fixture-run-1"}}

    def queue_get(_opener, request, _timeout, expected_type=dict):
        queue_reads.append(request.full_url)
        if request.full_url.endswith("/fixture-project-run"):
            if not hatchet_available:
                raise queue.Uncertain("Fixture worker unavailable")
            original = coding if exact_project_readback else candidate
            return {"run": {"metadata": {"id": "fixture-project-run"}, "tenantId": profile["hatchet"]["tenant"],
                            "additionalMetadata": {"idempotencyKey": queue.idempotency_key(coding)},
                            "input": {"input": original}, "status": "COMPLETED",
                            "output": {"lane": coding["lane"], "head": coding["head"], "commit": project_commit}},
                    "workflowConfig": {"Name": "fo-captain-project"}}
        if expected_type is str:
            return "COMPLETED"
        return {"run": {"metadata": {"id": "fixture-run-1"}, "output": {
            "lane": approved["lane"], "pr": approved["pull_request"], "head": result_head, "ci": "passing",
        }}}

    monkeypatch.setattr(queue, "post", queue_post)
    monkeypatch.setattr(queue, "response_json", queue_get)
    ready = Future()
    active_progress = threading.Barrier(2, timeout=1)

    def send_message(owner, text, message_id):
        if message_id == 13 and text == "On it.":
            active_progress.wait()
        sends.append((owner, text, message_id))
        if message_id in (12, 14) and text != "On it.":
            raise TimeoutError
        return {"chat": {"id": owner}, "message_id": 400 + message_id}

    def send_typing(owner):
        active_progress.wait()
        typing.append(owner)
        return True

    def serve():
        try:
            with transport.StandbyAuthority(state, clock=lambda: now) as authority, transport.Broker(
                    ("127.0.0.1", 0), authority, cert=tmp_path / "broker.pem", key=tmp_path / "broker.key",
                    ca=ca, peers=peers, effects={"telegram-reply": inbox.reply_adapter(send_message),
                                               "telegram-typing": inbox.typing_adapter(send_typing)},
                    inbox=inbox, poll=poll, review=review, project=project) as broker:
                ready.set_result(broker)
                broker.serve_forever(poll_interval=0.05)
        except Exception as error:
            if not ready.done():
                ready.set_exception(error)
                return
            raise

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    broker = ready.result(timeout=5)

    def request(peer, path, fields):
        context = ssl.create_default_context(cafile=ca)
        if peer:
            context.load_cert_chain(tmp_path / (peer + ".pem"), tmp_path / (peer + ".key"))
        client = http.client.HTTPSConnection("127.0.0.1", broker.server_port, context=context, timeout=4)
        try:
            client.request("POST", path, json.dumps(fields), {"Content-Type": "application/json"})
            reply = client.getresponse()
            return reply.status, json.loads(reply.read())
        finally:
            client.close()

    def client(peer, hostname="127.0.0.1"):
        return transport.Client(hostname, broker.server_port, cert=tmp_path / (peer + ".pem"),
                                key=tmp_path / (peer + ".key"), ca=ca)

    def effect(peer, epoch, message_id):
        fields = {"epoch": epoch, "kind": "telegram-reply",
                  "payload": {"message_id": message_id, "phase": "final", "text": "Fixture response"}}
        return request(peer, "/v1/effect", fields)

    def acknowledge(peer, epoch, message_id):
        return request(peer, "/v1/effect", {"epoch": epoch, "kind": "telegram-reply",
                       "payload": {"message_id": message_id, "phase": "ack"}})

    def next_message(peer, epoch):
        return request(peer, "/v1/telegram/next", {"epoch": epoch})

    def complete(peer, epoch, message_id):
        return request(peer, "/v1/telegram/complete", {"epoch": epoch, "message_id": message_id})

    try:
        with pytest.raises((ssl.SSLError, ConnectionError)):
            request(None, "/v1/claim", {})
        assert request("unregistered", "/v1/claim", {}) == (403, {"error": "peer-not-registered"})
        nuc = client("nuc")
        mac = client("mac")
        with pytest.raises(transport.BrokerUnavailable):
            client("nuc", "localhost").claim()
        active_gateways = set()

        def launcher(node):
            def start(epoch):
                marker = (node, epoch)
                active_gateways.add(marker)
                return SimpleNamespace(ready=lambda: True, exited=lambda: False,
                                       stop=lambda: active_gateways.remove(marker))

            return start

        primary = host.Supervisor(nuc, launcher(transport.PRIMARY), clock=lambda: now)
        standby = host.Supervisor(mac, launcher(transport.STANDBY), clock=lambda: now)
        assert standby.step() == "parked"
        assert primary.step() == "active"
        assert active_gateways == {(transport.PRIMARY, 1)}
        fields = {"epoch": 1, "request": approved}
        assert request("nuc", "/v1/review/submit", fields) == (403, {"error": "review-scope-required"})
        assert not queue_calls
        scope(1, approved)
        confirmed_review = nuc.submit_review(1, approved)
        assert confirmed_review["phase"] == "queued"
        assert confirmed_review["run"] == "fixture-run-1"
        scope(1, uncertain)
        assert nuc.submit_review(1, uncertain)["phase"] == "uncertain"
        assert nuc.submit_review(1, uncertain)["phase"] == "uncertain"
        assert len(queue_calls) == 2
        with pytest.raises(transport.ProjectScopeRejected):
            nuc.submit_project(1, coding)
        coding_scope(coding)
        with pytest.raises(transport.ProjectScopeRejected):
            nuc.submit_project(1, coding)
        worker_home = tmp_path / "worker"
        worker_home.mkdir(mode=0o700)
        plan_path = worker_home / "project-worker.json"
        operator_plan = {"request": {key: value for key, value in coding.items() if key != "lane"},
                         "checkout": str(checkout), "file": source.name,
                         "before": '"old"', "prefix": "", "suffix": "", "prompt": "Update delivery status"}
        worker["save"](plan_path, operator_plan)
        plan_sha = worker["digest"](plan_path.read_bytes())
        profile_path = worker_home / "broker.json"
        worker["save"](profile_path, {"hostname": "127.0.0.1", "port": broker.server_port, "bot_id": 234567,
                                      "cert": str(tmp_path / "nuc.pem"), "key": str(tmp_path / "nuc.key"), "ca": str(ca)})
        monkeypatch.setenv("FO_PROJECT_BROKER_PROFILE", str(profile_path))
        coding_scope(coding, plan_sha)
        with pytest.raises(transport.ProjectScopeRejected):
            worker["load_scope"](worker_home, coding)
        with pytest.raises(transport.ProjectScopeRejected):
            nuc.submit_project(1, coding, "e" * 64)
        assert nuc.submit_project(1, coding, plan_sha)["phase"] == "uncertain"
        assert nuc.project_worker_plan(1, coding) == plan_sha
        assert worker["load_scope"](worker_home, coding) == operator_plan
        worker["save"](plan_path, {**operator_plan, "prompt": "Changed operator plan"})
        with pytest.raises(ValueError, match="approved coding plan changed"):
            worker["load_scope"](worker_home, coding)
        worker["save"](plan_path, operator_plan)
        calls = []

        def model(prompt):
            calls.append(prompt)
            return "Delivery is unconfirmed"

        local_result = worker["run"](worker_home, coding, model)
        assert worker["run"](worker_home, coding, model) == local_result
        assert calls == [operator_plan["prompt"]]
        assert git(checkout, "show", local_result["commit"] + ":review.mjs") == b'const status = "Delivery is unconfirmed";\n'
        assert source.read_text() == 'const status = "old";\n'
        coding_scope(coding, "e" * 64)
        with pytest.raises(transport.ProjectScopeRejected):
            nuc.project_worker_plan(1, coding)
        with pytest.raises(transport.ProjectScopeRejected):
            nuc.submit_project(1, coding)
        coding_scope(candidate, "e" * 64)
        with pytest.raises(transport.ProjectBusy):
            nuc.submit_project(1, candidate)
        reopened = transport.ProjectQueue(state, project_profile, clock=lambda: now)
        with pytest.raises(transport.ProjectBusy):
            reopened.reserve(candidate, reopened.key(candidate), "e" * 64)
        assert len(queue_calls) == 3
        with pytest.raises(transport.LeaseRejected):
            mac.renew(1)
        assert request("nuc", "/v1/renew", {"epoch": 1, "node": "mac-mini"}) == (400, {"error": "invalid-request"})
        assert next_message("mac", 1) == (409, {"error": "lease-rejected"})
        assert next_message("nuc", 1)[1]["message"]["message_id"] == 11
        assert complete("nuc", 1, 11) == (409, {"error": "reply-receipt-required"})
        assert effect("mac", 1, 11) == (409, {"error": "lease-rejected"})
        assert effect("nuc", 1, 11) == (200, {"result": "confirmed"})
        assert complete("nuc", 1, 11) == (200, {"result": "confirmed"})
        assert next_message("nuc", 1)[1]["message"]["message_id"] == 12
        assert acknowledge("nuc", 1, 12) == (200, {"result": "confirmed"})
        with pytest.raises(transport.EffectUnconfirmed):
            nuc.request("/v1/effect", {"epoch": 1, "kind": "telegram-reply",
                                      "payload": {"message_id": 12, "phase": "final", "text": "Fixture response"}})
        assert request("nuc", "/v1/effect", {"epoch": 1, "kind": "hatchet-submit", "payload": {}}) == (
            403, {"error": "effect-not-enabled"})
        now = 89
        assert standby.step() == "parked"
        now = 90
        assert standby.step() == "active"
        assert primary.step() == "authority-lost"
        assert active_gateways == {(transport.STANDBY, 2)}
        with pytest.raises(transport.LeaseRejected):
            nuc.submit_review(2, uncertain)
        assert mac.review_receipt(2, approved)["run"] == "fixture-run-1"
        assert mac.review_receipt(2, uncertain)["phase"] == "uncertain"
        assert mac.poll_review(2, uncertain)["worker"] == "unresolved"
        assert not queue_reads
        status = mac.poll_review(2, approved)
        assert (status["worker"], status["head"], status["ci"]) == ("settled", approved["head"], "passing")
        assert len(queue_reads) == 2 and all("/workflow-runs/fixture-run-1" in url for url in queue_reads)
        result_head = "b" * 40
        assert mac.poll_review(2, approved)["worker"] == "invalid-result"
        assert request("mac", "/v1/review/submit", {"epoch": 2, "request": uncertain}) == (
            403, {"error": "review-scope-required"})
        scope(2, uncertain)
        assert mac.submit_review(2, uncertain)["phase"] == "uncertain"
        assert mac.project_receipt(2, coding)["phase"] == "uncertain"
        with pytest.raises(transport.ProjectScopeRejected):
            mac.retire_project(2, coding)
        with pytest.raises(transport.ProjectScopeRejected):
            mac.project_worker_plan(2, coding)
        assert mac.project_status(2) == {"enabled": True, "jobs": [{"request": coding, "state": "unresolved"}]}
        with pytest.raises(transport.ProjectScopeRejected):
            mac.submit_project(2, coding)
        with pytest.raises(transport.BrokerUnavailable):
            mac.reconcile_project(2, coding, "fixture-project-run")
        assert mac.project_receipt(2, coding)["phase"] == "uncertain"
        with pytest.raises(transport.ProjectScopeRejected):
            mac.retire_project(2, coding)
        with pytest.raises(transport.ProjectScopeRejected):
            mac.project_worker_plan(2, coding)
        assert mac.project_status(2) == {"enabled": True, "jobs": [{"request": coding, "state": "unresolved"}]}
        exact_project_readback = True
        reconciled = mac.reconcile_project(2, coding, "fixture-project-run")
        assert (reconciled["phase"], reconciled["run"]) == ("queued", "fixture-project-run")
        assert mac.reconcile_project(2, coding, "fixture-project-run")["reused"] is True
        assert mac.project_status(2) == {"enabled": True, "jobs": [{"request": coding, "state": "running"}]}
        with pytest.raises(transport.LeaseRejected):
            nuc.inspect_project(2, coding)
        with pytest.raises(transport.LeaseRejected):
            worker["load_scope"](worker_home, coding)
        observed = mac.inspect_project(2, coding)
        assert observed == {"request": coding, "state": "patch-ready", "commit": "b" * 40, "observed": 90}
        assert mac.project_status(2)["jobs"] == [observed]
        project_commit = "c" * 40
        now = 120
        polling = SimpleNamespace(done=False)
        polling.is_set = lambda: polling.done
        polling.wait = lambda _interval: setattr(polling, "done", True)
        project.refresh(polling)
        assert mac.project_status(2)["jobs"][0]["state"] == "invalid-artifact"
        project_commit = "b" * 40
        hatchet_available = False
        assert mac.inspect_project(2, coding)["state"] == "worker-evidence-unavailable"
        assert mac.project_status(2)["jobs"][0]["state"] == "worker-evidence-unavailable"
        hatchet_available = True
        assert mac.inspect_project(2, coding)["commit"] == "b" * 40
        with pytest.raises(transport.ProjectBusy):
            reopened.reserve(candidate, reopened.key(candidate), "e" * 64)
        with pytest.raises(transport.LeaseRejected):
            nuc.project_status(2)
        with pytest.raises(transport.LeaseRejected):
            nuc.reconcile_project(2, coding, "fixture-project-run")
        assert len(queue_calls) == 3
        assert all("/workflow-runs/trigger" in url for url, _ in queue_calls)
        assert effect("nuc", 2, 12) == (409, {"error": "lease-rejected"})
        assert mac.next_telegram_message(2)["message_id"] == 12
        assert mac.send_telegram_reply(2, 12, "ack") == "confirmed"
        assert mac.send_telegram_reply(2, 12, "final", "Fixture response") == "unconfirmed"
        assert mac.complete_telegram_message(2, 12) == "unconfirmed"
        assert next_message("mac", 2)[1]["message"]["message_id"] == 13
        local_token = "777777:" + "a" * 32
        with channel_module.Channel(mac, 2, 101, 777777, local_token) as local:
            relay = threading.Thread(target=local.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            relay.start()

            def telegram(method, body, token=local_token):
                client = http.client.HTTPConnection("127.0.0.1", local.server_port, timeout=4)
                try:
                    client.request("POST", f"/bot{token}/{method}", json.dumps(body),
                                   {"Content-Type": "application/json"})
                    response = client.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    client.close()

            try:
                assert telegram("getUpdates", {}, token="wrong")[0] == 401
                models = telegram("getUpdates", {"offset": 14})
                assert models[1]["result"][0]["message"]["message_id"] == 13
                assert telegram("getUpdates", {}) == (200, {"ok": True, "result": []})
                assert telegram("sendMessage", {"chat_id": 101, "text": "Unreferenced reply"})[0] == 400
                progress = {"chat_id": 101, "fo_message_id": 13}
                assert telegram("foProgress", {**progress, "fo_message_id": 12})[0] == 400
                expected = (200, {"ok": True, "result": {"typing": "confirmed", "ack": "confirmed"}})
                assert request("nuc", "/v1/telegram/progress", {"epoch": 2, "message_id": 13})[0] == 409
                assert telegram("foProgress", progress) == expected
                assert telegram("foProgress", progress) == expected
                assert telegram("sendChatAction", {"chat_id": 101, "action": "typing", "fo_message_id": 13})[0] == 200
                assert telegram("sendMessage", {"chat_id": 101, "text": "On it.", "fo_phase": "ack",
                                                "reply_parameters": {"message_id": 13}})[0] == 200
                assert telegram("sendMessage", {"chat_id": 999, "text": "Wrong owner",
                                                "reply_parameters": {"message_id": 13}})[0] == 400
                assert telegram("sendMessage", {"chat_id": 101, "text": "Fixture response",
                                                "reply_parameters": {"message_id": 13}})[0] == 200
            finally:
                local.shutdown()
                relay.join(timeout=5)

        assert typing == [101]
        assert next_message("mac", 2) == (200, {"message": None})
        poll.available.clear()
        assert next_message("mac", 2) == (503, {"error": "telegram-poll-unavailable"})
        assert sends == [(101, "Fixture response", 11), (101, "On it.", 12),
                         (101, "Fixture response", 12), (101, "On it.", 13),
                         (101, "Fixture response", 13)]
        poll.step()
        inbox.ingest([telegram_update(4, 14), telegram_update(5, 15)])
        assert mac.next_telegram_message(2)["message_id"] == 14
        with pytest.raises(transport.EffectUnconfirmed):
            mac.send_telegram_reply(2, 14, "final", "Fixture response")
        standby.park()
        assert not active_gateways
    finally:
        broker.shutdown()
        thread.join(timeout=5)

    assert not thread.is_alive()
    old_endpoint = mac
    old_state = state
    approved_source = lease.Grant(transport.STANDBY, 2)
    snapshot = tmp_path / "snapshot"
    transfer_id = "ab" * 16
    with lease.StandbyAuthority(old_state, export_id=transfer_id) as authority:
        lease.export_snapshot(authority, old_state, snapshot, approved_source, transport.PRIMARY, transfer_id)
    with pytest.raises(lease.LeaseRejected, match="sealed"):
        lease.StandbyAuthority(old_state)

    state = tmp_path / "destination"
    state.mkdir(mode=0o700)
    assert lease.import_snapshot(state, snapshot, approved_source, transport.PRIMARY, transfer_id)
    assert not (state / "project-scope.json").exists()

    telegram_module = importlib.import_module("standby_telegram")
    inbox = telegram_module.TelegramInbox(state, 101, 202)
    poll = telegram_module.TelegramPoll(inbox, lambda _offset: [telegram_update(4, 14), telegram_update(5, 15)])
    poll.step()  # Upstream replay does not recreate settled messages.
    review = transport.ReviewQueue(state, profile, clock=lambda: now)
    project = transport.ProjectQueue(state, project_profile, clock=lambda: now)
    ready = Future()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    broker = ready.result(timeout=5)
    try:
        with pytest.raises(transport.BrokerUnavailable):
            old_endpoint.claim()
        nuc, mac = client("nuc"), client("mac")
        assert nuc.readiness() == {"holder": None, "epoch": None, "gatewayRenewed": False,
                                   "telegramPolling": True, "bot": 202}
        assert nuc.claim() is None
        now += lease.LEASE_SECONDS
        assert mac.claim() is None
        epoch = nuc.claim()
        assert epoch == 3
        assert mac.readiness()["gatewayRenewed"] is False
        nuc.renew(3)
        assert mac.readiness()["gatewayRenewed"] is True

        verifier = Path(__file__).resolve().parents[2] / "first-officer/standby_launch.py"
        profile = tmp_path / "readiness-profile.json"
        profile.touch(mode=0o600)
        private_profile = {"hostname": "127.0.0.1", "port": broker.server_port, "bot_id": 202,
                           "cert": str(tmp_path / "mac.pem"), "key": str(tmp_path / "mac.key"),
                           "ca": str(ca)}
        profile.write_text(json.dumps(private_profile))

        def verify(expected_holder, expected_epoch, *options):
            return subprocess.run([sys.executable, "-I", "-B", str(verifier), "verify-broker", str(profile),
                                   "--holder", expected_holder, "--epoch", str(expected_epoch), "--timeout", "1",
                                   *options],
                                  capture_output=True, check=False, text=True, timeout=4)

        assert verify(lease.PRIMARY, 3).returncode == 0
        assert verify(lease.STANDBY, 3).returncode == 1
        assert verify(lease.PRIMARY, 2).returncode == 1
        pending = verify(lease.STANDBY, 4, "--previous-holder", lease.PRIMARY, "--previous-epoch", "3")
        assert pending.returncode == 1 and "timed out" in pending.stderr
        invalid = verify(lease.STANDBY, 5, "--previous-holder", lease.PRIMARY, "--previous-epoch", "3")
        assert invalid.returncode == 2
        poll.last_success -= 30
        assert verify(lease.PRIMARY, 3).returncode == 1
        profile.write_text(json.dumps({**private_profile, "bot_id": 203}))
        assert verify(lease.PRIMARY, 3).returncode == 1
        poll.step()
        with pytest.raises(transport.LeaseRejected):
            mac.renew(2)
        assert nuc.next_telegram_message(epoch)["message_id"] == 14
        assert nuc.send_telegram_reply(epoch, 14, "final", "Fixture response") == "unconfirmed"
        assert nuc.complete_telegram_message(epoch, 14) == "unconfirmed"
        assert nuc.next_telegram_message(epoch)["message_id"] == 15
        assert nuc.send_telegram_reply(epoch, 15, "final", "Fixture response") == "confirmed"
        assert nuc.complete_telegram_message(epoch, 15) == "confirmed"
        assert nuc.next_telegram_message(epoch) is None
        assert nuc.project_receipt(epoch, coding)["run"] == "fixture-project-run"
        assert nuc.reconcile_project(epoch, coding, "fixture-project-run")["reused"] is True
        assert nuc.review_receipt(epoch, approved)["run"] == "fixture-run-1"
        assert len(queue_calls) == 3
        assert [message_id for _, _, message_id in sends][-2:] == [14, 15]
        now += 31
        assert mac.readiness()["gatewayRenewed"] is False
    finally:
        broker.shutdown()
        thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.parametrize("mode", ["cold", "healthy", "uncertain"])
def test_broker_acknowledges_pending_owner_message_before_any_gateway_is_ready(tmp_path, monkeypatch, mode):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "first-officer"))
    transport = importlib.import_module("standby_broker")
    telegram = importlib.import_module("standby_telegram")
    certificates(tmp_path, ("broker", "nuc", "mac"))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    peers = {transport.client_fingerprint(tmp_path / (name + ".pem")): node
             for name, node in (("nuc", transport.PRIMARY), ("mac", transport.STANDBY))}
    inbox = telegram.TelegramInbox(state, 101, 202)
    fresh = telegram_update(1, 11)
    fresh["message"]["date"] = int(time.time())
    unrelated = telegram_update(2, 12, author=999)
    replay = telegram_update(3, 13)
    sends = []
    typing = []
    progress = threading.Barrier(2, timeout=1)

    def send(owner, text, message_id):
        assert inbox.pending(message_id)
        if mode != "healthy" and not sends:
            progress.wait()
        sends.append((owner, text, message_id))
        if mode == "uncertain":
            raise TimeoutError("The acknowledgment may have been delivered")
        return {"chat": {"id": owner}, "message_id": 411}

    def send_typing(owner):
        if mode != "healthy" and not typing:
            progress.wait()
        typing.append(owner)
        return True

    def broker(authority):
        poll = telegram.TelegramPoll(inbox, lambda _offset: [fresh, unrelated, replay])
        return transport.Broker(("127.0.0.1", 0), authority, cert=tmp_path / "broker.pem", key=tmp_path / "broker.key",
                                ca=tmp_path / "ca.pem", peers=peers,
                                effects={"telegram-reply": inbox.reply_adapter(send),
                                         "telegram-typing": inbox.typing_adapter(send_typing)}, inbox=inbox, poll=poll)

    now = 0
    with transport.StandbyAuthority(state, clock=lambda: now) as authority, broker(authority) as running:
        if mode == "healthy":
            primary = authority.claim(transport.PRIMARY)
            request = SimpleNamespace(server=running, path="/v1/telegram/next")
            transport.Request.dispatch(request, transport.PRIMARY, {"epoch": primary.epoch})
        worker = threading.Thread(target=running.poll.step)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert running.poll.available.is_set()
        assert inbox.next()["message_id"] == 11
        assert authority.claim(transport.STANDBY) is None
        primary = authority.claim(transport.PRIMARY)
        if mode == "healthy":
            assert sends == [] and typing == []
            operation, delivery = inbox.reply_adapter(send)({"message_id": 11, "phase": "ack"})
            assert authority.perform(primary, "telegram-reply", operation, delivery) == "confirmed"
            assert sends == [(101, "On it.", 11)]
            operation, delivery = inbox.typing_adapter(send_typing)({"message_id": 11})
            assert authority.perform(primary, "telegram-typing", operation, delivery) == "confirmed"
        else:
            assert sends == [(101, "Queued. I’ll reply when the gateway is ready.", 11)]
            assert typing == [101]
            assert authority.perform(primary, "telegram-typing", "message:11:typing",
                                     lambda: pytest.fail("The gateway must reuse the typing receipt")) == "confirmed"
            expected = "unconfirmed" if mode == "uncertain" else "confirmed"
            assert authority.effect_status(primary, "telegram-reply", "message:11:ack") == expected
            assert authority.perform(primary, "telegram-reply", "message:11:ack",
                                     lambda: pytest.fail("The gateway must reuse the queued acknowledgment")) == expected

    with transport.StandbyAuthority(state, clock=lambda: now) as authority, broker(authority) as running:
        running.poll.step()
        assert len(sends) == 1
        assert typing == [101]


def test_slow_valid_telegram_poll_still_commits_the_owner_update(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "first-officer"))
    telegram = importlib.import_module("standby_telegram")
    inbox = telegram.TelegramInbox(tmp_path, 101, 234567)
    api = telegram.BotAPI("234567:" + "x" * 32, 234567)

    def call(method, fields, *, timeout):
        assert method == "getUpdates" and fields["timeout"] == 4
        # Simulate a successful upstream response whose headers arrive seven seconds in.
        if timeout <= 7:
            raise TimeoutError("Fixture response arrived after the transport deadline")
        return [telegram_update(1, 11)]

    monkeypatch.setattr(api, "call", call)
    poll = telegram.TelegramPoll(inbox, api.get_updates)
    poll.step()
    assert poll.available.is_set() and poll.offset == 2
    assert inbox.next()["message_id"] == 11


def test_poll_commits_before_confirming_and_replay_never_resurrects_completed_messages(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "first-officer"))
    telegram = importlib.import_module("standby_telegram")
    inbox = telegram.TelegramInbox(tmp_path, 101, 202)
    offsets = []
    messages = [telegram_update(1, 11), telegram_update(2, 12, author=999)]

    def get_updates(offset):
        offsets.append(offset)
        if offset == 3:
            assert inbox.next()["message_id"] == 11
            return []
        return messages

    poll = telegram.TelegramPoll(inbox, get_updates)
    poll.step()
    poll.step()
    assert offsets == [None, 3]
    assert poll.offset is None

    inbox.complete(11, "confirmed")
    reopened = telegram.TelegramInbox(tmp_path, 101, 202)
    telegram.TelegramPoll(reopened, get_updates).step()
    assert offsets == [None, 3, None]
    assert reopened.next() is None
    with reopened.database() as db:
        assert db.execute("SELECT text, outcome FROM inbox").fetchall() == [(None, "confirmed")]
    with pytest.raises(ValueError, match="different owner or bot"):
        telegram.TelegramInbox(tmp_path, 101, 303)

    broken = telegram.TelegramPoll(reopened, lambda offset: [telegram_update(3, 13), {}])
    with pytest.raises(ValueError, match="update identity"):
        broken.step()
    assert broken.offset is None
    assert reopened.next() is None

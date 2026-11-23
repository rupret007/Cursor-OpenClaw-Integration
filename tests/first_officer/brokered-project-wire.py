"""Exercise brokered project admission with the private queue pilot."""
from concurrent.futures import Future
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import runpy
import shlex
import sys
from threading import Thread
import time


TESTS = Path(__file__).resolve().parent


def setup(stack, release, home, authority_path, job, coding, save):
    runtime = release / "round6/runtime/segment1/queue-runtime"
    launch = runpy.run_path(str(runtime / "standby_launch.py"))
    from standby_broker import Broker, Client, PRIMARY, STANDBY, StandbyAuthority, client_fingerprint
    from standby_queue import ProjectQueue
    from standby_telegram import TelegramInbox

    state = home / "broker-state"
    state.mkdir(mode=0o700)
    certs = home / "certs"
    runpy.run_path(str(TESTS / "standby_fixture.py"))["certificates"](certs)
    owner, bot_id = 123456, 234567
    inbox = TelegramInbox(state, owner, bot_id)
    projects = ProjectQueue(state, coding)
    grant = json.loads((authority_path / "project-grant.json").read_bytes())
    save(projects.scope, {"epoch": job["epoch"], "request": job, "expires": grant["expires"],
                          "worker": "local-qwen", "planSha256": grant["planSha256"]})
    effects = []
    ready = Future()

    def send_message(recipient, text, message_id):
        assert recipient == owner
        effects.append(("ack" if text == "On it." else "final", message_id, time.monotonic(), text))
        return {"chat": {"id": owner}, "message_id": message_id + 1000}

    def send_typing(recipient):
        assert recipient == owner
        effects.append(("typing", inbox.next()["message_id"], time.monotonic(), ""))
        return True

    def serve():
        try:
            peers = {client_fingerprint(certs / f"{name}.pem"): identity
                     for name, identity in (("nuc", PRIMARY), ("mac", STANDBY))}
            with StandbyAuthority(state) as authority, Broker(
                    ("127.0.0.1", 0), authority, cert=certs / "broker.pem", key=certs / "broker.key",
                    ca=certs / "ca.pem", peers=peers, inbox=inbox, project=projects,
                    effects={"telegram-reply": inbox.reply_adapter(send_message),
                             "telegram-typing": inbox.typing_adapter(send_typing)}) as server:
                ready.set_result(server)
                server.serve_forever(poll_interval=.05)
        except Exception as error:
            if not ready.done():
                ready.set_exception(error)
            else:
                raise

    thread = Thread(target=serve, daemon=True)
    thread.start()
    broker = ready.result(timeout=5)
    stack.callback(thread.join, 5)
    stack.callback(broker.shutdown)
    profile = home / "broker.json"
    save(profile, {"hostname": "127.0.0.1", "port": broker.server_port, "bot_id": bot_id,
                   "cert": str(certs / "nuc.pem"), "key": str(certs / "nuc.key"), "ca": str(certs / "ca.pem")})
    client = Client("127.0.0.1", broker.server_port, cert=certs / "nuc.pem", key=certs / "nuc.key", ca=certs / "ca.pem")
    assert client.claim() == job["epoch"]

    def exercise(node, cli, eventually, done):
        wire = runpy.run_path(str(TESTS / "project-telegram-wire.py"))
        gate = home / "gate"
        gate.mkdir()
        (gate / "fullgate.lock").touch()
        save(authority_path / "gate.json", {"directory": str(gate), "lock": "fullgate.lock", "hold": "HOLD"})
        save(authority_path / "capacity.json", {"commands": ["fixture-coding-capacity"], "limit": 1})
        gh = home / ".local/bin/gh"
        gh.parent.mkdir(parents=True)
        responses = {
            f"repos/{job['repository']}/issues/{job['issue']}": {
                "number": job["issue"], "state": "open",
                "repository_url": f"https://api.github.com/repos/{job['repository']}",
            },
            f"repos/{job['repository']}/git/ref/heads/main": {
                "ref": "refs/heads/main", "object": {"type": "commit", "sha": job["head"]},
            },
        }
        gh.write_text("#!/usr/bin/python3\nimport json, sys\nprint(json.dumps(" + repr(responses) + "[sys.argv[4]]))\n")
        gh.chmod(0o700)
        assert not Path("/usr/bin/gh").exists(), "The loopback fixture needs the private GitHub CLI override"
        executable = home / "openclaw"
        executable.write_text("#!/bin/sh\nexec " + shlex.join([str(node), str(cli)]) + ' "$@"\n')
        executable.chmod(0o700)
        local = home / "gateway-state"
        local.mkdir(mode=0o700)
        with ThreadingHTTPServer(("127.0.0.1", 0), wire["BotApi"]) as model:
            config = wire["configure"](model, home, release)
            template = json.loads(config.read_bytes())
            for key in ("apiRoot", "botToken"):
                del template["channels"]["telegram"][key]
            env = {"HOME": str(home), "PATH": f"{node.parent}:/usr/bin:/bin", "LANG": "C.UTF-8",
                   "FO_LOCAL_NODE": "nucbox", "OPENCLAW_STATE_DIR": str(local)}
            gateway = launch["BrokeredGateway"](client, job["epoch"], owner, bot_id, executable, template, env, local)
            try:
                eventually(gateway.ready, "brokered project gateway", timeout=22)

                def turn(number, text, expected):
                    started = time.monotonic()
                    inbox.ingest([{"update_id": number, "message": {
                        "message_id": number, "date": int(time.time()), "text": text,
                        "chat": {"id": owner, "type": "private"}, "from": {"id": owner, "is_bot": False},
                    }}])
                    eventually(lambda: inbox.next() is None, "brokered Telegram reply", timeout=30)
                    matched = {phase: (at - started, reply) for phase, mid, at, reply in effects if mid == number}
                    assert len([effect for effect in effects if effect[1] == number]) == len(matched) == 3
                    assert matched["typing"][0] < 2 and matched["ack"][0] < 2 and matched["final"][0] < 30
                    assert expected in matched["final"][1]
                    print(json.dumps({"brokered_case": number,
                                      **{phase: round(result[0], 3) for phase, result in matched.items()}}), flush=True)

                command = f"captain this: advance {job['repository']} #{job['issue']}"
                turn(101, command, "Hatchet accepted the coding job")
                receipt = client.project_receipt(job["epoch"], job)
                assert receipt["phase"] == "queued"
                eventually(lambda: done(receipt["run"]), "original brokered Hatchet run")
                observed = client.inspect_project(job["epoch"], job)
                assert observed["state"] == "patch-ready"
                turn(102, command, "already has a receipt")
                assert client.project_receipt(job["epoch"], job)["run"] == receipt["run"]
                turn(103, "handoff", "Candidate " + observed["commit"][:8])
                assert not (authority_path / "lease.json").exists()
                rows = [json.loads(line) for line in projects.ledger.read_text().splitlines()]
                assert [row["phase"] for row in rows] == ["pending", "queued"]
                return receipt
            finally:
                gateway.stop()

    return profile, exercise

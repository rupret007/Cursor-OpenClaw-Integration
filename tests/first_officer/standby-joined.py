"""Rehearse real packaged Nuc/Mac-role launchers with a loopback mTLS broker."""
from concurrent.futures import Future
from contextlib import ExitStack, contextmanager
from http.server import ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from threading import Thread
import time

from standby_fixture import certificates

TESTS = Path(__file__).resolve().parent


def until(condition, seconds, failure):
    deadline = time.monotonic() + seconds
    while True:
        result = condition()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(failure)
        time.sleep(0.05)


@contextmanager
def serving(server):
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(5)


@contextmanager
def run_host(scratch, identity, executable, runtime, config, profile, node):
    state = scratch / "gateway-state"
    state.mkdir(mode=0o700)
    environment = scratch / "environment.json"
    environment.write_text("{}")
    env = {"HOME": str(scratch), "PATH": f"{node.parent}:/usr/bin:/bin", "LANG": "C.UTF-8",
           "FO_LOCAL_NODE": identity, "OPENCLAW_STATE_DIR": str(state)}
    log = scratch / "supervisor.log"
    with log.open("w") as output:
        process = subprocess.Popen([sys.executable, "-I", "-B", str(runtime), str(executable), str(config),
                                    str(environment), str(profile)], env=env, stdout=output, stderr=output)

        def status(name):
            if process.poll() is not None:
                raise AssertionError(f"The isolated {identity} supervisor exited.")
            return f"Captain standby {name}" in log.read_text().splitlines()

        try:
            yield status
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    raise AssertionError(f"The isolated {identity} supervisor did not stop.")
    assert not list(state.glob(".broker-gateway-*"))


def main():
    baseline, node, cli = (Path(arg).resolve(strict=True) for arg in sys.argv[1:])
    if {row["ifname"] for row in json.loads(subprocess.check_output(
            ["/usr/sbin/ip", "-j", "link", "show"], timeout=4))} != {"lo"}:
        raise SystemExit("Use a loopback-only network namespace.")
    os.umask(0o077)
    wire = runpy.run_path(str(TESTS / "project-telegram-wire.py"))

    with TemporaryDirectory(prefix="fo-standby-joined-") as temp, ThreadingHTTPServer(
            ("127.0.0.1", 0), wire["BotApi"]) as model:
        root = Path(temp)
        if baseline.is_file() and baseline.suffix == ".tar":
            with tarfile.open(baseline, "r") as archive:
                archive.extractall(root, filter="data")
            baseline = root / baseline.stem
        releases = root / "releases"
        releases.mkdir()
        stage = runpy.run_path(str(TESTS.parents[1] / "first-officer/stage.py"))["stage_release"]
        release = releases / ("first-officer-" + stage(baseline, releases))
        runtime = release / "round6/runtime/segment1/queue-runtime"
        sys.path.insert(0, str(runtime))
        transport = importlib.import_module("standby_broker")
        telegram = importlib.import_module("standby_telegram")
        lease = importlib.import_module("standby")
        certs = root / "certs"
        certificates(certs)
        state = root / "broker-state"
        state.mkdir()
        inbox = telegram.TelegramInbox(state, 123456, 234567)
        timestamps = {}
        sends = []
        ready = Future()

        class MeasuredAuthority(lease.StandbyAuthority):
            def renew(self, grant):
                super().renew(grant)
                timestamps[grant.node] = time.monotonic()

        def send_message(owner, text, message_id):
            assert owner == 123456
            sends.append((message_id, text, time.monotonic()))
            return {"chat": {"id": owner}, "message_id": message_id + 1000}

        updates = []
        poll = telegram.TelegramPoll(inbox, lambda _offset: updates)
        poll.step()

        def serve_broker():
            try:
                peers = {transport.client_fingerprint(certs / f"{name}.pem"): identity
                         for name, identity in (("nuc", lease.PRIMARY), ("mac", lease.STANDBY))}
                with MeasuredAuthority(state) as authority, transport.Broker(
                        ("127.0.0.1", 0), authority, cert=certs / "broker.pem", key=certs / "broker.key",
                        ca=certs / "ca.pem", peers=peers, inbox=inbox, poll=poll,
                        effects={"telegram-reply": inbox.reply_adapter(send_message),
                                 "telegram-typing": inbox.typing_adapter(lambda _owner: True)}) as broker:
                    ready.set_result(broker)
                    broker.serve_forever(poll_interval=0.1)
            except Exception as error:
                if not ready.done():
                    ready.set_exception(error)
                else:
                    raise

        outer = Thread(target=serve_broker, daemon=True)
        outer.start()
        broker = ready.result(timeout=5)
        executable = root / "openclaw"
        executable.write_text("#!/bin/sh\nexec " + shlex.join([str(node), str(cli)]) + ' "$@"\n')
        executable.chmod(0o700)

        def host(name, identity):
            directory = root / name
            directory.mkdir()
            config = wire["configure"](model, directory, release)
            template = json.loads(config.read_bytes())
            for key in ("apiRoot", "botToken"):
                del template["channels"]["telegram"][key]
            wire["write"](config, template)
            profile = directory / "broker.json"
            wire["write"](profile, {"hostname": "127.0.0.1", "port": broker.server_port, "bot_id": 234567,
                                    "cert": str(certs / f"{name}.pem"), "key": str(certs / f"{name}.key"),
                                    "ca": str(certs / "ca.pem")})
            return run_host(directory, identity, executable, runtime / "standby_launch.py", config, profile, node)

        def enqueue(number):
            updates[:] = [{"update_id": number, "message": {
                "message_id": number, "date": int(time.time()), "text": "captain status",
                "chat": {"type": "private", "id": 123456}, "from": {"id": 123456, "is_bot": False},
            }}]
            poll.step()

        def reply(number, epoch, deadline, acknowledgment="On it."):
            def settled():
                pair = [row for row in sends if row[0] == number]
                return pair if len(pair) >= 2 else None

            pair = until(settled, deadline, "A brokered Captain reply did not settle.")
            assert len(pair) == 2 and pair[0][1] == acknowledgment and f"epoch {epoch}" in pair[1][1]
            return pair[-1][2]

        try:
            with serving(model), host("mac", lease.STANDBY) as mac_status, ExitStack() as primary:
                until(lambda: mac_status("parked"), 10, "The standby host did not park.")
                nuc_status = primary.enter_context(host("nuc", lease.PRIMARY))
                until(lambda: nuc_status("active"), 30, "The primary did not become ready.")
                until(lambda: broker.last_telegram_pull is not None, 5, "Primary did not poll the broker.")
                enqueue(10)
                reply(10, 1, 10)
                assert lease.STANDBY not in timestamps
                stopped = time.monotonic()
                primary.close()
                last_renewal = timestamps[lease.PRIMARY]
                until(lambda: time.monotonic() - broker.last_telegram_pull >= 2, 5,
                      "Primary polling did not stop.")
                received = time.monotonic()
                enqueue(11)
                queued = "Queued. I’ll reply when the gateway is ready."
                assert sends[-1][:2] == (11, queued) and lease.STANDBY not in timestamps
                acknowledgment_seconds = sends[-1][2] - received
                assert acknowledgment_seconds < 2
                print("Nuc-role process stopped. Broker acknowledged the queued message.", flush=True)
                finished = reply(11, 2, 115, queued)
                mac_ready = until(lambda: timestamps.get(lease.STANDBY), 5, "Standby never renewed its own lease.")
                assert mac_ready - last_renewal >= lease.LEASE_SECONDS
                assert finished - stopped < 120
                old = transport.Client("127.0.0.1", broker.server_port, cert=certs / "nuc.pem",
                                       key=certs / "nuc.key", ca=certs / "ca.pem")
                assert old.claim() is None
                try:
                    old.renew(2)
                except lease.LeaseRejected:
                    pass
                else:
                    raise AssertionError("The previous host renewed the new epoch.")
                until(lambda: inbox.next() is None, 5, "The shared inbox did not settle.")
                assert len(sends) == 4
                print(json.dumps({"queued_ack_seconds": round(acknowledgment_seconds, 3),
                                  "takeover_from_last_renewal": round(mac_ready - last_renewal, 2),
                                  "reply_after_nuc_stop": round(finished - stopped, 2),
                                  "final_replies": 2, "duplicate_replies": 0}))
        finally:
            broker.shutdown()
            outer.join(5)


if __name__ == "__main__":
    main()

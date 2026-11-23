"""Run the packaged OpenClaw gateway through the standby launcher in isolation."""
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time

TESTS = Path(__file__).resolve().parent


class FixtureBroker:
    def __init__(self):
        self.polling = Event()
        self.message = None
        self.effects = []
        self.completed = Event()

    def next_telegram_message(self, epoch):
        assert epoch == 4
        self.polling.set()
        return self.message

    def project_status(self, epoch):
        assert epoch == 4
        return {"enabled": True, "jobs": [{
            "request": {"repository": "example/integration", "issue": 40, "head": "a" * 40, "epoch": 3},
            "state": "unresolved",
        }]}

    def telegram_typing(self, epoch, message_id):
        assert epoch == 4 and self.message["message_id"] == message_id
        self.effects.append(("typing", time.monotonic(), None))
        return "confirmed"

    def send_telegram_reply(self, epoch, message_id, phase, text):
        assert epoch == 4 and self.message["message_id"] == message_id
        self.effects.append((phase, time.monotonic(), text))
        return "confirmed"

    def complete_telegram_message(self, epoch, message_id):
        assert epoch == 4 and self.message["message_id"] == message_id
        self.message = None
        self.completed.set()
        return "confirmed"


def verify_turn(broker, message_id, text, expected, final_limit):
    broker.completed.clear()
    broker.effects.clear()
    started = time.monotonic()
    broker.message = {"message_id": message_id, "date": int(time.time()), "text": text}
    if not broker.completed.wait(final_limit):
        phases = [phase for phase, _, _ in broker.effects]
        raise AssertionError(f"Native brokered reply did not settle. Observed phases {phases}.")
    observed = {name: (moment - started, reply) for name, moment, reply in broker.effects}
    assert sorted(name for name, _, _ in broker.effects) == ["ack", "final", "typing"]
    assert expected.casefold() in observed["final"][1].casefold()
    assert observed["ack"][0] < 2 and observed["typing"][0] < 2
    print(json.dumps({"case": message_id, **{name: round(timing, 2)
                      for name, (timing, _) in observed.items()}}))


def main():
    baseline, node, cli = (Path(arg).resolve(strict=True) for arg in sys.argv[1:])
    links = json.loads(subprocess.check_output(["/usr/sbin/ip", "-j", "link", "show"], timeout=4))
    if {row["ifname"] for row in links} != {"lo"}:
        raise SystemExit("Use a loopback-only network namespace.")
    os.umask(0o077)
    wire_test = runpy.run_path(str(TESTS / "project-telegram-wire.py"))
    broker = FixtureBroker()

    with TemporaryDirectory(prefix="fo-standby-native-") as directory, ThreadingHTTPServer(
            ("127.0.0.1", 0), wire_test["BotApi"]) as model:
        root = Path(directory)
        if baseline.is_file() and baseline.suffix == ".tar":
            with tarfile.open(baseline, "r") as archive:
                archive.extractall(root, filter="data")
            baseline = root / baseline.stem
        staging = root / "staged"
        staging.mkdir(mode=0o700)
        stage = runpy.run_path(str(TESTS.parents[1] / "first-officer/stage.py"))["stage_release"]
        release = staging / ("first-officer-" + stage(baseline, staging))
        launch = runpy.run_path(str(release / "round6/runtime/segment1/queue-runtime/standby_launch.py"))
        authority = root / ".local/state/openclaw-captain"
        (authority / "reviews").mkdir(parents=True, mode=0o700)
        gate = root / "gate"
        gate.mkdir()
        (gate / "fullgate.lock").touch()
        wire_test["write"](authority / "gate.json", {"directory": str(gate), "lock": "fullgate.lock", "hold": "HOLD"})
        config = wire_test["configure"](model, root, release)
        settings = json.loads(config.read_bytes())
        for key in ("apiRoot", "botToken"):
            del settings["channels"]["telegram"][key]
        wire_test["write"](config, settings)
        template, owner = launch["template_from"](config)
        executable = root / "openclaw"
        executable.write_text("#!/bin/sh\nexec " + shlex.join([str(node), str(cli)]) + ' "$@"\n')
        executable.chmod(0o700)
        state = root / "gateway-state"
        state.mkdir(mode=0o700)
        environment = {"HOME": str(root), "PATH": f"{node.parent}:/usr/bin:/bin", "LANG": "C.UTF-8",
                       "FO_LOCAL_NODE": "nucbox", "OPENCLAW_STATE_DIR": str(state)}
        thread = Thread(target=model.serve_forever, daemon=True)
        thread.start()
        startup = time.monotonic()
        gateway = launch["BrokeredGateway"](broker, 4, owner, 234567, executable, template, environment, state)
        try:
            assert broker.polling.wait(20) and not gateway.exited(), "The packaged child did not reach the loopback bridge"
            while not gateway.ready():
                assert not gateway.exited() and time.monotonic() - startup < 20, "The packaged child did not become ready"
                time.sleep(0.05)
            print(json.dumps({"gateway_ready_seconds": round(time.monotonic() - startup, 2)}))
            for message_id, text, expected, final_limit in (
                    (10, "captain status", "epoch 4", 30),
                    (11, "gate status", "gate", 30),
                    (12, "what is a branch?", "line of development", 15),
                    (13, "captain grant integration-ci epoch 4", "dispatch need", 30),
                    (14, "what needs me?", "coding submission is unresolved", 30),
                    (15, "handoff", "coding submission is unresolved", 30),
            ):
                verify_turn(broker, message_id, text, expected, final_limit)
            assert any(phase == "final" and "Brokered handoff checked at" in text
                       for phase, _, text in broker.effects)
            assert not (authority / "lease.json").exists()
        finally:
            gateway.stop()
            model.shutdown()
            thread.join(5)
        assert not list(state.glob(".broker-gateway-*"))


if __name__ == "__main__":
    main()

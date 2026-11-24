import importlib
from pathlib import Path
import os
import signal
import socket
import subprocess
import sys
import time

import pytest


@pytest.fixture
def host(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "first-officer"))
    return importlib.import_module("standby_host")


class Child:
    def __init__(self, ready):
        self.available = ready
        self.stopped = False

    def ready(self):
        return self.available

    def exited(self):
        return self.stopped

    def stop(self):
        self.stopped = True


def test_partition_parks_nuc_and_mac_starts_after_lease_expires(host, tmp_path):
    standby = importlib.import_module("standby")
    now = 0
    started = []

    def launch(node):
        def start(epoch):
            child = Child(True)
            started.append((node, epoch, child))
            return child

        return start

    class Peer:
        connected = True

        def __init__(self, node):
            self.node = node

        def claim(self):
            if not self.connected:
                raise host.BrokerUnavailable("Fixture partition")
            grant = authority.claim(self.node)
            return grant.epoch if grant is not None else None

        def renew(self, epoch):
            if not self.connected:
                raise host.BrokerUnavailable("Fixture partition")
            authority.renew(standby.Grant(self.node, epoch))

    with standby.StandbyAuthority(tmp_path, clock=lambda: now) as authority:
        primary = Peer(standby.PRIMARY)
        nuc = host.Supervisor(primary, launch(standby.PRIMARY), clock=lambda: now)
        mac = host.Supervisor(Peer(standby.STANDBY), launch(standby.STANDBY), clock=lambda: now)
        assert mac.step() == "parked"
        assert nuc.step() == "active"
        assert started[0][:2] == (standby.PRIMARY, 1)
        primary.connected = False
        now = 15
        assert nuc.step() == "authority-lost"
        assert started[0][2].stopped
        now = 89
        assert mac.step() == "parked"
        now = 90
        assert mac.step() == "active"
        assert started[1][:2] == (standby.STANDBY, 2)
        primary.connected = True
        assert nuc.step() == "parked"
        assert len(started) == 2
        mac.park()
        assert started[1][2].stopped


@pytest.mark.parametrize("startup_seconds", [20, 60])
def test_unready_gateway_never_renews_and_stops_after_bounded_startup(host, startup_seconds):
    class Peer:
        def claim(self):
            return 1

        def renew(self, _epoch):
            pytest.fail("Unready gateway must not renew a lease")

    now = 0
    child = Child(False)
    supervisor = host.Supervisor(Peer(), lambda _epoch: child, clock=lambda: now, startup_seconds=startup_seconds)
    assert supervisor.step() == "starting"
    now = startup_seconds - 1
    assert supervisor.step() == "starting"
    now = startup_seconds
    assert supervisor.step() == "gateway-unready"
    assert child.stopped


@pytest.fixture
def gateway_fixture(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    executable = tmp_path / "openclaw"
    executable.write_text(f"""#!{sys.executable}
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path

Path({str(tmp_path / "gateway.pid")!r}).write_text(str(os.getpid()))

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        assert self.path == "/readyz"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{{"ready":true}}')

HTTPServer(("127.0.0.1", {port}), Handler).serve_forever()
""")
    executable.chmod(0o700)
    return executable, port


def test_gateway_process_checks_readiness_and_stops_the_real_child(host, gateway_fixture):
    executable, port = gateway_fixture
    gateway = host.GatewayProcess(executable, {}, port)
    try:
        deadline = time.monotonic() + 4
        while not gateway.ready():
            assert not gateway.exited()
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        gateway.stop()
    assert gateway.exited()
    assert not gateway.ready()


def test_gateway_never_uses_an_existing_listeners_readiness(host, gateway_fixture):
    executable, _ = gateway_fixture
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(OSError, match="already in use"):
            host.GatewayProcess(executable, {}, listener.getsockname()[1])


def test_gateway_exits_after_supervisor_is_killed_and_the_port_is_reusable(host, gateway_fixture, tmp_path):
    executable, port = gateway_fixture
    launch = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from standby_host import GatewayProcess
live = GatewayProcess(Path(sys.argv[2]), {}, int(sys.argv[3]))
while True:
    time.sleep(1)
"""
    directory = Path(host.__file__).parent
    supervisor = subprocess.Popen([sys.executable, "-c", launch, str(directory), str(executable), str(port)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def listening():
        with socket.socket() as probe:
            probe.settimeout(.1)
            return probe.connect_ex(("127.0.0.1", port)) == 0

    try:
        deadline = time.monotonic() + 4
        while not listening():
            assert supervisor.poll() is None and time.monotonic() < deadline
            time.sleep(.02)
        supervisor.kill()
        supervisor.wait(timeout=4)
        deadline = time.monotonic() + 4
        while listening():
            assert time.monotonic() < deadline
            time.sleep(.02)
        restarted = host.GatewayProcess(executable, {}, port)
        try:
            deadline = time.monotonic() + 4
            while not restarted.ready():
                assert not restarted.exited() and time.monotonic() < deadline
                time.sleep(.02)
        finally:
            restarted.stop()
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=4)
        if listening():
            os.kill(int((tmp_path / "gateway.pid").read_text()), signal.SIGTERM)

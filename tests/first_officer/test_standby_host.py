import importlib
from pathlib import Path
import socket
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


def test_unready_gateway_never_renews_and_stops_after_bounded_startup(host):
    class Peer:
        def claim(self):
            return 1

        def renew(self, _epoch):
            pytest.fail("Unready gateway must not renew a lease")

    now = 0
    child = Child(False)
    supervisor = host.Supervisor(Peer(), lambda _epoch: child, clock=lambda: now)
    assert supervisor.step() == "starting"
    now = 19
    assert supervisor.step() == "starting"
    now = 20
    assert supervisor.step() == "gateway-unready"
    assert child.stopped


def test_gateway_process_checks_readiness_and_stops_the_real_child(host, tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    executable = tmp_path / "openclaw"
    executable.write_text(f"""#!{sys.executable}
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        assert self.path == "/readyz"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{{"ready":true}}')

HTTPServer(("127.0.0.1", {port}), Handler).serve_forever()
""")
    executable.chmod(0o700)
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

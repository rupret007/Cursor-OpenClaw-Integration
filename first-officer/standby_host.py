"""Host lifecycle for Captain's brokered standby mode."""

import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

from standby import HEARTBEAT_SECONDS, LEASE_SECONDS, LeaseRejected
from standby_broker import BrokerUnavailable


STARTUP_SECONDS = 20
PRIMARY_STARTUP_SECONDS = 60
POLL_SECONDS = 1
RETRY_SECONDS = 3
STOP_SECONDS = 4


class GatewayProcess:
    def __init__(self, executable, environment, port):
        if not executable.is_absolute() or executable.name != "openclaw" or type(port) is not int or not 0 < port < 65536:
            raise ValueError("Expected a pinned OpenClaw gateway and a local port")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                raise OSError("The configured gateway port is already in use")
        except ConnectionRefusedError:
            pass
        self.port = port
        guard = Path(__file__).with_name("standby_child.py")
        monitor, self.lifeline = os.pipe()
        try:
            self.child = subprocess.Popen(
                [sys.executable, "-I", "-B", str(guard), str(monitor), str(executable)],
                env={**environment, "OPENCLAW_NO_RESPAWN": "1"}, pass_fds=(monitor,),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except BaseException:
            os.close(self.lifeline)
            raise
        finally:
            os.close(monitor)

    def exited(self):
        return self.child.poll() is not None

    def ready(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
        try:
            connection.request("GET", "/readyz")
            response = connection.getresponse()
            raw = response.read(8193)
            if response.status != 200 or len(raw) > 8192:
                return False
            payload = json.loads(raw)
            return isinstance(payload, dict) and payload.get("ready") is True and not self.exited()
        except (OSError, http.client.HTTPException, ValueError, RecursionError):
            return False
        finally:
            connection.close()

    def stop(self):
        if self.lifeline is not None:
            os.close(self.lifeline)
            self.lifeline = None
        try:
            os.killpg(self.child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.child.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        # The guard owns the gateway group. This bounds shutdown if the guard wedges.
        try:
            os.killpg(self.child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.child.wait(timeout=STOP_SECONDS)


class Supervisor:
    def __init__(self, client, start_gateway, *, clock=time.monotonic, startup_seconds=STARTUP_SECONDS):
        if type(startup_seconds) is not int or not 0 < startup_seconds < LEASE_SECONDS:
            raise ValueError("Gateway startup must fit inside the unrenewed lease")
        self.startup_seconds = startup_seconds
        self.client = client
        self.start_gateway = start_gateway
        self.clock = clock
        self.epoch = None
        self.gateway = None
        self.startup_until = 0
        self.renew_at = None
        self.retry_at = 0

    def park(self):
        if self.gateway is not None:
            self.gateway.stop()
        self.gateway = None
        self.epoch = None
        self.renew_at = None
        self.retry_at = self.clock() + RETRY_SECONDS

    def step(self):
        now = self.clock()
        if self.gateway is None:
            if now < self.retry_at:
                return "parked"
            try:
                epoch = self.client.claim()
                if epoch is None:
                    return "parked"
                self.gateway = self.start_gateway(epoch)
            except OSError:
                self.park()
                return "unavailable"
            self.epoch = epoch
            self.startup_until = self.clock() + self.startup_seconds

        if self.gateway.exited():
            self.park()
            return "gateway-exited"

        if self.renew_at is not None and now < self.renew_at:
            return "active"

        if not self.gateway.ready():
            if self.renew_at is None and self.clock() < self.startup_until:
                return "starting"
            self.park()
            return "gateway-unready"

        renewal_started = self.clock()
        if self.renew_at is None and renewal_started >= self.startup_until:
            self.park()
            return "gateway-unready"
        try:
            self.client.renew(self.epoch)
        except (LeaseRejected, BrokerUnavailable):
            self.park()
            return "authority-lost"
        self.renew_at = renewal_started + HEARTBEAT_SECONDS
        return "active"

    def run(self, stopped, update):
        previous = None
        try:
            while not stopped.is_set():
                status = self.step()
                if status != previous:
                    update(status)
                    previous = status
                stopped.wait(POLL_SECONDS)
        finally:
            self.park()

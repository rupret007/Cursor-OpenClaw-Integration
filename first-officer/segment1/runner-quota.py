"""Read the existing lane runner's subscription quota without starting a turn."""

import json
import os
from pathlib import Path
import runpy
from selectors import DefaultSelector, EVENT_READ
import subprocess
import tempfile
import time


def quota_windows(result, account, now):
    observed_account = result.get("accountId")
    if observed_account not in (None, account):
        raise ValueError("Lane quota account changed")
    snapshot = (result.get("rateLimitsByLimitId") or {}).get("codex") or result["rateLimits"]
    limits = {}
    for label, key, duration in (("session", "primary", 300), ("weekly", "secondary", 10080)):
        window = snapshot[key]
        used, reset = window["usedPercent"], window["resetsAt"]
        if (type(used) not in (int, float) or not 0 <= used <= 100 or type(reset) is not int
                or reset <= now or window["windowDurationMins"] != duration):
            raise ValueError("Incomplete lane quota")
        limits[label] = {"used": used, "reset": reset}
    credits = snapshot.get("credits") or {}
    limits["allowed"] = (result.get("ordinaryUsageAllowed") is not False and
                         not snapshot.get("rateLimitReachedType") and
                         not credits.get("hasCredits") and not credits.get("unlimited") and
                         all(limits[key]["used"] < 90 for key in ("session", "weekly")))
    return limits


def quota_rpc(proc, timeout=5):
    deadline = time.monotonic() + timeout
    pending = b""

    def send(message):
        proc.stdin.write((json.dumps(message) + "\n").encode())
        proc.stdin.flush()

    with DefaultSelector() as reader:
        reader.register(proc.stdout, EVENT_READ)

        def receive(request_id):
            nonlocal pending
            received = 0
            while time.monotonic() < deadline:
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    row = json.loads(line)
                    if row.get("id") == request_id:
                        return row["result"]
                if not reader.select(max(0, deadline - time.monotonic())):
                    break
                block = os.read(proc.stdout.fileno(), 4096)
                received += len(block)
                if not block or received > 128 * 1024:
                    break
                pending += block
            raise ValueError("Lane quota RPC did not return")

        send({"id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "openclaw_captain", "version": "1.0"}}})
        receive(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        return receive(2)


def read_runner_quota(runner):
    hooks = runpy.run_path(str(runner))
    credentials = hooks["codex_credential"](hooks["JEFF_AUTH"])
    with tempfile.TemporaryDirectory(prefix="fo-runner-quota-") as directory:
        local = Path(directory)
        cli = local / "cli"
        hooks["stage_codex_credential"](cli, credentials)
        env = {"HOME": str(local), "CODEX_HOME": str(cli), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        with subprocess.Popen(
            ["/usr/bin/nice", "-n", "10", "/usr/bin/codex", "app-server", "--listen", "stdio://",
             "-c", 'cli_auth_credentials_store="file"'],
            cwd=local, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
        ) as proc:
            try:
                result = quota_rpc(proc)
                return quota_windows(result, credentials["tokens"]["account_id"], time.time())
            finally:
                proc.kill()

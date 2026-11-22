"""Read the existing lane runner's subscription quota without starting a turn."""

import json
import os
from pathlib import Path
import runpy
from selectors import DefaultSelector, EVENT_READ
import subprocess
import tempfile
import time


def quota_snapshot(result):
    return (result.get("rateLimitsByLimitId") or {}).get("codex") or result["rateLimits"]


def quota_window(window, duration, now):
    if not isinstance(window, dict):
        raise ValueError("Incomplete lane quota")
    used, reset = window.get("usedPercent"), window.get("resetsAt")
    if (type(used) not in (int, float) or not 0 <= used <= 100 or type(reset) is not int
            or reset <= now or window.get("windowDurationMins") != duration):
        raise ValueError("Incomplete lane quota")
    return {"used": used, "reset": reset}


def quota_windows(result, account, now):
    observed_account = result.get("accountId")
    if observed_account not in (None, account):
        raise ValueError("Lane quota account changed")
    snapshot = quota_snapshot(result)
    limits = {
        "session": quota_window(snapshot.get("primary"), 300, now),
        "weekly": quota_window(snapshot.get("secondary"), 10080, now),
    }
    credits = snapshot.get("credits") or {}
    limits["allowed"] = (result.get("ordinaryUsageAllowed") is not False and
                         not snapshot.get("rateLimitReachedType") and
                         not credits.get("hasCredits") and not credits.get("unlimited") and
                         all(limits[key]["used"] < 90 for key in ("session", "weekly")))
    return limits


def local_quota_windows(result, now):
    # Standalone accounts can expose a single weekly window in the primary slot.
    snapshot = quota_snapshot(result)
    limits = {}
    for key in ("primary", "secondary"):
        window = snapshot.get(key)
        if window is None:
            continue
        if not isinstance(window, dict):
            raise ValueError("Incomplete local quota")
        duration = window.get("windowDurationMins")
        if type(duration) is not int:
            raise ValueError("Incomplete local quota")
        label = {300: "session", 10080: "weekly"}.get(duration)
        if label is None:
            continue
        if label in limits:
            raise ValueError("Ambiguous local quota windows")
        limits[label] = quota_window(window, duration, now)
    if not limits:
        raise ValueError("The local quota windows are unavailable")
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


def read_local_quota(binary):
    """Query the standalone host's signed-in local CLI without copying credentials."""
    path = Path(binary)
    if not path.is_absolute() or path.name != "codex" or not os.access(path, os.X_OK) or not path.is_file():
        raise ValueError("The approved standalone Codex CLI is unavailable")
    with subprocess.Popen(
        ["/usr/bin/nice", "-n", "10", str(path), "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as proc:
        try:
            result = quota_rpc(proc)
            now = time.time()
            return {**local_quota_windows(result, now), "observed": int(now)}
        finally:
            proc.kill()


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

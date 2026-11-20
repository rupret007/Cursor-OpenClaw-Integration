#!/usr/bin/env python3
"""Prepare, activate or roll back the reviewed First Officer release as operator."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from urllib.request import ProxyHandler, build_opener

from stage import BASELINE, ROLLBACK_SLOT, verify_inventory


STORE = Path("/opt/openclaw-fo")
SERVICE = "openclaw-gateway.service"
PLUGIN = "first-officer-conductor"
FAST_MODEL = "litellm/minimax-m3.1-flash"
PIN_PREFIX = f"ExecStartPre=/usr/bin/python3 -I -B {STORE / 'verify-install.py'} %h/.openclaw/openclaw.json "
DIGEST = re.compile(r"[0-9a-f]{64}")


def pin_line(digest):
    return PIN_PREFIX + digest + " " + ROLLBACK_SLOT


def systemctl(*args, capture=False):
    result = subprocess.run(
        ["systemctl", "--user", *args],
        check=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=45,
    )
    return result.stdout


def owner_file(path, max_size, private=False):
    info = path.lstat()
    permitted = 0o077 if private else 0o022
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & permitted:
        raise ValueError("An expected operator file has unsafe permissions")
    if info.st_size > max_size:
        raise ValueError("An expected operator file exceeded the size limit")
    return path.read_bytes()


def verify_config(path, release):
    for args, env, label in (
        (["/usr/bin/python3", "-I", "-B", str(STORE / "verify-install.py"), str(path), release, ROLLBACK_SLOT],
         None, "The installed release verifier rejected the config and pin"),
        (["openclaw", "config", "validate"], {**os.environ, "OPENCLAW_CONFIG_PATH": str(path)},
         "OpenClaw rejected the staged configuration"),
    ):
        try:
            subprocess.run(args, env=env, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
        except (OSError, subprocess.SubprocessError):
            raise ValueError(label) from None


def candidate_config(original, release):
    cfg = json.loads(original)
    paths = cfg["plugins"]["load"]["paths"]
    base = STORE / f"first-officer-{BASELINE}"
    expected = [str(base / "first-officer-exec-guard.mjs"),
                str(base / "round6/runtime/first-officer-conductor.mjs")]
    if paths != expected:
        raise ValueError("The active plugin paths are not the expected Round 13 pair")
    entry = cfg["plugins"]["entries"][PLUGIN]
    if entry.get("llm") or entry.get("enabled") is not True:
        raise ValueError("The conductor has a custom model policy or is disabled")
    provider = cfg["models"]["providers"]["litellm"]
    if not any(row.get("id") == FAST_MODEL.split("/")[1] for row in provider.get("models", [])):
        raise ValueError("The existing fast model is unavailable in the gateway config")
    cfg["plugins"]["load"]["paths"] = [path.replace(BASELINE, release, 1) for path in expected]
    entry["llm"] = {"allowModelOverride": True, "allowedModels": [FAST_MODEL], "allowedCompletionModels": [FAST_MODEL]}
    return (json.dumps(cfg, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def prepare(release, backup, config):
    if not DIGEST.fullmatch(release) or release in (BASELINE, ROLLBACK_SLOT):
        raise ValueError("Expected a distinct, full candidate digest")
    root = STORE / f"first-officer-{release}"
    verify_inventory(root, release)
    dropins = shlex.split(systemctl("show", "-p", "DropInPaths", "--value", SERVICE, capture=True))
    pins = []
    for entry in dropins:
        path = Path(entry)
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_size <= 65_536:
            contents = owner_file(path, 65_536)
            if pin_line(BASELINE) in contents.decode("utf-8").splitlines():
                pins.append((path, contents))
    if len(pins) != 1:
        raise ValueError("Could not find the existing Round 13 service pin")
    path, old_pin = pins[0]
    new_pin = old_pin.replace(pin_line(BASELINE).encode(), pin_line(release).encode(), 1)
    original = owner_file(config, 512 * 1024, private=True)
    updated = candidate_config(original, release)
    backup.mkdir(mode=0o700)
    files = {
        "original-openclaw.json": original, "candidate-openclaw.json": updated,
        "original-pin.conf": old_pin, "candidate-pin.conf": new_pin,
    }
    for name, content in files.items():
        (backup / name).write_bytes(content)
    verify_config(backup / "original-openclaw.json", BASELINE)
    verify_config(backup / "candidate-openclaw.json", release)
    (backup / "plan.json").write_text(json.dumps({"release": release, "pin": str(path)}) + "\n", encoding="utf-8")


def replace_file(path, payload):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".fo-segment1-", delete=False) as handle:
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(handle.name, path)
        finally:
            Path(handle.name).unlink(missing_ok=True)


def wait_ready(config):
    gateway = json.loads(config)["gateway"]
    port = gateway["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("The OpenClaw readiness port is unavailable")
    scheme = "https" if gateway.get("tls", {}).get("enabled") is True else "http"
    reader = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            systemctl("is-active", "--quiet", SERVICE)
            with reader.open(f"{scheme}://localhost:{port}/readyz", timeout=2) as response:
                if response.status == 200 and json.loads(response.read(16_384)).get("ready") is True:
                    return
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(2)
    raise ValueError("OpenClaw did not reach readiness within 120 seconds")


def start(config):
    systemctl("daemon-reload")
    systemctl("start", SERVICE)
    wait_ready(config)


def switch(backup, config, restore):
    info = backup.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise ValueError("The installation backup must be a private operator directory")
    plan = json.loads(owner_file(backup / "plan.json", 4096, private=True))
    release = plan["release"]
    pin = Path(plan["pin"])
    if not DIGEST.fullmatch(release) or str(pin) not in shlex.split(
        systemctl("show", "-p", "DropInPaths", "--value", SERVICE, capture=True)
    ):
        raise ValueError("The current service or release does not match the saved installation plan")
    original = (
        owner_file(backup / "original-openclaw.json", 512 * 1024, private=True),
        owner_file(backup / "original-pin.conf", 65_536, private=True),
    )
    candidate = (
        owner_file(backup / "candidate-openclaw.json", 512 * 1024, private=True),
        owner_file(backup / "candidate-pin.conf", 65_536, private=True),
    )
    if pin_line(BASELINE) not in original[1].decode("utf-8").splitlines() or candidate != (
        candidate_config(original[0], release),
        original[1].replace(pin_line(BASELINE).encode(), pin_line(release).encode(), 1),
    ):
        raise ValueError("The saved config and pin pair changed")
    before, after = (candidate, original) if restore else (original, candidate)
    current = (owner_file(config, 512 * 1024, private=True), owner_file(pin, 65_536))
    if current != before:
        raise ValueError("Gateway config or pin changed since preparation. Switch aborted.")
    verify_config(backup / ("original-openclaw.json" if restore else "candidate-openclaw.json"),
                  BASELINE if restore else release)
    systemctl("stop", SERVICE)
    current_after_stop = (owner_file(config, 512 * 1024, private=True), owner_file(pin, 65_536))
    if current_after_stop != before:
        systemctl("start", SERVICE)
        raise ValueError("Gateway config or pin changed during shutdown. Switch aborted.")
    try:
        replace_file(config, after[0])
        replace_file(pin, after[1])
        start(after[0])
    except (Exception, KeyboardInterrupt):
        try:
            systemctl("stop", SERVICE)
            replace_file(config, before[0])
            replace_file(pin, before[1])
            start(before[0])
        except (Exception, KeyboardInterrupt):
            raise ValueError("Switch failed and automatic recovery failed. Use the private backup to recover.") from None
        raise ValueError("Switch failed. The prior configuration and service were restored.") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "activate", "rollback"))
    parser.add_argument("backup", type=Path)
    parser.add_argument("--release")
    args = parser.parse_args()
    os.umask(0o077)
    config = Path.home() / ".openclaw/openclaw.json"
    try:
        with (config.parent / ".fo-segment1-install.lock").open("ab") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.action == "prepare":
                prepare(args.release or "", args.backup, config)
            else:
                switch(args.backup, config, restore=args.action == "rollback")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        message = str(error) if type(error) is ValueError else "Gateway release operation failed"
        print(message, file=sys.stderr)
        return 1
    print(f"First Officer release {args.action} completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

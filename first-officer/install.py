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


def pin_file(path):
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_nlink != 1 or mode & 0o022:
        raise ValueError("The service pin has unsafe permissions")
    if info.st_size > 65_536:
        raise ValueError("The service pin exceeds the size limit")
    return path.read_bytes(), [info.st_uid, info.st_gid, mode]


def patch_pin(contents, release):
    expected = shlex.split(pin_line(BASELINE).partition("=")[2])
    expected_config = str(Path.home() / ".openclaw/openclaw.json")
    lines = contents.decode("utf-8").splitlines(keepends=True)
    found = []
    for index, line in enumerate(lines):
        key, separator, args = line.partition("=")
        if separator and key.strip() == "ExecStartPre" and "verify-install.py" in args:
            command = shlex.split(args)
            if (len(command) in (6, 7) and command[0] == expected[0] and
                    command[1:-4] in (["-I", "-B"], ["-B", "-I"], ["-IB"], ["-BI"]) and
                    command[-4] == expected[3] and
                    command[-3] in (expected[4], expected_config) and command[-2:] == expected[5:] and
                    line.count(BASELINE) == 1):
                found.append(index)
    if len(found) != 1:
        raise ValueError("The Round 13 verifier command did not match exactly once")
    index = found[0]
    lines[index] = lines[index].replace(BASELINE, release, 1)
    return "".join(lines).encode("utf-8")


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
        if stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.getuid()) and info.st_size <= 65_536:
            try:
                contents, identity = pin_file(path)
                changed = patch_pin(contents, release)
            except (OSError, ValueError):
                continue
            pins.append((path, contents, changed, identity))
    if len(pins) != 1:
        raise ValueError("Could not find the existing Round 13 service pin")
    path, old_pin, new_pin, identity = pins[0]
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
    (backup / "plan.json").write_text(
        json.dumps({"release": release, "pin": str(path), "pin_identity": identity}) + "\n", encoding="utf-8",
    )


def replace_file(path, payload, mode=0o600, gid=None):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".fo-segment1-", delete=False) as handle:
        try:
            if gid is not None and os.fstat(handle.fileno()).st_gid != gid:
                os.fchown(handle.fileno(), -1, gid)
            os.fchmod(handle.fileno(), mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(handle.name, path)
        finally:
            Path(handle.name).unlink(missing_ok=True)


def authorize_root_pin():
    try:
        subprocess.run(["sudo", "-v"], check=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Sudo authorization for the root-owned verifier pin failed") from None


def sudo_pin(*args, capture=False):
    return subprocess.run(["sudo", "-n", *args], check=True, text=True, timeout=20,
                          stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).stdout


def replace_pin(path, payload, identity):
    uid, gid, mode = identity
    if uid == os.getuid():
        replace_file(path, payload, mode, gid)
        return

    temporary = Path(sudo_pin("/usr/bin/mktemp", str(path.parent / ".fo-pin.XXXXXXXX"), capture=True).strip())
    if temporary.parent != path.parent or not re.fullmatch(r"\.fo-pin\.[A-Za-z0-9]{8}", temporary.name):
        raise ValueError("Could not confirm the privileged pin staging file")
    try:
        with tempfile.NamedTemporaryFile(prefix="fo-pin-source-") as source:
            source.write(payload)
            source.flush()
            sudo_pin("/usr/bin/install", "-o", str(uid), "-g", str(gid), "-m", f"{mode:o}", "-T",
                     "--", source.name, str(temporary))
        sudo_pin("/usr/bin/mv", "-fT", "--", str(temporary), str(path))
    finally:
        if temporary.exists():
            sudo_pin("/usr/bin/rm", "-f", "--", str(temporary))


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
    identity = plan["pin_identity"]
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
    if candidate != (
        candidate_config(original[0], release),
        patch_pin(original[1], release),
    ):
        raise ValueError("The saved config and pin pair changed")
    before, after = (candidate, original) if restore else (original, candidate)
    live_pin, live_identity = pin_file(pin)
    current = (owner_file(config, 512 * 1024, private=True), live_pin)
    if current != before or live_identity != identity:
        raise ValueError("Gateway config or pin changed since preparation. Switch aborted.")
    verify_config(backup / ("original-openclaw.json" if restore else "candidate-openclaw.json"),
                  BASELINE if restore else release)
    if live_identity[0] != os.getuid():
        authorize_root_pin()
    systemctl("stop", SERVICE)
    try:
        live_pin, live_identity = pin_file(pin)
        current_after_stop = (owner_file(config, 512 * 1024, private=True), live_pin)
    except (OSError, ValueError):
        systemctl("start", SERVICE)
        raise ValueError("Could not recheck the config and pin after shutdown. Switch aborted.") from None
    if current_after_stop != before or live_identity != identity:
        systemctl("start", SERVICE)
        raise ValueError("Gateway config or pin changed during shutdown. Switch aborted.")
    try:
        replace_pin(pin, after[1], identity)
        replace_file(config, after[0])
        start(after[0])
    except (Exception, KeyboardInterrupt):
        try:
            systemctl("stop", SERVICE)
            replace_file(config, before[0])
            if pin_file(pin) != (before[1], identity):
                replace_pin(pin, before[1], identity)
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

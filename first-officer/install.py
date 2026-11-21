#!/usr/bin/env python3
"""Prepare, activate or roll back the reviewed First Officer release as operator."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.request import ProxyHandler, build_opener

from stage import BASELINE, ROLLBACK_SLOT, SUPPORTED_BASES, verify_inventory


STORE = Path("/opt/openclaw-fo")
SERVICE = "openclaw-gateway.service"
PLUGIN = "first-officer-conductor"
FAST_MODEL = "litellm/minimax-m3.1-flash"
LANE_KEY_REF = "${OPENCLAW_LITELLM_API_KEY}"
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


def patch_pin(contents, release, previous=BASELINE):
    expected = shlex.split(pin_line(previous).partition("=")[2])
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
                    line.count(previous) == 1):
                found.append(index)
    if len(found) != 1:
        raise ValueError("The prior verifier command did not match exactly once")
    index = found[0]
    lines[index] = lines[index].replace(previous, release, 1)
    return "".join(lines).encode("utf-8")


def gateway_launcher():
    effective = systemctl("show", "-p", "ExecStart", "--value", SERVICE, capture=True)
    commands = re.findall(r"argv\[\]=([^;]+)", effective)
    if len(commands) != 1:
        raise ValueError("Could not identify the running gateway launcher")
    args = shlex.split(commands[0])
    if not args or "gateway" not in args:
        raise ValueError("The running gateway command has an unsupported shape")
    launcher = args[:args.index("gateway")]
    if not launcher or not Path(launcher[0]).is_absolute():
        raise ValueError("The running gateway launcher is not an absolute path")
    binary = Path(launcher[0]).name
    if binary in ("openclaw", "openclaw.mjs", "openclaw.js"):
        return launcher
    if binary in ("node", "nodejs") and any(
        Path(arg).is_absolute() and Path(arg).suffix in (".js", ".mjs", ".cjs")
        and "openclaw" in arg.lower() for arg in launcher[1:]
    ):
        return launcher
    raise ValueError("The running gateway does not use a supported OpenClaw launcher")


def _require_unpacked_plugins(config_path, release, candidate):
    """Map unpublished store paths onto files that exist in the unpacked candidate."""
    try:
        paths = json.loads(Path(config_path).read_text(encoding="utf-8"))["plugins"]["load"]["paths"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("The candidate config does not name the unpacked release") from None
    root = Path(candidate)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("The candidate release must be a directory")
    prefix = str(STORE / f"first-officer-{release}") + "/"
    if not isinstance(paths, list) or not paths:
        raise ValueError("The candidate config does not name the unpacked release")
    for raw in paths:
        if not isinstance(raw, str) or not raw.startswith(prefix):
            raise ValueError("The candidate config does not name the unpacked release")
        relative = Path(raw[len(prefix):])
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != raw[len(prefix):]:
            raise ValueError("The candidate config does not name the unpacked release")
        plugin = root / relative
        if plugin.is_symlink() or not plugin.is_file():
            raise ValueError("The unpacked candidate is missing a plugin file")


def installed_verifier(path, release):
    """Return the service pin's verifier command for this config.

    The pin runs `verify-install.py` with the config, the release digest, and the
    rollback slot. The unpacked candidate directory is not an argument. That
    program is not in this checkout. Preparation checks the unpacked plugins
    itself. This command runs once those files are readable at the store path,
    before the gateway is switched.
    """
    pinned = shlex.split(pin_line(release).partition("=")[2])
    if (len(pinned) != 7 or pinned[:3] != ["/usr/bin/python3", "-I", "-B"]
            or Path(pinned[3]).name != "verify-install.py"
            or pinned[5] != release or pinned[6] != ROLLBACK_SLOT):
        raise ValueError("The installed release verifier rejected the config and pin")
    return [pinned[0], pinned[1], pinned[2], str(STORE / "verify-install.py"),
            str(Path(path)), pinned[5], pinned[6]]


def verify_config(path, release, candidate=None):
    if candidate is not None:
        # The installed verifier reads the store path. Those files are not there
        # during preparation, so this checks the unpacked copies instead.
        _require_unpacked_plugins(path, release, candidate)
    else:
        try:
            subprocess.run(
                installed_verifier(path, release), check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            raise ValueError("The installed release verifier rejected the config and pin") from None

    launcher = gateway_launcher()
    label = "Round 13" if release == BASELINE else "candidate"
    try:
        validation = subprocess.run(
            [*launcher, "config", "validate", "--json"],
            env={**os.environ, "OPENCLAW_CONFIG_PATH": str(path)},
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=60,
        )
    except FileNotFoundError:
        raise ValueError("The running gateway launcher is unavailable to the installer") from None
    except subprocess.TimeoutExpired:
        raise ValueError(f"OpenClaw timed out validating the {label} configuration") from None
    try:
        parsed = json.loads(validation.stdout)
        valid = isinstance(parsed, dict) and parsed.get("valid") is True
    except ValueError:
        valid = False
    if validation.returncode or not valid:
        raise ValueError(f"OpenClaw rejected the {label} configuration using the running gateway launcher")


def candidate_config(original, release, previous=BASELINE, *, budgeted_litellm=True):
    cfg = json.loads(original)
    paths = cfg["plugins"]["load"]["paths"]
    base = STORE / f"first-officer-{previous}"
    expected = [str(base / "first-officer-exec-guard.mjs"),
                str(base / "round6/runtime/first-officer-conductor.mjs")]
    if paths != expected:
        raise ValueError("The active plugin paths do not match the requested prior release")
    entry = cfg["plugins"]["entries"][PLUGIN]
    policy = {"allowModelOverride": True, "allowedModels": [FAST_MODEL], "allowedCompletionModels": [FAST_MODEL]}
    prior_policy = entry.get("llm")
    custom_policy = bool(prior_policy) if previous == BASELINE else prior_policy != policy
    if entry.get("enabled") is not True or custom_policy:
        raise ValueError("The conductor has a custom model policy or is disabled")
    provider = cfg["models"]["providers"]["litellm"]
    if not any(row.get("id") == FAST_MODEL.split("/")[1] for row in provider.get("models", [])):
        raise ValueError("The existing fast model is unavailable in the gateway config")
    if budgeted_litellm:
        if any(name.lower() in {"authorization", "x-litellm-api-key", "x-api-key"}
               for name in provider.get("headers", {})):
            raise ValueError("A custom LiteLLM authorization header would bypass the gateway lane key")
        provider["apiKey"] = LANE_KEY_REF
    cfg["plugins"]["load"]["paths"] = [path.replace(previous, release, 1) for path in expected]
    entry["llm"] = policy
    return (json.dumps(cfg, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def prepare(release, backup, config, previous=BASELINE, candidate=None):
    if previous not in SUPPORTED_BASES or not DIGEST.fullmatch(release) or release in (*SUPPORTED_BASES, ROLLBACK_SLOT):
        raise ValueError("Expected a distinct, full candidate digest")
    root = Path(candidate) if candidate is not None else STORE / f"first-officer-{release}"
    if root.is_symlink():
        raise ValueError("The candidate release must be a directory")
    verify_inventory(root, release)
    dropins = shlex.split(systemctl("show", "-p", "DropInPaths", "--value", SERVICE, capture=True))
    pins = []
    for entry in dropins:
        path = Path(entry)
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.getuid()) and info.st_size <= 65_536:
            try:
                contents, identity = pin_file(path)
                changed = patch_pin(contents, release, previous)
            except (OSError, ValueError):
                continue
            pins.append((path, contents, changed, identity))
    if len(pins) != 1:
        raise ValueError("Could not find the service pin for the requested prior release")
    path, old_pin, new_pin, identity = pins[0]
    original = owner_file(config, 512 * 1024, private=True)
    updated = candidate_config(original, release, previous)
    backup.mkdir(mode=0o700)
    files = {
        "original-openclaw.json": original, "candidate-openclaw.json": updated,
        "original-pin.conf": old_pin, "candidate-pin.conf": new_pin,
    }
    for name, content in files.items():
        (backup / name).write_bytes(content)
    verify_config(backup / "original-openclaw.json", previous)
    verify_config(backup / "candidate-openclaw.json", release, root)
    (backup / "plan.json").write_text(
        json.dumps({"release": release, "previous": previous, "pin": str(path), "pin_identity": identity,
                    "budgeted_litellm": True}) + "\n",
        encoding="utf-8",
    )


def _fsync_parent(path):
    try:
        handle = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _known_pair(config_bytes, pin_bytes, original, candidate):
    return (config_bytes in (original[0], candidate[0]) and pin_bytes in (original[1], candidate[1]))


def _saved_pair(backup, name):
    return (
        owner_file(backup / f"{name}-openclaw.json", 512 * 1024, private=True),
        owner_file(backup / f"{name}-pin.conf", 65_536, private=True),
    )


def _expected_candidate(original, release, previous, plan):
    return (
        candidate_config(original[0], release, previous, budgeted_litellm=plan.get("budgeted_litellm") is True),
        patch_pin(original[1], release, previous),
    )


def _write_journal(backup, target):
    temporary = backup / ".switch-journal.json.tmp"
    payload = (json.dumps({"target": target}) + "\n").encode()
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(handle, "wb") as file:
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, backup / "switch-journal.json")
    os.chmod(backup / "switch-journal.json", 0o600)
    _fsync_parent(backup)


def _clear_journal(backup):
    (backup / "switch-journal.json").unlink(missing_ok=True)
    _fsync_parent(backup)


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
            _fsync_parent(path.parent)
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
        _fsync_parent(path.parent)
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


def _discard_unverified_candidate(release):
    """Remove a candidate the installed verifier rejected before it is published."""
    if not DIGEST.fullmatch(str(release)) or release in (*SUPPORTED_BASES, ROLLBACK_SLOT):
        raise ValueError("The installed release verifier rejected the config and pin")
    published = STORE / f"first-officer-{release}"
    if published.is_symlink():
        raise ValueError("The installed release verifier rejected the config and pin")
    if not published.exists():
        return
    if not published.is_dir() or published.parent != STORE:
        raise ValueError("The installed release verifier rejected the config and pin")
    if published.stat().st_uid == os.getuid():
        shutil.rmtree(published)
        return
    subprocess.run(
        ["sudo", "-n", "rm", "-rf", "--", str(published)],
        check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _discard_inactive_candidate(backup, config, plan):
    """Only clean up if the complete prior pair is live and no switch is in progress."""
    journal = backup / "switch-journal.json"
    try:
        try:
            journal.lstat()
        except FileNotFoundError:
            pass
        else:
            return
        release = plan["release"]
        previous = plan.get("previous", BASELINE)
        pin = Path(plan["pin"])
        dropins = shlex.split(systemctl("show", "-p", "DropInPaths", "--value", SERVICE, capture=True))
        if previous not in SUPPORTED_BASES or str(pin) not in dropins:
            return
        original = _saved_pair(backup, "original")
        live_pin, identity = pin_file(pin)
        if identity != plan["pin_identity"] or (owner_file(config, 512 * 1024, private=True), live_pin) != original:
            return
        if _saved_pair(backup, "candidate") != _expected_candidate(original, release, previous, plan):
            return
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return
    _discard_unverified_candidate(release)


def verify_installed_candidate(backup, config):
    """Verify the candidate at its store path and clean it up on rejection if inactive.

    The gateway is still running. The installed verifier receives the config
    containing the candidate's store paths.
    """
    info = backup.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise ValueError("The installation backup must be a private operator directory")
    plan = json.loads(owner_file(backup / "plan.json", 4096, private=True))
    release = plan["release"]
    if not DIGEST.fullmatch(release) or release in (*SUPPORTED_BASES, ROLLBACK_SLOT):
        raise ValueError("Expected a distinct, full candidate digest")
    published = STORE / f"first-officer-{release}"
    if published.is_symlink() or not published.is_dir():
        raise ValueError("The installed release verifier rejected the config and pin")
    try:
        verify_config(backup / "candidate-openclaw.json", release)
    except ValueError:
        _discard_inactive_candidate(backup, config, plan)
        raise


def switch(backup, config, restore):
    info = backup.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise ValueError("The installation backup must be a private operator directory")
    plan = json.loads(owner_file(backup / "plan.json", 4096, private=True))
    release = plan["release"]
    previous = plan.get("previous", BASELINE)
    pin = Path(plan["pin"])
    identity = plan["pin_identity"]
    if previous not in SUPPORTED_BASES or not DIGEST.fullmatch(release) or str(pin) not in shlex.split(
        systemctl("show", "-p", "DropInPaths", "--value", SERVICE, capture=True)
    ):
        raise ValueError("The current service or release does not match the saved installation plan")
    original = _saved_pair(backup, "original")
    candidate = _saved_pair(backup, "candidate")
    if candidate != _expected_candidate(original, release, previous, plan):
        raise ValueError("The saved config and pin pair changed")
    after = original if restore else candidate
    live_pin, live_identity = pin_file(pin)
    current_config = owner_file(config, 512 * 1024, private=True)
    if live_identity != identity or not _known_pair(current_config, live_pin, original, candidate):
        raise ValueError("Gateway config or pin changed since preparation. Switch aborted.")
    try:
        verify_config(backup / ("original-openclaw.json" if restore else "candidate-openclaw.json"),
                      previous if restore else release)
    except ValueError:
        if not restore:
            _discard_inactive_candidate(backup, config, plan)
        raise
    _write_journal(backup, "original" if restore else "candidate")
    if live_identity[0] != os.getuid():
        authorize_root_pin()
    systemctl("stop", SERVICE)
    try:
        live_pin, live_identity = pin_file(pin)
        current_config = owner_file(config, 512 * 1024, private=True)
    except (OSError, ValueError):
        systemctl("start", SERVICE)
        raise ValueError("Could not recheck the config and pin after shutdown. Switch aborted.") from None
    if live_identity != identity or not _known_pair(current_config, live_pin, original, candidate):
        systemctl("start", SERVICE)
        raise ValueError("Gateway config or pin changed during shutdown. Switch aborted.")
    observed = (current_config, live_pin)
    # Rollback recovery is always the saved prior pair. A mixed live config and
    # pin, or a complete candidate, must not be started again when the restored
    # original fails readiness. Activation may restart a complete pair that was
    # already live, and otherwise returns to that same prior pair.
    recovery = original
    if not restore and observed in (original, candidate):
        recovery = observed
    try:
        if live_pin != after[1]:
            replace_pin(pin, after[1], identity)
        if owner_file(config, 512 * 1024, private=True) != after[0]:
            replace_file(config, after[0])
        start(after[0])
        _clear_journal(backup)
    except (Exception, KeyboardInterrupt):
        try:
            systemctl("stop", SERVICE)
            replace_file(config, recovery[0])
            if pin_file(pin) != (recovery[1], identity):
                replace_pin(pin, recovery[1], identity)
            start(recovery[0])
            _clear_journal(backup)
        except (Exception, KeyboardInterrupt):
            raise ValueError("Switch failed and automatic recovery failed. Use the private backup to recover.") from None
        raise ValueError("Switch failed. The prior configuration and service were restored.") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "verify-candidate", "activate", "rollback"))
    parser.add_argument("backup", type=Path)
    parser.add_argument("--release")
    parser.add_argument("--from-release", choices=SUPPORTED_BASES, default=BASELINE)
    parser.add_argument("--candidate", type=Path,
                        help="Unpacked candidate to validate before it is published")
    args = parser.parse_args()
    os.umask(0o077)
    config = Path.home() / ".openclaw/openclaw.json"
    try:
        with (config.parent / ".fo-segment1-install.lock").open("ab") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.action == "prepare":
                prepare(args.release or "", args.backup, config, args.from_release, args.candidate)
            elif args.action == "verify-candidate":
                verify_installed_candidate(args.backup, config)
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

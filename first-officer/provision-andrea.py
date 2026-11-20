#!/usr/bin/env python3
"""Provision First Officer's restricted Andrea reader from the operator's Nuc."""

import argparse
import base64
import fcntl
import hashlib
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


KEY_LABEL = "openclaw-firstofficer-andrea-read"
READER_COMMAND = "first-officer-andrea-summary-v1"
MODULES = ("andrea-mac.mjs", "andrea-ssh.mjs", "tools.mjs")
HISTORY_SOURCES = {"cache", "live-provider", "provider-unconfigured", "no-authorized-chats", "scope-unverified"}
STATE = Path.home() / ".ssh/openclaw-fo-andrea"
PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]{60,120}={0,2}")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,126}")
ACCOUNT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,59}")
DIAGNOSTIC_FAILURES = {
    "ssh-session": "SSH authentication or the Mac remote shell failed",
    "ssh-authentication": "SSH rejected the login",
    "ssh-host-key": "SSH could not verify the saved host key",
    "ssh-connection": "SSH could not connect to the host",
    "ssh-timeout": "the SSH session timed out",
    "remote-python": "the Mac Python diagnostic did not return a result",
    "remote-runtime": "the Mac diagnostic failed before reading Andrea",
    "node-unavailable": "Node is unavailable on the Mac",
    "node-diagnostic": "Node could not start the Andrea diagnostic",
    "settings-unavailable": "the Mac LaunchAgent settings could not be read",
    "settings-invalid": "the Mac loopback reader settings failed validation",
    "api-unreachable": "the Mac loopback API could not be reached before the timeout",
    "api-authentication": "the Mac loopback API rejected reader authentication",
    "api-response": "the Mac loopback API returned an invalid chat list or error",
    "invalid-counts": "the Mac diagnostic returned invalid counts",
}


class DiagnosticError(ValueError):
    def __init__(self, code):
        self.code = code if code in DIAGNOSTIC_FAILURES else "remote-runtime"
        super().__init__(DIAGNOSTIC_FAILURES[self.code])


def private_file(path, limit=8192):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise ValueError("Unexpected reader file")
    if info.st_mode & 0o077 or info.st_size > limit:
        raise ValueError("Reader file is not private or is too large")
    return path.read_bytes()


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Reader directory must be private")


def write_once(path, payload):
    if path.exists() or path.is_symlink():
        if private_file(path, 256 * 1024) != payload:
            raise ValueError("A reader file differs from the expected contents")
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        staging = Path(handle.name)
        try:
            handle.write(payload)
            handle.flush()
            os.link(staging, path)
        finally:
            staging.unlink(missing_ok=True)


def public_key(text):
    key = " ".join(text.split()[:2])
    if not PUBLIC_KEY.fullmatch(key):
        raise ValueError("The expected Ed25519 public key is unavailable")
    return key


def make_key():
    private_dir(STATE)
    identity = STATE / "id_ed25519"
    with (STATE / "setup.lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not identity.exists() and not identity.is_symlink():
            quiet_run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", KEY_LABEL, "-f", str(identity)])
        private_file(identity)
        key = quiet_run(["/usr/bin/ssh-keygen", "-y", "-f", str(identity)])
    return {"public": public_key(key)}


def quiet_run(args, *, env=None, timeout=20):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                          env=env, check=True, timeout=timeout).stdout


def add_restriction(entry, mode="install"):
    directory = Path.home() / ".ssh"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("The Mac SSH directory has unsafe permissions")
    path = directory / "authorized_keys"
    private_dir(STATE)
    lock_fd = os.open(STATE / "authorized-keys.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        update_authorized_keys(path, entry, mode)


def update_authorized_keys(path, entry, mode):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_CREAT if mode == "install" else 0), 0o600)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022:
            raise ValueError("The Mac authorized keys file has unsafe permissions")
        current = file.read(1024 * 1024 + 1)
        if len(current) > 1024 * 1024:
            raise ValueError("The Mac authorized keys file exceeds the size limit")
        rows = current.splitlines(keepends=True)
        matches = [line.rstrip(b"\r\n") for line in rows if line.rstrip(b"\r\n").endswith(b" " + KEY_LABEL.encode())]
        if matches == [entry]:
            return
        if matches:
            if mode == "install" or len(matches) != 1 or restricted_key(matches[0]) != restricted_key(entry):
                raise ValueError("An existing First Officer reader key needs operator review")
            if mode == "refresh":
                write_once(STATE / "reader.previous", matches[0])
            updated = b"".join(entry + b"\n" if line.rstrip(b"\r\n") == matches[0] else line for line in rows)
        else:
            if mode != "install":
                raise ValueError("The existing restricted reader key could not be verified")
            updated = current + (b"\n" if current and not current.endswith(b"\n") else b"") + entry + b"\n"
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".fo-andrea-key-", delete=False) as temp:
            staged = Path(temp.name)
            try:
                os.fchmod(temp.fileno(), stat.S_IMODE(info.st_mode))
                temp.write(updated)
                temp.flush()
                os.fsync(temp.fileno())
                live = path.lstat()
                if (live.st_dev, live.st_ino, live.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_mtime_ns):
                    raise ValueError("Mac authorized keys changed during reader update")
                os.replace(staged, path)
            finally:
                staged.unlink(missing_ok=True)


def restricted_key(line):
    match = re.fullmatch(rb'restrict,command="([^"\r\n]+)" (ssh-ed25519 [A-Za-z0-9+/=]+) ' +
                         KEY_LABEL.encode(), line)
    if not match:
        raise ValueError("Expected the dedicated restricted reader line")
    argv = shlex.split(match[1].decode())
    root = Path.home() / ".local/libexec/openclaw-firstofficer-andrea"
    if len(argv) != 2 or not Path(argv[0]).is_absolute() or Path(argv[0]).name != "node":
        raise ValueError("Expected the reviewed reader command")
    script = Path(argv[1])
    if (script.parent.parent != root or script.name != "andrea-mac.mjs"
            or not re.fullmatch(r"[a-f0-9]{64}", script.parent.name) or not script.is_file()
            or script.resolve() != script):
        raise ValueError("Expected a versioned Mac reader on disk")
    return public_key(match[2].decode())


def mac_node():
    node = shutil.which("node")
    if not node:
        node = next((str(path) for path in map(Path, ("/opt/homebrew/bin/node", "/usr/local/bin/node"))
                     if os.access(path, os.X_OK)), None)
    if not node:
        raise ValueError("Node is unavailable to the Mac reader")
    return node


def reader_modules(payload):
    files = payload["files"]
    if sorted(files) != sorted(MODULES):
        raise ValueError("Expected the three reviewed reader modules")
    return files


def validate_counts(counts):
    fields = ("listedChats", "groupChats", "excludedChats", "returnedRows", "botRows",
              "rowsWithValidTimestamp", "eligibleChats", "freshChatMetadata", "selectedChats",
              "readFailures", "acceptedRows", "recentTextPreviews")
    if (not isinstance(counts, dict) or counts.keys() != set(fields)
            or any(type(value) is not int or not 0 <= value <= 100 for value in counts.values())):
        raise DiagnosticError("invalid-counts")
    return counts


def validate_sync(status):
    states = {
        "channel": {"connected", "disconnected"},
        "scope": {"allowlist", "contacts_only", "all_synced"},
        "transport": {"not_checked", "reachable", "auth_failed", "unreachable"},
        "webhook": {"not_configured", "registered", "missing", "auth_failed", "unreachable"},
        "shadowPoll": {"recent", "older"},
        "lastInbound": {"recent", "older"},
    }
    if not isinstance(status, dict) or status.keys() != states.keys() or any(
        not isinstance(value, str) or (value != "unknown" and value not in states[key])
        for key, value in status.items()
    ):
        raise DiagnosticError("invalid-counts")
    return status


def diagnose_mac(payload):
    try:
        node = mac_node()
    except ValueError:
        raise DiagnosticError("node-unavailable") from None
    files = reader_modules(payload)
    with tempfile.TemporaryDirectory(prefix="fo-andrea-check-") as directory:
        # macOS can expose its temp directory through /var, a symlink.
        # Node's module URL uses the resolved path for its entry-point check.
        path = Path(directory).resolve(strict=True)
        for name in MODULES:
            write_once(path / name, files[name].encode())
        env = {name: value for name, value in os.environ.items() if name != "SSH_ORIGINAL_COMMAND"}
        option = "--diagnose-sync" if payload.get("sync") else "--diagnose"
        result = subprocess.run([node, str(path / "andrea-mac.mjs"), option],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, text=True)
        try:
            counts = json.loads(result.stdout)
        except ValueError:
            raise DiagnosticError("node-diagnostic") from None
    if result.returncode:
        code = counts.get("problem") if isinstance(counts, dict) else None
        if code in ("settings-unavailable", "settings-invalid", "api-unreachable", "api-authentication", "api-response"):
            raise DiagnosticError(code)
        raise DiagnosticError("node-diagnostic")
    return validate_sync(counts) if payload.get("sync") else validate_counts(counts)


def prepare_mac(payload):
    key = public_key(payload["public"])
    pin = public_key(Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text())
    node = mac_node()
    files = reader_modules(payload)
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    reader = Path.home() / ".local/libexec/openclaw-firstofficer-andrea" / digest
    private_dir(reader.parent)
    private_dir(reader)
    for name in MODULES:
        write_once(reader / name, files[name].encode())
    main = str(reader / "andrea-mac.mjs")
    refresh = payload.get("refresh") is True
    if refresh:
        env = {name: value for name, value in os.environ.items() if name != "SSH_ORIGINAL_COMMAND"}
        proof = json.loads(quiet_run([node, main, "--diagnose-live"], env=env))
        source = proof.get("source")
        if source not in HISTORY_SOURCES:
            raise ValueError("The candidate did not return verified live-history evidence")
        if type(proof.get("recentTextPreviews")) is not int or not 1 <= proof["recentTextPreviews"] <= 80:
            if type(proof.get("readFailures")) is int and proof["readFailures"] > 0:
                raise ValueError("The candidate could not read the selected recent history")
            raise ValueError("The candidate finds no recent previews. History source " + source + ".")
    result = json.loads(quiet_run([node, main], env={**os.environ, "SSH_ORIGINAL_COMMAND": READER_COMMAND}))
    if result.get("version") != 1 or not str(result.get("summary", "")).startswith("Andrea · "):
        raise ValueError("Mac reader did not return a verified summary")
    target = shlex.join([node, main]).replace("\\", "\\\\").replace('"', '\\"')
    if "\n" in target or "\r" in target:
        raise ValueError("Mac reader command path is invalid")
    entry = f'restrict,command="{target}" {key} {KEY_LABEL}'.encode()
    if refresh and not re.search(r"\n\d+ incoming, \d+ outgoing across [1-4] recent conversations\.", result["summary"]):
        raise ValueError("The candidate reader still finds no recent text previews")
    add_restriction(entry, "refresh" if refresh else "install")
    return {"pin": pin}


def restore_mac():
    add_restriction(private_file(STATE / "reader.previous"), "restore")
    return {"restored": True}


def verify_from_bobserver(payload):
    host, user = payload["host"], payload["user"]
    if not HOST.fullmatch(host) or not ACCOUNT.fullmatch(user):
        raise ValueError("Invalid Mac SSH destination")
    pin = public_key(payload["pin"])
    private_dir(STATE)
    private_file(STATE / "id_ed25519")
    write_once(STATE / "known_hosts", f"{host} {pin}\n".encode())
    write_once(STATE / "route.json", (json.dumps({"host": host, "user": user}) + "\n").encode())
    encoded = base64.b64encode(payload["client"].encode()).decode("ascii")
    verification = (
        r"if (!/\n\d+ incoming, \d+ outgoing across [1-4] recent conversations\./.test(summary ?? '')) process.exit(1);"
        if payload.get("require_recent") else
        "if (!summary) process.exit(1);"
    )
    code = (
        f"const m = await import('data:text/javascript;base64,{encoded}');"
        "const summary = await m.sshAndreaSummary(AbortSignal.timeout(15000));"
        + verification
    )
    quiet_run(["node", "--input-type=module", "-e", code])
    return {"verified": True}


def run_on_host(host, action, payload, source):
    prefix = "REMOTE_PAYLOAD = " + repr(payload) + "\n"
    call = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8",
         "-o", "NumberOfPasswordPrompts=2", host, "python3 - " + action],
        input=prefix + source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=150, env={**os.environ, "LC_ALL": "C"},
    )
    if call.returncode == 255:
        stderr = getattr(call, "stderr", "") or ""
        if re.search(r"permission denied|too many authentication failures|authentication failed", stderr, re.I):
            code = "ssh-authentication"
        elif re.search(r"host key verification failed|host identification has changed|strict checking", stderr, re.I):
            code = "ssh-host-key"
        elif re.search(r"could not resolve hostname|connect to host|connection (?:closed|reset|timed out)", stderr, re.I):
            code = "ssh-connection"
        else:
            code = "ssh-session"
        if action == "remote-diagnose":
            raise DiagnosticError(code)
        raise ValueError(DIAGNOSTIC_FAILURES[code] + ".")
    if action == "remote-diagnose":
        try:
            result = json.loads(call.stdout)
        except ValueError:
            raise DiagnosticError("remote-python") from None
        if call.returncode:
            code = result.get("diagnostic") if isinstance(result, dict) else None
            raise DiagnosticError(code if isinstance(code, str) else "remote-runtime")
        return validate_sync(result) if payload.get("sync") else validate_counts(result)
    if not call.stdout:
        if call.returncode in (126, 127):
            raise ValueError("The remote shell could not start Python.")
        raise ValueError("The SSH command returned no verification result.")
    try:
        result = json.loads(call.stdout)
    except ValueError:
        raise ValueError("The remote helper did not return a valid verification result.") from None
    if call.returncode:
        detail = result.get("problem")
        if isinstance(detail, str) and re.fullmatch(r"[A-Za-z0-9 .-]{1,120}", detail):
            raise ValueError(detail)
        raise ValueError("The host could not complete the reader check.")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mac-host", required=True)
    parser.add_argument("--mac-user", required=True)
    parser.add_argument("--bob-host", default="bobserver")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--diagnose", action="store_true", help="Print only counts and freshness from the Mac reader")
    action.add_argument("--diagnose-sync", action="store_true", help="Print only the Mac reader's upstream sync status")
    action.add_argument("--refresh-reader", action="store_true", help="Switch the existing restricted key to this reviewed reader")
    action.add_argument("--restore-reader", action="store_true", help="Restore the previous restricted Mac reader")
    args = parser.parse_args()
    if not HOST.fullmatch(args.mac_host) or not ACCOUNT.fullmatch(args.mac_user) or not HOST.fullmatch(args.bob_host):
        parser.error("Use plain SSH host and user names")
    source = Path(__file__).read_text(encoding="utf-8")
    module_dir = Path(__file__).with_name("segment1")
    files = {name: (module_dir / name).read_text(encoding="utf-8") for name in MODULES}
    mac = args.mac_user + "@" + args.mac_host
    if args.diagnose or args.diagnose_sync:
        try:
            counts = run_on_host(mac, "remote-diagnose", {"files": files, "sync": args.diagnose_sync}, source)
        except DiagnosticError as error:
            print(f"Andrea diagnostic failed [{error.code}]. {error}.", file=sys.stderr)
            return 1
        except subprocess.TimeoutExpired:
            print("Andrea diagnostic failed [ssh-timeout]. The SSH session timed out.", file=sys.stderr)
            return 1
        except (OSError, ValueError, subprocess.SubprocessError):
            print("Andrea diagnostic failed [remote-runtime]. The Mac diagnostic did not complete.", file=sys.stderr)
            return 1
        print(json.dumps(counts, sort_keys=True))
        return 0
    if args.restore_reader:
        try:
            if run_on_host(mac, "remote-restore", {}, source) != {"restored": True}:
                raise ValueError("Mac reader rollback could not be verified")
        except (OSError, ValueError, subprocess.SubprocessError):
            print("Andrea Mac reader rollback failed.", file=sys.stderr)
            return 1
        print("Previous restricted Mac reader restored.")
        return 0
    phase = "Bobserver reader key"
    try:
        identity = run_on_host(args.bob_host, "remote-key", {}, source)
        phase = "Mac Mini reader and restricted key"
        print("SSH may ask for the Mac Mini password.", flush=True)
        target = run_on_host(mac, "remote-mac", {**identity, "files": files, "refresh": args.refresh_reader}, source)
        phase = "Bobserver-to-Mac reader verification"
        result = run_on_host(args.bob_host, "remote-verify", {
            "host": args.mac_host, "user": args.mac_user, "pin": target["pin"], "client": files["andrea-ssh.mjs"],
            "require_recent": args.refresh_reader,
        }, source)
        if result != {"verified": True}:
            raise ValueError("The reader did not verify")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print("Andrea setup failed at " + phase + ".", file=sys.stderr)
        if type(error) is ValueError:
            print(str(error), file=sys.stderr)
        elif isinstance(error, subprocess.TimeoutExpired):
            print("The SSH or reader request timed out.", file=sys.stderr)
        return 1
    print("Andrea reader verified from bobserver.")
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    if "REMOTE_PAYLOAD" in globals():
        try:
            mode = sys.argv[1]
            result = {
                "remote-key": lambda: make_key(),
                "remote-mac": lambda: prepare_mac(REMOTE_PAYLOAD),
                "remote-diagnose": lambda: diagnose_mac(REMOTE_PAYLOAD),
                "remote-restore": restore_mac,
                "remote-verify": lambda: verify_from_bobserver(REMOTE_PAYLOAD),
            }[mode]()
            print(json.dumps(result))
        except DiagnosticError as error:
            print(json.dumps({"diagnostic": error.code}))
            raise SystemExit(1) from None
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            detail = str(error) if type(error) is ValueError else "The host could not complete the reader check."
            print(json.dumps({"problem": detail}))
            raise SystemExit(1) from None
    else:
        raise SystemExit(main())

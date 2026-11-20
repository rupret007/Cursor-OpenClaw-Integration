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
STATE = Path.home() / ".ssh/openclaw-fo-andrea"
PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]{60,120}={0,2}")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,126}")
ACCOUNT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,59}")


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


def add_restriction(entry):
    directory = Path.home() / ".ssh"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("The Mac SSH directory has unsafe permissions")
    path = directory / "authorized_keys"
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022:
            raise ValueError("The Mac authorized keys file has unsafe permissions")
        fcntl.flock(file, fcntl.LOCK_EX)
        current = file.read(1024 * 1024 + 1)
        if len(current) > 1024 * 1024:
            raise ValueError("The Mac authorized keys file exceeds the size limit")
        matches = [line for line in current.splitlines() if line.endswith(b" " + KEY_LABEL.encode())]
        if matches == [entry]:
            return
        if matches:
            raise ValueError("An existing First Officer reader key needs operator review")
        if current and not current.endswith(b"\n"):
            file.write(b"\n")
        file.write(entry + b"\n")
        file.flush()
        os.fsync(file.fileno())


def prepare_mac(payload):
    key = public_key(payload["public"])
    pin = public_key(Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text())
    node = shutil.which("node")
    if not node:
        node = next((str(path) for path in map(Path, ("/opt/homebrew/bin/node", "/usr/local/bin/node"))
                     if os.access(path, os.X_OK)), None)
    if not node:
        raise ValueError("Node is unavailable to the Mac reader")
    files = payload["files"]
    if sorted(files) != sorted(MODULES):
        raise ValueError("Expected the three reviewed reader modules")
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    reader = Path.home() / ".local/libexec/openclaw-firstofficer-andrea" / digest
    private_dir(reader.parent)
    private_dir(reader)
    for name in MODULES:
        write_once(reader / name, files[name].encode())
    main = str(reader / "andrea-mac.mjs")
    result = json.loads(quiet_run([node, main], env={**os.environ, "SSH_ORIGINAL_COMMAND": READER_COMMAND}))
    if result.get("version") != 1 or not str(result.get("summary", "")).startswith("Andrea · "):
        raise ValueError("Mac reader did not return a verified summary")
    target = shlex.join([node, main]).replace("\\", "\\\\").replace('"', '\\"')
    if "\n" in target or "\r" in target:
        raise ValueError("Mac reader command path is invalid")
    entry = f'restrict,command="{target}" {key} {KEY_LABEL}'.encode()
    add_restriction(entry)
    return {"pin": pin}


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
    code = (
        f"const m = await import('data:text/javascript;base64,{encoded}');"
        "if (!(await m.sshAndreaSummary(AbortSignal.timeout(15000)))) process.exit(1);"
    )
    quiet_run(["node", "--input-type=module", "-e", code])
    return {"verified": True}


def run_on_host(host, action, payload, source):
    prefix = "REMOTE_PAYLOAD = " + repr(payload) + "\n"
    call = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8",
         "-o", "NumberOfPasswordPrompts=1", host, "python3 - " + action],
        input=prefix + source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, timeout=150,
    )
    if not call.stdout:
        raise ValueError("The SSH command returned no verification result.")
    result = json.loads(call.stdout)
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
    args = parser.parse_args()
    if not HOST.fullmatch(args.mac_host) or not ACCOUNT.fullmatch(args.mac_user) or not HOST.fullmatch(args.bob_host):
        parser.error("Use plain SSH host and user names")
    source = Path(__file__).read_text(encoding="utf-8")
    module_dir = Path(__file__).with_name("segment1")
    files = {name: (module_dir / name).read_text(encoding="utf-8") for name in MODULES}
    mac = args.mac_user + "@" + args.mac_host
    phase = "Bobserver reader key"
    try:
        identity = run_on_host(args.bob_host, "remote-key", {}, source)
        phase = "Mac Mini reader and restricted key"
        print("SSH may ask for the Mac Mini password.", flush=True)
        target = run_on_host(mac, "remote-mac", {**identity, "files": files}, source)
        phase = "Bobserver-to-Mac reader verification"
        result = run_on_host(args.bob_host, "remote-verify", {
            "host": args.mac_host, "user": args.mac_user, "pin": target["pin"], "client": files["andrea-ssh.mjs"],
        }, source)
        if result != {"verified": True}:
            raise ValueError("The reader did not verify")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print("Andrea setup failed at " + phase + ".", file=sys.stderr)
        if type(error) is ValueError:
            print(str(error), file=sys.stderr)
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
                "remote-verify": lambda: verify_from_bobserver(REMOTE_PAYLOAD),
            }[mode]()
            print(json.dumps(result))
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            detail = str(error) if type(error) is ValueError else "The host could not complete the reader check."
            print(json.dumps({"problem": detail}))
            raise SystemExit(1) from None
    else:
        raise SystemExit(main())

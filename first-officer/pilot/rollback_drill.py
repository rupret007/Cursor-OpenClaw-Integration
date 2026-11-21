#!/usr/bin/env python3
"""Time a restore of the prior config and pin on a staged copy.

The copy is built in the directory the caller names. The restore writes the
prior bytes back and reads them again. It does not call systemctl, read the
live gateway config, or write the release store.
"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import time


def _installer():
    parent = Path(__file__).resolve().parents[1]
    inserted = str(parent) not in sys.path
    if inserted:
        sys.path.insert(0, str(parent))
    try:
        path = parent / "install.py"
        spec = importlib.util.spec_from_file_location("fo_install_drill", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if inserted and str(parent) in sys.path:
            sys.path.remove(str(parent))


RELEASE = hashlib.sha256(b"fo-rollback-drill").hexdigest()


def refuse_live(path):
    resolved = Path(path).resolve()
    blocked = (
        Path("/opt/openclaw-fo"),
        Path.home() / ".openclaw",
        Path.home() / ".local/state/openclaw-conductor",
        Path.home() / "work/lanes/openclaw-backup2-20261118-1340",
    )
    for marker in blocked:
        if resolved == marker or marker in resolved.parents:
            raise ValueError("The rollback drill must stay on a staged copy")


def _root(path):
    refuse_live(path)
    root = Path(path)
    if root.is_symlink():
        raise ValueError("The drill root must be a private directory")
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    info = root.lstat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("The drill root must be a private directory")
    return root


def _write(path, payload):
    temporary = path.with_name(f".{path.name}.tmp")
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(handle, "wb") as file:
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _read(path):
    if path.is_symlink():
        raise ValueError("The staged file must be a private owner file")
    handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(handle, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("The staged file must be a private owner file")
        return file.read()


def _pair(installer):
    if RELEASE in installer.SUPPORTED_BASES:
        raise ValueError("The drill release collides with a supported pin")
    base = installer.STORE / f"first-officer-{installer.BASELINE}"
    original_config = (json.dumps({
        "gateway": {"port": 18789},
        "plugins": {
            "load": {"paths": [
                str(base / "first-officer-exec-guard.mjs"),
                str(base / "round6/runtime/first-officer-conductor.mjs"),
            ]},
            "entries": {installer.PLUGIN: {"enabled": True}},
        },
        "models": {"providers": {"litellm": {
            "apiKey": "${LEGACY_MODEL_KEY}",
            "models": [{"id": installer.FAST_MODEL.split("/")[1]}],
        }}},
    }).encode("utf-8"))
    original_pin = ("[Service]\n" + installer.pin_line(installer.BASELINE) + "\n").encode("utf-8")
    candidate_config = installer.candidate_config(original_config, RELEASE)
    candidate_pin = installer.patch_pin(original_pin, RELEASE)
    if candidate_config == original_config or candidate_pin == original_pin:
        raise ValueError("The staged candidate did not differ from the prior pin")
    return original_config, original_pin, candidate_config, candidate_pin


def restore(config_path, pin_path, original_config, original_pin, candidate_config, candidate_pin):
    """Put the prior bytes back. A file that is not the candidate is left as it is."""
    if _read(config_path) != candidate_config or _read(pin_path) != candidate_pin:
        raise ValueError("The staged copy is not the candidate that was prepared")
    _write(config_path, original_config)
    _write(pin_path, original_pin)
    if _read(config_path) != original_config or _read(pin_path) != original_pin:
        raise ValueError("The staged copy did not return to the prior pin")


def drill(root):
    installer = _installer()
    root = _root(root)
    original_config, original_pin, candidate_config, candidate_pin = _pair(installer)
    config_path = root / "openclaw.json"
    pin_path = root / "pin.conf"
    _write(config_path, candidate_config)
    _write(pin_path, candidate_pin)
    started = time.perf_counter_ns()
    restore(config_path, pin_path, original_config, original_pin, candidate_config, candidate_pin)
    elapsed_us = (time.perf_counter_ns() - started) // 1000
    if type(elapsed_us) is not int or elapsed_us < 0:
        raise ValueError("The rollback timing is unusable")
    receipt = {
        "elapsed_us": elapsed_us, "gateway_touched": False, "previous": installer.BASELINE,
        "release": RELEASE, "restored": True,
    }
    _write(root / "receipt.json", (json.dumps(receipt, sort_keys=True) + "\n").encode("utf-8"))
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = drill(args.root)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        message = str(error) if type(error) is ValueError else "The staged rollback could not be completed"
        print(message, file=sys.stderr)
        return 1
    print(f"Restored the prior pin in {receipt['elapsed_us']} us. The gateway was not touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Validate a pinned standalone Captain and render a user-service definition."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys

LABEL = "local.openclaw.captain"
UNIT = "openclaw-captain.service"
ROOT = Path(__file__).resolve().parent
RUNTIME = "round6/runtime/segment1/captain-state.py"


def systemd_quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(node, command, environment):
    if node == "mac-mini":
        return plistlib.dumps({
            "Label": LABEL,
            "ProgramArguments": command,
            "EnvironmentVariables": environment,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 15,
        })
    entries = "\n".join("Environment=" + systemd_quote(key + "=" + value) for key, value in environment.items())
    argv = " ".join(map(systemd_quote, command))
    return f"""[Unit]
Description=OpenClaw Captain
After=network-online.target

[Service]
Type=simple
{entries}
ExecStart={argv}
KillMode=control-group
Restart=on-failure
RestartSec=15s
RestartPreventExitStatus=78
TimeoutStopSec=30s

[Install]
WantedBy=default.target
""".encode("utf-8")


def load_module(name, source):
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def absolute(path):
    if not path.is_absolute() or any(char in str(path) for char in "\n\r%$"):
        raise ValueError("Use absolute paths without service expansion characters")


def executable(path):
    absolute(path)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("The standalone runtime executable is unavailable")


def private_config(path):
    absolute(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Use a private local configuration file")
    return path.read_bytes()


def prepare(node, release, python, node_binary, openclaw, config, state, secrets, check=subprocess.run):
    for path in (release, config, state, secrets):
        absolute(path)
    executable(python)
    executable(node_binary)
    executable(openclaw)
    if node_binary.name != "node" or openclaw.name != "openclaw":
        raise ValueError("The guarded launcher expects the installed OpenClaw executable")
    pin = re.fullmatch(r"first-officer-([a-f0-9]{64})", release.name)
    if not pin or release.resolve() != release:
        raise ValueError("Expected the pinned candidate release path")
    stage = load_module("fo_standalone_stage", ROOT / "stage.py")
    stage.verify_inventory(release, pin.group(1))
    config_data = json.loads(private_config(config))
    plugin_paths = config_data["plugins"]["load"]["paths"]
    expected = [str(release / "first-officer-exec-guard.mjs"),
                str(release / "round6/runtime/first-officer-conductor.mjs")]
    if not isinstance(plugin_paths, list) or any(plugin_paths.count(path) != 1 for path in expected):
        raise ValueError("The gateway configuration does not select the pinned Captain plugin")
    info = state.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("Use a private standalone gateway state directory")
    authority = load_module("fo_standalone_state", release / RUNTIME)
    absolute(secrets)
    environment = {
        "FO_LOCAL_NODE": node, "OPENCLAW_CONFIG_PATH": str(config), "OPENCLAW_STATE_DIR": str(state),
        "PATH": os.pathsep.join(dict.fromkeys((str(node_binary.parent), str(openclaw.parent), str(python.parent), "/usr/bin", "/bin"))),
    }
    result = check([str(openclaw), "config", "validate"], check=False, timeout=30,
                   env={**authority.gateway_environment(secrets), **environment},
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError("OpenClaw rejected the standalone gateway configuration")
    command = [str(python), "-I", "-B", str(release / RUNTIME), "local-run", str(openclaw), str(secrets)]
    return render(node, command, environment)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", choices=("mac-mini", "nucbox"), required=True)
    for name in ("release", "python", "node-binary", "openclaw", "config", "state", "environment"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        definition = prepare(args.node, args.release, args.python, args.node_binary, args.openclaw,
                             args.config, args.state, args.environment)
    except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
        print("Standalone preflight failed. Check the pinned release, private paths and local OpenClaw config.",
              file=sys.stderr)
        return 1
    sys.stdout.buffer.write(definition)
    return 0


if __name__ == "__main__":
    sys.exit(main())

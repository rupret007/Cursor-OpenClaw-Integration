#!/usr/bin/env python3
"""Validate a pinned standalone Captain and render a service definition."""
import argparse
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory

LABEL = "local.openclaw.captain"
BROKER_LABEL = "local.openclaw.captain-broker"
UNIT = "openclaw-captain.service"
ROOT = Path(__file__).resolve().parent
RUNTIME = "round6/runtime/segment1/captain-state.py"
STANDBY = "round6/runtime/segment1/queue-runtime/standby_launch.py"
BROKER = "round6/runtime/segment1/queue-runtime/standby_broker.py"


def systemd_quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(node, command, environment, *, mac_account=None):
    if node == "mac-mini":
        definition = {
            "Label": LABEL,
            "ProgramArguments": command,
            "EnvironmentVariables": environment,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 15,
        }
        if mac_account is not None:
            definition.update(UserName=mac_account.pw_name, WorkingDirectory=mac_account.pw_dir, RunAtLoad=True)
        return plistlib.dumps(definition)
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
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
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


def check_telegram_owner(config):
    telegram = config.get("channels", {}).get("telegram")
    if telegram is None or telegram.get("enabled") is False:
        return
    conductor = config.get("plugins", {}).get("entries", {}).get("first-officer-conductor", {})
    if conductor.get("hooks", {}).get("allowConversationAccess") is not True:
        raise ValueError("The Captain needs conversation-hook access for Telegram")
    allow = telegram.get("allowFrom")
    approvers = telegram.get("execApprovals", {}).get("approvers")
    if (telegram.get("dmPolicy") != "allowlist" or not isinstance(allow, list) or len(allow) != 1
            or not isinstance(approvers, list) or len(approvers) != 1):
        raise ValueError("Use the same single Telegram owner and approver")
    owner = re.sub(r"^(?:telegram|tg):", "", str(allow[0]), flags=re.IGNORECASE)
    approver = re.sub(r"^(?:telegram|tg):", "", str(approvers[0]), flags=re.IGNORECASE)
    if owner != approver or not re.fullmatch(r"[1-9][0-9]{0,15}", owner):
        raise ValueError("Use the same single Telegram owner and approver")


def verify_release(release):
    absolute(release)
    pin = re.fullmatch(r"first-officer-([a-f0-9]{64})", release.name)
    if not pin or release.resolve() != release:
        raise ValueError("Expected the pinned candidate release path")
    stage = load_module("fo_standalone_stage", ROOT / "stage.py")
    stage.verify_inventory(release, pin.group(1))


def prepare_mac_broker(release, python, state, config, listen, *, review_queue_config=None,
                       project_queue_config=None, queue_environment=None, memory_node=None):
    if os.getuid() == 0 or os.getuid() != os.geteuid():
        raise ValueError("Prepare the broker as its unprivileged service account")
    for path in (state, config):
        absolute(path)
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("Use a private broker state and configuration directory")
    executable(python)
    verify_release(release)
    if not (release / BROKER).is_file():
        raise ValueError("Pinned release is missing the broker")
    if listen.is_global or listen.is_unspecified or listen.is_multicast:
        raise ValueError("Select a specific private broker interface")
    key, telegram = config / "broker.key", config / "telegram.json"
    for path in (key, telegram):
        private_config(path)
    certificates = {name: config / filename for name, filename in (
        ("ca", "ca.pem"), ("cert", "broker.pem"), ("nuc-cert", "nuc.pem"), ("mac-cert", "mac.pem"),
    )}
    for path in certificates.values():
        if not path.is_file() or path.is_symlink() or not os.access(path, os.R_OK):
            raise ValueError("Missing broker TLS material")
    command = [str(python), "-I", "-B", str(release / BROKER), "--listen", str(listen), "--state", str(state),
               "--key", str(key), "--telegram-config", str(telegram)]
    for flag, path in certificates.items():
        command.extend(("--" + flag, str(path)))
    if (queue_environment is None) != (review_queue_config is None and project_queue_config is None):
        raise ValueError("Configure broker queue policies and their private environment together")
    for flag, path in (("review-queue-config", review_queue_config), ("project-queue-config", project_queue_config),
                       ("queue-environment", queue_environment)):
        if path is not None:
            private_config(path)
            command.extend(("--" + flag, str(path)))
    if memory_node is not None:
        executable(memory_node)
        command.extend(("--memory-node", str(memory_node)))
    return plistlib.dumps({
        "Label": BROKER_LABEL, "ProgramArguments": command, "UserName": pwd.getpwuid(os.getuid()).pw_name,
        "WorkingDirectory": str(state), "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 15, "Umask": 0o077,
    })


def prepare(node, release, python, node_binary, openclaw, config, state, secrets,
            check=subprocess.run, *, mac_launch_daemon=False, standby_profile=None):
    mac_account = None
    if mac_launch_daemon:
        if node != "mac-mini" or os.getuid() == 0 or os.getuid() != os.geteuid():
            raise ValueError("Prepare a Mac LaunchDaemon as the unprivileged Mac gateway account")
        mac_account = pwd.getpwuid(os.getuid())
        absolute(Path(mac_account.pw_dir))
    for path in (release, config, state, secrets):
        absolute(path)
    executable(python)
    executable(node_binary)
    executable(openclaw)
    if node_binary.name != "node" or openclaw.name != "openclaw":
        raise ValueError("The guarded launcher expects the installed OpenClaw executable")
    verify_release(release)
    config_data = json.loads(private_config(config))
    check_telegram_owner(config_data)
    plugin_paths = config_data["plugins"]["load"]["paths"]
    expected = [str(release / "first-officer-exec-guard.mjs"),
                str(release / "round6/runtime/first-officer-conductor.mjs")]
    if not isinstance(plugin_paths, list) or any(plugin_paths.count(path) != 1 for path in expected):
        raise ValueError("The gateway configuration does not select the pinned Captain plugin")
    info = state.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("Use a private standalone gateway state directory")
    absolute(secrets)
    environment = {
        "FO_LOCAL_NODE": node, "OPENCLAW_CONFIG_PATH": str(config), "OPENCLAW_STATE_DIR": str(state),
        "PATH": os.pathsep.join(dict.fromkeys((str(node_binary.parent), str(openclaw.parent), str(python.parent), "/usr/bin", "/bin"))),
    }
    if mac_account is not None:
        environment["HOME"] = mac_account.pw_dir
    with TemporaryDirectory(prefix=".standalone-preflight-", dir=state) as scratch:
        if standby_profile is None:
            authority = load_module("fo_standalone_state", release / RUNTIME)
            validation_env = {**authority.gateway_environment(secrets), **environment}
            command = [str(python), "-I", "-B", str(release / RUNTIME), "local-run", str(openclaw), str(secrets)]
        else:
            absolute(standby_profile)
            launcher = load_module("fo_standby_launch", release / STANDBY)
            profile = launcher.profile_from(standby_profile)
            template, _ = launcher.template_from(config)
            checked_config = Path(scratch) / "gateway.json"
            # Config validation needs a syntactically valid local token. This token cannot reach Telegram.
            launcher.write_gateway_config(checked_config, template, 1, f"{profile['bot_id']}:" + "x" * 32)
            validation_env = {**launcher.gateway_env(secrets), **environment, "OPENCLAW_CONFIG_PATH": str(checked_config)}
            command = [str(python), "-I", "-B", str(release / STANDBY), str(openclaw),
                       str(config), str(secrets), str(standby_profile)]
            if mac_account is not None:
                launcher.prewarm_template(template, release)
                command.append("--prewarm-on-start")
        result = check([str(openclaw), "config", "validate"], check=False, timeout=30,
                       env=validation_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise ValueError("OpenClaw rejected the standalone gateway configuration")
    return render(node, command, environment, mac_account=mac_account)


def mac_broker_main(argv):
    parser = argparse.ArgumentParser(description="Render the separate Mac Captain broker LaunchDaemon.")
    for name in ("release", "python", "state", "config-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--listen", type=ipaddress.IPv4Address, required=True)
    for option in ("review-queue-config", "project-queue-config", "queue-environment", "memory-node"):
        parser.add_argument("--" + option, type=Path)
    args = parser.parse_args(argv)
    try:
        definition = prepare_mac_broker(args.release, args.python, args.state, args.config_dir, args.listen,
                                        review_queue_config=args.review_queue_config,
                                        project_queue_config=args.project_queue_config,
                                        queue_environment=args.queue_environment, memory_node=args.memory_node)
    except (OSError, ValueError):
        print("Broker preflight failed. Check the release, interface and private service paths.", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(definition)
    return 0


def main():
    if sys.argv[1:2] == ["mac-broker"]:
        return mac_broker_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", choices=("mac-mini", "nucbox"), required=True)
    for name in ("release", "python", "node-binary", "openclaw", "config", "state", "environment"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--mac-launch-daemon", action="store_true")
    parser.add_argument("--standby-profile", type=Path)
    args = parser.parse_args()
    try:
        definition = prepare(args.node, args.release, args.python, args.node_binary, args.openclaw,
                             args.config, args.state, args.environment, mac_launch_daemon=args.mac_launch_daemon,
                             standby_profile=args.standby_profile)
    except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
        print("Standalone preflight failed. Check the pinned release, private paths and local OpenClaw config.",
              file=sys.stderr)
        return 1
    sys.stdout.buffer.write(definition)
    return 0


if __name__ == "__main__":
    sys.exit(main())

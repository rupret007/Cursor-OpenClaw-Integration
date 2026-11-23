"""Run one pinned OpenClaw standby gateway with a brokered Telegram channel."""

import argparse
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread

# The release inventory pins this directory, including under Python -I.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from standby import open_private, read_private_json
from standby_broker import Client
from standby_channel import Channel
from standby_host import GatewayProcess, Supervisor
from standby_telegram import positive_id


def profile_from(path):
    profile = read_private_json(path)
    if (not isinstance(profile, dict) or profile.keys() != {"hostname", "port", "cert", "key", "ca", "bot_id"}
            or not isinstance(profile["hostname"], str) or not profile["hostname"]
            or type(profile["port"]) is not int or not 0 < profile["port"] < 65536
            or not positive_id(profile["bot_id"])):
        raise ValueError("Invalid standby broker profile")
    for name in ("cert", "key", "ca"):
        if not isinstance(profile[name], str) or not Path(profile[name]).is_absolute():
            raise ValueError("Use absolute standby certificate paths")
    key = Path(profile["key"])
    with open_private(key.parent, key.name, create=False):
        pass
    return profile


def template_from(path):
    template = read_private_json(path)
    gateway = template["gateway"]
    telegram = template["channels"]["telegram"]
    if (gateway.get("mode") != "local" or gateway.get("bind") != "loopback"
            or type(gateway.get("port")) is not int or not 0 < gateway["port"] < 65536):
        raise ValueError("Use a pinned loopback gateway port")
    if (set(template["channels"]) != {"telegram"} or telegram.get("enabled") is not True
            or telegram.get("dmPolicy") != "allowlist" or telegram.get("groupPolicy") != "disabled"
            or {"botToken", "tokenFile", "accounts", "proxy", "apiRoot", "webhookUrl", "webhookSecret"} & telegram.keys()):
        raise ValueError("Use a token-free, private standby Telegram template")
    owners = telegram.get("allowFrom")
    approvers = telegram.get("execApprovals", {}).get("approvers")
    if (not isinstance(owners, list) or not isinstance(approvers, list) or len(owners) != 1
            or owners != approvers or not positive_id(owners[0])):
        raise ValueError("Use the same single numeric owner and approver")
    return template, owners[0]


def gateway_env(environment_file):
    settings = read_private_json(environment_file)
    if (not isinstance(settings, dict) or settings.keys() - {"OPENCLAW_LITELLM_API_KEY"}
            or any(not isinstance(value, str) or not value or len(value) > 4096
                   or any(c.isspace() for c in value) for value in settings.values())):
        raise ValueError("Standby private environment may contain only the local model credential")
    inherited = ("HOME", "PATH", "LANG", "TZ", "TMPDIR", "XDG_CONFIG_HOME", "OPENCLAW_STATE_DIR", "FO_LOCAL_NODE")
    return {**{key: os.environ[key] for key in inherited if key in os.environ}, **settings}


def write_gateway_config(path, template, port, token):
    telegram = {"apiRoot": f"http://127.0.0.1:{port}", "botToken": token, "replyToMode": "all"}
    messages = template.get("messages", {})
    inbound = messages.get("inbound", {})
    # The broker delivers one update at a time and waits for its final reply.
    by_channel = {**inbound.get("byChannel", {}), "telegram": 0}
    selected = {
        **template,
        "channels": {"telegram": {**template["channels"]["telegram"], **telegram}},
        "messages": {**messages, "inbound": {**inbound, "byChannel": by_channel}},
    }
    with path.open("x", encoding="utf-8") as handle:
        json.dump(selected, handle)


class BrokeredGateway:
    def __init__(self, broker, epoch, owner, bot_id, executable, template, environment, directory):
        token = f"{bot_id}:{secrets.token_urlsafe(32)}"
        with ExitStack() as opening:
            scratch = Path(opening.enter_context(TemporaryDirectory(prefix=".broker-gateway-", dir=directory)))
            bridge = opening.enter_context(Channel(broker, epoch, owner, bot_id, token))
            thread = Thread(target=bridge.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            thread.start()

            def stop_bridge():
                bridge.shutdown()
                thread.join(timeout=2)

            opening.callback(stop_bridge)
            config = scratch / "config.json"
            write_gateway_config(config, template, bridge.server_port, token)
            child_env = {**environment, "OPENCLAW_CONFIG_PATH": str(config), "FO_STANDBY_TELEGRAM": "1",
                         "FO_STANDBY_EPOCH": str(epoch), "FO_CAPTAIN_PYTHON": sys.executable}
            self.gateway = GatewayProcess(executable, child_env, template["gateway"]["port"])
            self.resources = opening.pop_all()

    def ready(self):
        return self.gateway.ready()

    def exited(self):
        return self.gateway.exited()

    def stop(self):
        try:
            self.gateway.stop()
        finally:
            self.resources.close()


def run(executable, template_path, environment_path, profile_path):
    template, owner = template_from(template_path)
    profile = profile_from(profile_path)
    environment = gateway_env(environment_path)
    state = Path(environment["OPENCLAW_STATE_DIR"])
    info = state.lstat()
    if not state.is_absolute() or not state.is_dir() or state.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Standby needs a private gateway state directory")
    client = Client(profile["hostname"], profile["port"], cert=profile["cert"], key=profile["key"], ca=profile["ca"])
    stop = Event()

    def shutdown(_number, _frame):
        stop.set()

    def start(epoch):
        return BrokeredGateway(client, epoch, owner, profile["bot_id"], executable, template, environment, state)

    with open_private(state, "standby-supervisor.lock") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(number, shutdown)
        Supervisor(client, start).run(stop, lambda status: print("Captain standby " + status, flush=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("executable", "template", "environment", "profile"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        run(args.executable, args.template, args.environment, args.profile)
    except (KeyError, OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError):
        print("Captain standby launcher could not proceed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

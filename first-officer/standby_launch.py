"""Run one pinned OpenClaw standby gateway with a brokered Telegram channel."""

import argparse
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import re
import runpy
import secrets
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time

# The release inventory pins this directory, including under Python -I.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lanes import queue
from standby import PRIMARY, STANDBY, open_private, private_directory, read_private_json
from standby_broker import BrokerUnavailable, Client
from standby_channel import Channel
from standby_host import PRIMARY_STARTUP_SECONDS, STARTUP_SECONDS, GatewayProcess, Supervisor
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
            admit_project = None
            if environment.get("FO_LOCAL_NODE") == "nucbox":
                source = Path(__file__).resolve().parents[1] / "captain-state.py"
                if not source.is_file():
                    source = Path(__file__).resolve().with_name("segment1") / "captain-state.py"
                admission = runpy.run_path(str(source))["brokered_project_submit"]
                home = Path(environment["HOME"])
                admit_project = lambda repository, issue: admission(home, queue, broker, epoch, repository, issue)
            bridge = opening.enter_context(Channel(broker, epoch, owner, bot_id, token, admit_project=admit_project))
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


def gateway_state(environment):
    state = Path(environment["OPENCLAW_STATE_DIR"])
    if not state.is_absolute():
        raise ValueError("Standby needs an absolute gateway state directory")
    private_directory(state)
    scratch = state / "standby-worker-cache"
    scratch.mkdir(mode=0o700, exist_ok=True)
    private_directory(scratch)
    return state, {**environment, "TMPDIR": str(scratch)}


def run(executable, template_path, environment_path, profile_path):
    template, owner = template_from(template_path)
    profile = profile_from(profile_path)
    state, environment = gateway_state(gateway_env(environment_path))
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
        startup = PRIMARY_STARTUP_SECONDS if environment.get("FO_LOCAL_NODE") == PRIMARY else STARTUP_SECONDS
        Supervisor(client, start, startup_seconds=startup).run(
            stop, lambda status: print("Captain standby " + status, flush=True))


def prewarm_template(template, release):
    # Admit only the reviewed Captain plugins and the Telegram channel adapter.
    plugins = template["plugins"]
    expected = {str(release / "first-officer-exec-guard.mjs"),
                str(release / "round6/runtime/first-officer-conductor.mjs")}
    paths = plugins["load"]["paths"]
    allowed = {"first-officer-conductor", "first-officer-exec-guard", "telegram"}
    if (not isinstance(paths, list) or len(paths) != len(expected) or set(paths) != expected
            or set(plugins["allow"]) != allowed or set(plugins["entries"]) != allowed):
        raise ValueError("Prewarm requires the pinned Captain plugins")
    models = template["models"]["providers"]
    local = "litellm/qwen-local-fast"
    selected = template["agents"]["defaults"]["model"]
    provider = models.get("litellm", {})
    if (set(models) != {"litellm"} or selected.get("primary") != local or selected.get("fallbacks")
            or re.fullmatch(r"http://(?:127\.0\.0\.1|localhost|\[::1\]):[1-9][0-9]{0,4}/v1/?",
                            provider.get("baseUrl", "")) is None
            or [entry.get("id") for entry in provider.get("models", [])] != ["qwen-local-fast"]):
        raise ValueError("Prewarm requires the single local Qwen route")
    return {
        "gateway": {"mode": "local", "bind": "loopback", "port": template["gateway"]["port"],
                    "auth": {"mode": "token", "token": secrets.token_urlsafe(32)}},
        "plugins": {"allow": sorted(allowed), "load": {"paths": paths}, "entries": {
            "telegram": {"enabled": True},
            "first-officer-exec-guard": {"enabled": True},
            "first-officer-conductor": {
                "enabled": True, "hooks": {"allowConversationAccess": True},
                "llm": {"allowModelOverride": True, "allowedModels": [local], "allowedCompletionModels": [local]},
            },
        }},
        "agents": {"defaults": {"model": {"primary": local}}},
        "models": {"providers": models},
        "channels": template["channels"],
    }


class IdlePrewarmRelay:
    def next_telegram_message(self, _epoch):
        return None


def prewarm(executable, template_path, environment_path):
    template, _ = template_from(template_path)
    template = prewarm_template(template, Path(__file__).resolve().parents[4])
    state, environment = gateway_state(gateway_env(environment_path))
    with open_private(state, "standby-supervisor.lock") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with TemporaryDirectory(prefix=".standby-prewarm-", dir=state) as directory:
            scratch = Path(directory)
            env = {**environment, "HOME": str(scratch), "XDG_CONFIG_HOME": str(scratch / ".config"),
                   "OPENCLAW_STATE_DIR": str(scratch)}
            gateway = BrokeredGateway(IdlePrewarmRelay(), 1, 101, 234567, executable, template, env, scratch)
            started = time.monotonic()
            try:
                while time.monotonic() - started < 60:
                    if gateway.exited():
                        raise ValueError("The isolated prewarm gateway exited")
                    if gateway.ready():
                        return time.monotonic() - started
                    time.sleep(0.25)
                raise TimeoutError("The isolated prewarm gateway did not reach readiness")
            finally:
                gateway.stop()


def prewarm_main(argv):
    parser = argparse.ArgumentParser(description="Warm the pinned gateway with isolated state and an idle local relay.")
    for name in ("executable", "template", "environment"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        elapsed = prewarm(args.executable, args.template, args.environment)
    except (OSError, KeyError, ValueError, TypeError, RecursionError, subprocess.SubprocessError):
        print("Captain prewarm failed. Check the stopped supervisor, private paths, and pinned local model config.",
              file=sys.stderr)
        return 1
    print(f"Isolated Captain prewarm reached readiness in {elapsed:.2f} seconds.")
    return 0


def verify_broker(profile_path, holder, epoch, timeout):
    profile = profile_from(profile_path)
    broker = Client(profile["hostname"], profile["port"], cert=profile["cert"], key=profile["key"], ca=profile["ca"])
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            status = broker.readiness(timeout=min(remaining, 3))
        except BrokerUnavailable:
            pass
        else:
            if status["bot"] != profile["bot_id"]:
                raise ValueError("Broker bot identity did not match the profile")
            live_holder, live_epoch = status["holder"], status["epoch"]
            if live_holder is not None and (live_holder, live_epoch) != (holder, epoch):
                raise ValueError("A different holder or epoch is already active")
            if live_holder is not None and status["gatewayRenewed"] and status["telegramPolling"]:
                return
        time.sleep(max(0, min(1, deadline - time.monotonic())))
    raise TimeoutError("Broker did not report a renewed gateway and recent Telegram polling")


def verify_broker_main(argv):
    parser = argparse.ArgumentParser(description="Verify a Captain handoff without claiming or renewing a lease.")
    parser.add_argument("profile", type=Path)
    parser.add_argument("--holder", choices=(PRIMARY, STANDBY), required=True)
    parser.add_argument("--epoch", type=int, required=True)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)
    if not positive_id(args.epoch) or not 1 <= args.timeout <= 180:
        parser.error("Use a positive epoch and a timeout between 1 and 180 seconds")
    try:
        verify_broker(args.profile, args.holder, args.epoch, args.timeout)
    except TimeoutError:
        print("Broker readiness timed out before the expected renewal and polling evidence.", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, RecursionError):
        print("Broker readiness failed. Check the private profile, bot identity, holder and epoch.", file=sys.stderr)
        return 1
    print(f"Captain handoff verified for {args.holder} at epoch {args.epoch}.")
    return 0


def main():
    if sys.argv[1:2] == ["prewarm"]:
        return prewarm_main(sys.argv[2:])
    if sys.argv[1:2] == ["verify-broker"]:
        return verify_broker_main(sys.argv[2:])
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

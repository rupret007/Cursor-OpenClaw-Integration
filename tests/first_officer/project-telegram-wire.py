
"""Drive the packaged Captain through a loopback Telegram Bot API fixture."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from queue import Empty, Queue
import runpy
import secrets
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
from urllib.parse import parse_qs, urlsplit


def write(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value))
    temp.replace(path)


class BotApi(BaseHTTPRequestHandler):
    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        path = urlsplit(self.path)
        method = path.path.rsplit("/", 1)[-1]
        wire = self.server
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if raw and "application/json" in self.headers.get("Content-Type", ""):
            data = json.loads(raw)
        else:
            data = {key: items[-1] for key, items in parse_qs(raw.decode() if raw else path.query).items()}
        if path.path == "/v1/chat/completions":
            wire.model_calls += 1
            answer = "A branch names a line of development."
            result = {"id": "fixture-model", "object": "chat.completion", "created": int(time.time()),
                      "model": "qwen-local-fast", "choices": [
                          {"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"},
                      ], "usage": {"prompt_tokens": 4, "completion_tokens": 7, "total_tokens": 11}}
            if data.get("stream"):
                chunk = {"id": result["id"], "object": "chat.completion.chunk", "created": result["created"],
                         "model": result["model"], "choices": [
                             {"index": 0, "delta": {"role": "assistant", "content": answer}, "finish_reason": "stop"},
                         ]}
                return self.send_bytes(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode(), "text/event-stream")
            return self.send_bytes(json.dumps(result).encode())
        result = True
        if method == "getMe":
            result = wire.bot
        elif method == "getWebhookInfo":
            result = {"url": "", "has_custom_certificate": False, "pending_update_count": 0}
        elif method == "getUpdates":
            wire.polling.set()
            try:
                result = [wire.updates.get(timeout=.25)]
            except Empty:
                result = []
        elif method == "sendChatAction":
            wire.typing.append(time.monotonic())
        elif method == "sendMessage":
            text = data.get("text", "")
            wire.replies.append((time.monotonic(), text))
            if wire.drop_project_final and "Handoff recorded" in text:
                wire.drop_project_final = False
                self.close_connection = True
                return
            result = {"message_id": 900 + len(wire.replies), "date": int(time.time()),
                      "chat": wire.chat, "from": wire.bot, "text": data.get("text", "")}
        self.send_bytes(json.dumps({"ok": True, "result": result}).encode())

    def send_bytes(self, data, media="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


@contextmanager
def gateway(node, cli, root, config, wire, extra_env=None):
    wire.polling.clear()
    env = {"HOME": str(root), "PATH": f"{node.parent}:/usr/bin:/bin", "LANG": "C.UTF-8",
           "FO_LOCAL_NODE": "nucbox", "OPENCLAW_CONFIG_PATH": str(config),
           "OPENCLAW_STATE_DIR": str(root / "gateway-state"),
           "XDG_CONFIG_HOME": str(root / ".config")}
    env.update(extra_env or {})
    with (root / "gateway.log").open("ab") as log:
        process = subprocess.Popen([str(node), str(cli), "gateway", "run"], env=env, stdout=log, stderr=log)
        try:
            assert wire.polling.wait(35) and process.poll() is None, "The isolated gateway could not reach the fixture"
            yield
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def enqueue(wire, update_id, message_id, text):
    wire.updates.put({"update_id": update_id, "message": {
        "message_id": message_id, "chat": wire.chat,
        "from": {"id": wire.chat["id"], "is_bot": False, "first_name": "Fixture"},
        "text": text, "date": int(time.time()),
    }})


def exercise(wire, label, number, text, expected, deadline):
    prior, typed = len(wire.replies), len(wire.typing)
    started = time.monotonic()
    enqueue(wire, number, number, text)
    while len(wire.replies) < prior + 2 and time.monotonic() - started < deadline:
        time.sleep(.025)
    pair = wire.replies[prior:]
    if len(pair) != 2 or pair[0][1] != "On it." or expected.casefold() not in pair[1][1].casefold():
        kinds = ("already has a receipt", "state or evidence is unavailable", "authority epoch changed",
                 "scope", "required CI", "review the candidate", "On it.", "verified",
                 "approve the exact issue", "active coding job", "project admission result", "Hatchet refused")
        found = [kind for kind in kinds if any(kind.casefold() in item[1].casefold() for item in pair)]
        print(f"{label} reply mismatch. Replies {len(pair)}. Model calls {wire.model_calls}. "
              f"Known reply labels {found}.", flush=True)
        raise AssertionError("The Telegram reply did not match the fixture")
    ack, final = (reply[0] - started for reply in pair)
    typed_at = wire.typing[typed] - started if len(wire.typing) > typed else None
    timing = f"{typed_at:.2f}s" if typed_at is not None else "missing"
    print(f"{label} typing {timing}, acknowledgment {ack:.2f}s, final {final:.2f}s.", flush=True)
    return typed_at is not None and typed_at < 2 and ack < 2 and final < deadline


def configure(wire, root, release):
    wire.daemon_threads = True
    wire.updates, wire.polling, wire.replies, wire.typing = Queue(), Event(), [], []
    wire.model_calls = 0
    wire.drop_project_final = False
    owner = 123456
    wire.bot = {"id": 234567, "is_bot": True, "first_name": "Fixture", "username": "project_rehearsal_bot"}
    wire.chat = {"id": owner, "type": "private", "first_name": "Fixture"}
    endpoint = f"http://127.0.0.1:{wire.server_port}"
    config = root / "config.json"
    with ThreadingHTTPServer(("127.0.0.1", 0), BotApi) as port_probe:
        port = port_probe.server_port
    alias = "litellm/qwen-local-fast"
    write(config, {
        "gateway": {"mode": "local", "bind": "loopback", "port": port,
                    "auth": {"mode": "token", "token": secrets.token_urlsafe(32)}},
        "plugins": {"allow": ["first-officer-conductor", "first-officer-exec-guard", "telegram"],
                    "load": {"paths": [str(release / "first-officer-exec-guard.mjs"),
                                       str(release / "round6/runtime/first-officer-conductor.mjs")]},
                    "entries": {"telegram": {"enabled": True}, "first-officer-exec-guard": {"enabled": True},
                                "first-officer-conductor": {"enabled": True, "hooks": {"allowConversationAccess": True},
                                            "llm": {"allowModelOverride": True, "allowedModels": [alias],
                                                    "allowedCompletionModels": [alias]}}}},
        "agents": {"defaults": {"model": {"primary": alias}}},
        "models": {"providers": {"litellm": {"baseUrl": endpoint + "/v1", "apiKey": "fixture-only",
                                            "api": "openai-completions",
                                            "models": [{"id": "qwen-local-fast", "name": "Fixture"}]}}},
        "channels": {"telegram": {"enabled": True, "dmPolicy": "allowlist", "allowFrom": [owner],
                                  "groupPolicy": "disabled", "botToken": "234567:" + secrets.token_urlsafe(32),
                                  "execApprovals": {"approvers": [owner]}, "apiRoot": endpoint}},
    })
    return config


def main():
    release, node, cli = (Path(arg).resolve(strict=True) for arg in sys.argv[1:])
    links = json.loads(subprocess.check_output(["/usr/sbin/ip", "-j", "link", "show"], timeout=4))
    interfaces = {row["ifname"] for row in links}
    if interfaces != {"lo"}:
        raise SystemExit("Use a loopback-only network namespace for this fixture.")
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="fo-project-telegram-") as directory, \
            ThreadingHTTPServer(("127.0.0.1", 0), BotApi) as wire:
        root = Path(directory)
        authority = root / ".local/state/openclaw-captain"
        (authority / "reviews").mkdir(parents=True, mode=0o700)
        gate = root / "gate"
        gate.mkdir()
        (gate / "fullgate.lock").touch()
        write(authority / "lease.json", {"node": "nucbox", "epoch": 3, "active": True})
        write(authority / "gate.json", {"directory": str(gate), "lock": "fullgate.lock", "hold": "HOLD"})
        config = configure(wire, root, release)
        thread = Thread(target=wire.serve_forever, daemon=True)
        thread.start()
        checks = []
        try:
            with gateway(node, cli, root, config, wire):
                checks.append(exercise(wire, "Ungrantable coding", 100, "captain this: advance example/integration #40",
                                       "approve the exact issue", 30))
                checks.append(exercise(wire, "Simple reply", 101, "Define a Git branch", "line of development", 15))
                checks.append(exercise(wire, "Live gate status", 102, "gate status", "Gate is idle", 30))
            request = {"repository": "example/integration", "issue": 40, "head": "a" * 40, "epoch": 3}
            queue = runpy.run_path(str(release / "round6/runtime/segment1/queue-runtime/lanes/queue.py"))
            write(authority / "project-grant.json", request)
            write(authority / "reviews/state.json", {"grant": None, "lastControl": 0, "jobs": [], "projectJobs": [{
                "request": {**request, "lane": queue["project_lane"](**request)},
                "state": "reserved", "worker": "local-qwen", "started": int(time.time()),
            }]})
            with gateway(node, cli, root, config, wire):
                checks.append(exercise(wire, "Restarted coding request", 103,
                                       "captain this: advance example/integration #40", "already has a receipt", 30))
                wire.drop_project_final = True
                checks.append(exercise(wire, "Lost handoff receipt", 104, "handoff", "Handoff recorded", 30))
                ledger = authority / "reviews/state.json"
                until = time.monotonic() + 5
                while time.monotonic() < until:
                    state = json.loads(ledger.read_text())
                    if any(row["state"] == "unverified" for row in state.get("projectTelegram", [])):
                        break
                    time.sleep(.05)
                else:
                    raise AssertionError("The lost response was not preserved as uncertain")
                sent = len(wire.replies)
            with gateway(node, cli, root, config, wire):
                enqueue(wire, 105, 104, "handoff")
                checks.append(exercise(wire, "Uncertain delivery decision", 106, "what needs me?",
                                       "1 Captain Telegram delivery is unconfirmed", 30))
                assert len(wire.replies) == sent + 2, "Restart must not repeat the uncertain handoff reply"
            assert wire.model_calls == 1, "Only the simple question should reach the model fixture"
            assert all(checks), "One or more Telegram fixture latency targets were missed"
        finally:
            wire.shutdown()
            thread.join()


if __name__ == "__main__":
    main()


"""Drive only a synthetic update through the verified test bot's outbound API."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
from threading import Event, Lock
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Relay(BaseHTTPRequestHandler):
    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        url = urlsplit(self.path)
        request_path, _, method = url.path.rpartition("/")
        if not hmac.compare_digest(request_path, self.server.prefix):
            self.send_error(403)
            return
        size = int(self.headers.get("Content-Length", 0))
        if size > 8192:
            self.send_error(413)
            return
        payload = self.rfile.read(size)
        data = (json.loads(payload) if payload and "application/json" in self.headers.get("Content-Type", "")
                else {key: vals[-1] for key, vals in parse_qs(payload.decode() if payload else url.query).items()})
        owner = self.server.owner
        result = True
        if method == "getMe":
            result = self.server.bot
        elif method == "getWebhookInfo":
            result = {"url": "", "has_custom_certificate": False, "pending_update_count": 0}
        elif method == "getUpdates":
            self.server.armed.wait(timeout=.4)
            with self.server.lock:
                if self.server.started is None and (self.server.root / "exercise-scope").is_file():
                    self.server.started = time.monotonic()
                    result = [{"update_id": self.server.update_id, "message": {
                        "message_id": self.server.update_id, "date": int(time.time()),
                        "chat": {"id": owner, "type": "private", "first_name": "Test"},
                        "from": {"id": owner, "is_bot": False, "first_name": "Test"},
                        "text": "captain this: advance rupret007/cursor-openclaw-integration #40",
                    }}]
                    self.server.armed.set()
                else:
                    result = []
            if not result:
                time.sleep(.3)
        elif method in ("sendChatAction", "sendMessage"):
            if str(data.get("chat_id")) != str(owner) or self.server.started is None:
                self.send_error(403)
                return
            message = str(data.get("text") or "")
            if method == "sendChatAction" and data.get("action") == "typing":
                phase = "typing"
            elif method == "sendMessage" and message == "On it.":
                phase = "ack"
            elif method == "sendMessage" and "approve the exact issue, base commit, and authority epoch" in message.lower():
                phase = "final"
            else:
                self.send_error(403)
                return
            try:
                result = self.server.api(method, data)
            except Exception:
                self.send_error(502)
                return
            with self.server.lock, (self.server.root / "acceptance-timing.jsonl").open("a") as log:
                log.write(json.dumps({"case": "scope", "phase": phase,
                                      "ms": round((time.monotonic() - self.server.started) * 1000)}) + "\n")
        elif method not in {"setMyCommands", "deleteMyCommands", "deleteWebhook"}:
            self.send_error(403)
            return
        body = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


def main():
    os.umask(0o077)
    root = Path(__file__).resolve().parent
    config = json.loads((root / "acceptance-config.json").read_bytes())["channels"]["telegram"]
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
    token = config["botToken"]
    owner = int(config["allowFrom"][0])
    prefix = "/bot" + token

    def api(method, fields=None):
        request = Request("https://api.telegram.org" + prefix + "/" + method,
                          data=None if fields is None else json.dumps(fields).encode(),
                          headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=6) as response:
            row = json.loads(response.read(16384))
        if row.get("ok") is not True:
            raise ValueError("The test bot rejected the request")
        return row["result"]

    bot = api("getMe")
    if bot.get("username", "").casefold() != "bobtestesbot":
        raise ValueError("The expected test bot was not verified")
    with ThreadingHTTPServer(("127.0.0.1", 41635), Relay) as server:
        server.daemon_threads = True
        server.root, server.bot, server.owner, server.prefix, server.api = root, bot, owner, prefix, api
        server.armed, server.lock, server.started = Event(), Lock(), None
        server.update_id = 312_001
        print("Private test-bot timing relay ready.", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("The verified test-bot relay could not start.") from None

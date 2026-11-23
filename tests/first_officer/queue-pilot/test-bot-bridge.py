
"""Forward three fixed rehearsal phases only to the verified BobTestesBot pairing."""

import argparse
import json
import os
from pathlib import Path
import re
import socketserver
import stat
from threading import Lock
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


PHASES = {
    "typing": ("sendChatAction", {"action": "typing"}),
    "ack": ("sendMessage", {"text": "On it."}),
    "admitted": ("sendMessage", {"text": "Captain rehearsal · Hatchet accepted one isolated coding job."}),
}


def private_info(path, kind):
    info = path.lstat()
    return kind(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077


def run(socket_path, credential):
    os.umask(0o077)
    if not private_info(socket_path.parent, stat.S_ISDIR) or not private_info(credential, stat.S_ISREG):
        raise ValueError("The test pairing and socket must be private")
    with credential.open("rb") as file:
        raw = file.read(4097)
    if len(raw) > 4096:
        raise ValueError("The test pairing exceeded its limit")
    settings = json.loads(raw)
    token, owner = settings["token"], str(settings["owner"])
    if not re.fullmatch(r"[0-9]{6,12}:[A-Za-z0-9_-]{30,100}", token) or not re.fullmatch(r"[1-9][0-9]{0,15}", owner):
        raise ValueError("The test pairing is invalid")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    base = "https://api.telegram.org/bot" + token + "/"

    def call(method, fields=None):
        request = Request(base + method, data=json.dumps(fields).encode() if fields else None,
                          headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=4) as response:
            payload = response.read(16385)
        if len(payload) > 16384:
            raise ValueError("The response exceeded its limit")
        parsed = json.loads(payload)
        if parsed.get("ok") is not True:
            raise ValueError("The test Bot API call failed")
        return parsed["result"]

    identity = call("getMe")
    if identity.get("is_bot") is not True or identity.get("username", "").casefold() != "bobtestesbot":
        raise ValueError("The paired account is not BobTestesBot")
    lock = Lock()
    attempted = set()

    class Bridge(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            try:
                phase = self.rfile.readline(24).decode().strip()
                with lock:
                    if phase not in PHASES or phase in attempted:
                        raise ValueError("The phase is unavailable")
                    attempted.add(phase)
                method, fields = PHASES[phase]
                result = call(method, {"chat_id": owner, **fields})
                if method == "sendChatAction":
                    confirmed = result is True
                else:
                    confirmed = (str(result.get("chat", {}).get("id")) == owner and
                                 type(result.get("message_id")) is int and result["message_id"] > 0)
                if confirmed:
                    print("Confirmed test-bot phase:", phase, flush=True)
                self.wfile.write(b"ok\n" if confirmed else b"unverified\n")
            except Exception:
                try:
                    self.wfile.write(b"unverified\n")
                except OSError:
                    pass

    with socketserver.ThreadingUnixStreamServer(str(socket_path), Bridge) as bridge:
        bridge.daemon_threads = True
        print("Verified BobTestesBot outbound bridge ready.", flush=True)
        bridge.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket", type=Path)
    parser.add_argument("credential", type=Path)
    args = parser.parse_args()
    try:
        run(args.socket, args.credential)
    except Exception:
        raise SystemExit("The verified test-bot bridge could not start.") from None

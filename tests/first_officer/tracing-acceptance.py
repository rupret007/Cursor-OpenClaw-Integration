#!/usr/bin/env python3
"""Prove one OpenLLMetry span arrives at a loopback Phoenix OTLP collector."""

import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import threading


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fo_trace", ROOT / "first-officer/tracing/deliver.py")
deliver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deliver)

HEAD = "3d350baac7636fe068d5be6299991f5e776259b5"
CANARY = "phoenix-canary-key"
GOOD = {"fo.lane": "review", "fo.head": HEAD, "fo.backend": "restate", "fo.outcome": "queued"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        self.server.seen.append({
            "path": self.path,
            "body": body,
            "headers": {key.lower(): value for key, value in self.headers.items()},
        })
        status = self.server.reply_status
        self.send_response(status)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):
        return


def serve(status):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.seen = []
    server.reply_status = status
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop(server):
    server.shutdown()
    server.server_close()


def reject():
    server = serve(500)
    try:
        try:
            deliver.deliver(f"http://127.0.0.1:{server.server_address[1]}", GOOD)
        except ValueError as error:
            if str(error) != "Phoenix did not accept the trace":
                raise
            if len(server.seen) != 1 or server.seen[0]["path"] != "/v1/traces":
                raise SystemExit("the rejected trace did not reach the collector")
            return
        raise SystemExit("a rejected trace was treated as delivered")
    finally:
        stop(server)


def main():
    if "--reject" in sys.argv:
        reject()
        print("Phoenix refusal was not recorded as delivery.")
        return 0
    probe = subprocess.run([sys.executable, __file__, "--reject"], check=False)
    if probe.returncode != 0:
        raise SystemExit(probe.returncode)

    server = serve(200)
    os.environ["TRACELOOP_BASE_URL"] = "http://127.0.0.1:9"
    os.environ["TRACELOOP_API_KEY"] = CANARY
    os.environ["PHOENIX_API_KEY"] = CANARY
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
    os.environ["http_proxy"] = "http://127.0.0.1:9"
    try:
        try:
            deliver.deliver(f"http://127.0.0.1:{server.server_address[1]}",
                            {**GOOD, "prompt": "sk-live-secretvalue"})
        except ValueError as error:
            if "allowlist" not in str(error):
                raise
        else:
            raise SystemExit("a prompt attribute was exported")
        if server.seen:
            raise SystemExit("a rejected attribute reached the collector")
        deliver.deliver(f"http://127.0.0.1:{server.server_address[1]}", GOOD)
    finally:
        stop(server)

    if len(server.seen) != 1:
        raise SystemExit(f"expected one trace, saw {len(server.seen)}")
    request = server.seen[0]
    body = request["body"]
    if request["path"] != "/v1/traces":
        raise SystemExit("the trace did not use Phoenix's OTLP path")
    if b"first-officer.lane" not in body or b"review" not in body or HEAD.encode() not in body:
        raise SystemExit("the collector did not receive the lane span")
    if b"restate" not in body or b"queued" not in body:
        raise SystemExit("the collector did not receive the lane outcome")
    if CANARY.encode() in body or "sk-live" in body.decode("utf-8", "replace"):
        raise SystemExit("the trace carried a credential or a prompt")
    if "authorization" in request["headers"]:
        raise SystemExit("the trace carried an authorization header")
    print("Phoenix accepted one first-officer.lane trace at the loopback collector.")
    print("It carried lane review, the head, backend restate and outcome queued.")
    print("An ambient collector, proxy and API key were ignored, and a prompt was not exported.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

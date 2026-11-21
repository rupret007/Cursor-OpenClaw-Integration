#!/usr/bin/env python3
"""Publish the shared status file the fleet reader already accepts, and show it read-only.

The snapshot uses the fields `readFleetSnapshot` checks: version 1, a matching timestamp,
and a host object. It stays under 3,000 bytes, mode 0600, and contains no free text.
A loopback page can display it. The page accepts GET only and does not bind any other address.
"""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import os
from pathlib import Path
import re
import sys


NODES = ("mac-mini", "nucbox", "macbook-pro")
COUNTS = ("running", "active", "queued", "stalled")
MAX_BYTES = 3000
FRESH_S = 300
SECRET = re.compile(r"sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|\b(?:\d{1,3}\.){3}\d{1,3}\b")
_server = None


def _natural(value, limit):
    return type(value) is int and 0 <= value <= limit


def _load(value):
    return type(value) in (int, float) and not isinstance(value, bool) and 0 <= float(value) <= 1000


def _stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(evidence, now):
    """Build one snapshot. Unknown numbers are omitted rather than reported as zero."""
    if type(now) is not int or now <= 0:
        raise ValueError("The status clock is unusable")
    if not isinstance(evidence, dict):
        raise ValueError("The status evidence is unreadable")
    alerts = []
    if SECRET.search(json.dumps(evidence, default=str)):
        alerts.append("redacted")
    document = {"alerts": alerts, "generated_at": _stamp(now), "host": {}, "ts": now, "v": 1}
    heavy = evidence.get("heavy_jobs", None)
    if heavy is not None:
        if not _natural(heavy, 100):
            raise ValueError("The heavy-job count is unusable")
        document["host"]["heavy_jobs"] = heavy
    counts = evidence.get("lane_counts", {})
    if counts is None:
        counts = {}
    if not isinstance(counts, dict) or any(key not in COUNTS for key in counts):
        raise ValueError("The lane counts are unusable")
    clean_counts = {}
    for key in COUNTS:
        if key in counts:
            if not _natural(counts[key], 1000):
                raise ValueError("The lane counts are unusable")
            clean_counts[key] = counts[key]
    if clean_counts:
        document["lane_counts"] = clean_counts
    nodes = evidence.get("nodes", {})
    if nodes is None:
        nodes = {}
    if not isinstance(nodes, dict) or any(key not in NODES for key in nodes):
        raise ValueError("A status node is not one of the known machines")
    published = {}
    for name in NODES:
        sample = nodes.get(name)
        if sample is None:
            continue
        if not isinstance(sample, dict):
            raise ValueError("A status node is unreadable")
        age = sample.get("age_s", 0)
        if not _natural(age, 86_400):
            raise ValueError("A status node age is unusable")
        if age > FRESH_S:
            published[name] = {"missing": True, "stale": True}
            if "stale" not in alerts:
                alerts.append("stale")
            continue
        loads = [sample.get("load1"), sample.get("load5"), sample.get("load15")]
        jobs = [sample.get("codex_jobs"), sample.get("test_jobs")]
        if not all(_load(value) for value in loads) or not all(_natural(value, 1000) for value in jobs):
            published[name] = {"missing": True, "stale": True}
            if "stale" not in alerts:
                alerts.append("stale")
            continue
        published[name] = {
            "codex_jobs": jobs[0], "load1": loads[0], "load15": loads[2], "load5": loads[1],
            "test_jobs": jobs[1], "timestamp": _stamp(now - age),
        }
    if published:
        document["nodes"] = published
    document["alerts"] = sorted(set(alerts))
    encoded = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
    if len(encoded) > MAX_BYTES:
        document.pop("nodes", None)
        if "trimmed" not in document["alerts"]:
            document["alerts"] = sorted(set(document["alerts"]) | {"trimmed"})
        encoded = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
    if len(encoded) > MAX_BYTES or SECRET.search(encoded.decode()):
        raise ValueError("The status snapshot is not safe to publish")
    return document


def write_snapshot(path, document):
    target = Path(path)
    temporary = target.with_name(f".{target.name}.tmp")
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n"
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, target)
    os.chmod(target, 0o600)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path not in ("/", "/status.json"):
            self._send(404, "text/plain; charset=utf-8", b"Not found\n")
            return
        try:
            text = Path(self.server.snapshot).read_text(encoding="utf-8")
            document = json.loads(text)
            if document.get("v") != 1 or not isinstance(document.get("host"), dict) or SECRET.search(text):
                raise ValueError
        except (OSError, ValueError, json.JSONDecodeError):
            self._send(503, "text/plain; charset=utf-8", b"Status unavailable\n")
            return
        if self.path == "/status.json":
            self._send(200, "application/json", (text if text.endswith("\n") else text + "\n").encode())
            return
        body = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>First Officer status</title></head>"
                f"<body><h1>First Officer status</h1><pre>{html.escape(text)}</pre></body></html>\n")
        self._send(200, "text/html; charset=utf-8", body.encode())

    def do_POST(self):
        self._send(405, "text/plain; charset=utf-8", b"Read only\n")

    do_PUT = do_DELETE = do_POST

    def _send(self, status, content_type, payload):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        return


def serve(path, port=0):
    """Serve the snapshot on loopback. The caller stops the returned server."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("The status page port is unusable")
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    if server.server_address[0] != "127.0.0.1":
        server.server_close()
        raise ValueError("The status page must stay on loopback")
    server.snapshot = str(path)
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("evidence", type=Path)
    publish.add_argument("snapshot", type=Path)
    page = commands.add_parser("serve")
    page.add_argument("snapshot", type=Path)
    page.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        if args.command == "publish":
            document = build(json.loads(args.evidence.read_text(encoding="utf-8")),
                             int(datetime.now(timezone.utc).timestamp()))
            write_snapshot(args.snapshot, document)
            print(f"Published status {document['generated_at']} ({len(json.dumps(document))} bytes).")
            return 0
        server = serve(args.snapshot, args.port)
        print(f"Status page on http://127.0.0.1:{server.server_address[1]}/")
        server.serve_forever()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        message = str(error) if type(error) is ValueError else "The status page could not be published"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

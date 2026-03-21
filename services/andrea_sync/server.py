"""HTTP server for Andrea lockstep (local-first)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, Dict, Optional

from .adapters import alexa as alexa_adapt
from .adapters import telegram as tg_adapt
from .bus import handle_command
from .kill_switch import is_kill_switch_engaged, kill_switch_status
from .policy import digest_age_seconds, evaluate_skill_absence_claim, get_capability_digest
from .projector import project_task_dict
from .store import (
    connect,
    ensure_system_task,
    get_task_channel,
    list_tasks,
    load_events_for_task,
    migrate,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_db_path() -> Path:
    raw = os.environ.get("ANDREA_SYNC_DB")
    if raw:
        return Path(raw).expanduser()
    return _repo_root() / "data" / "andrea_sync.db"


class SyncServer:
    def __init__(self) -> None:
        self.db_path = default_db_path()
        self.conn = connect(self.db_path)
        migrate(self.conn)
        ensure_system_task(self.conn)
        self.lock = threading.Lock()
        self.queue: Queue[Callable[[], None]] = Queue()
        self._worker = threading.Thread(target=self._run_queue, daemon=True)
        self._worker.start()
        self.telegram_secret = os.environ.get("ANDREA_SYNC_TELEGRAM_SECRET", "")
        self.telegram_header_secret = os.environ.get(
            "ANDREA_SYNC_TELEGRAM_WEBHOOK_SECRET", ""
        )
        self.internal_token = os.environ.get("ANDREA_SYNC_INTERNAL_TOKEN", "")

    def _run_queue(self) -> None:
        while True:
            try:
                fn = self.queue.get(timeout=0.5)
            except Empty:
                continue
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                print(f"andrea_sync worker error: {e}", flush=True)
            finally:
                self.queue.task_done()

    def with_lock(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        with self.lock:
            return fn(self.conn)


def make_handler(server: SyncServer) -> type:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            if os.environ.get("ANDREA_SYNC_VERBOSE", "0") == "1":
                super().log_message(fmt, *args)

        def _auth_internal(self) -> bool:
            if not server.internal_token:
                return False
            auth = self.headers.get("Authorization") or ""
            return auth == f"Bearer {server.internal_token}"

        _ADMIN_COMMAND_TYPES = frozenset(
            {
                "PublishCapabilitySnapshot",
                "KillSwitchEngage",
                "KillSwitchRelease",
            }
        )

        def _commands_require_internal(self, body: Dict[str, Any]) -> bool:
            ct = str(body.get("command_type") or body.get("type") or "")
            return ct in self._ADMIN_COMMAND_TYPES

        def _send(self, code: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {}

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/v1/health":

                def health_body(c: sqlite3.Connection) -> bytes:
                    ks = kill_switch_status(c)
                    age = digest_age_seconds(c)
                    return json.dumps(
                        {
                            "ok": True,
                            "service": "andrea_sync",
                            "db": str(server.db_path),
                            "kill_switch": ks,
                            "capability_digest_age_seconds": age,
                        }
                    ).encode("utf-8")

                self._send(200, server.with_lock(health_body))
                return
            if path == "/v1/status":
                def status_body(c: sqlite3.Connection) -> bytes:
                    cap = get_capability_digest(c)
                    ks = kill_switch_status(c)
                    return json.dumps(
                        {
                            "ok": True,
                            "service": "andrea_sync",
                            "db": str(server.db_path),
                            "kill_switch": ks,
                            "capabilities": cap,
                        },
                        indent=2,
                    ).encode("utf-8")

                self._send(200, server.with_lock(status_body))
                return
            if path == "/v1/capabilities":

                def cap_body(c: sqlite3.Connection) -> bytes:
                    info = get_capability_digest(c)
                    return json.dumps(info, indent=2).encode("utf-8")

                self._send(200, server.with_lock(cap_body))
                return
            if path == "/v1/policy/skill-absence":
                qs = urllib.parse.parse_qs(parsed.query)
                sk = (qs.get("skill") or [""])[0].strip()
                if not sk:
                    self._send(400, b'{"error":"skill query param required"}')
                    return

                def pol(c: sqlite3.Connection) -> bytes:
                    raw_ttl = (qs.get("max_age_seconds") or [""])[0].strip()
                    try:
                        ttl = float(raw_ttl) if raw_ttl else 900.0
                    except ValueError:
                        ttl = 900.0
                    ev = evaluate_skill_absence_claim(c, sk, max_age_seconds=ttl)
                    return json.dumps(ev, indent=2).encode("utf-8")

                self._send(200, server.with_lock(pol))
                return
            if path.startswith("/v1/tasks/") and len(path) > len("/v1/tasks/"):
                tid = path.split("/v1/tasks/", 1)[1].split("?", 1)[0].strip()
                if not tid:
                    self._send(400, b'{"error":"missing task id"}')
                    return

                def one(c: sqlite3.Connection) -> bytes:
                    ch = get_task_channel(c, tid)
                    if not ch:
                        return json.dumps({"error": "not found"}).encode("utf-8")
                    proj = project_task_dict(c, tid, ch)
                    events = [
                        {
                            "seq": s,
                            "ts": t,
                            "event_type": et,
                            "payload": p,
                        }
                        for s, t, et, p in load_events_for_task(c, tid)
                    ]
                    return json.dumps(
                        {"task": proj, "events": events}, indent=2
                    ).encode("utf-8")

                payload = server.with_lock(one)
                try:
                    obj = json.loads(payload.decode("utf-8"))
                except json.JSONDecodeError:
                    self._send(500, b'{"error":"projection failed"}')
                    return
                if obj.get("error"):
                    self._send(404, payload)
                else:
                    self._send(200, payload)
                return
            if path == "/v1/tasks":
                raw_lim = (urllib.parse.parse_qs(parsed.query).get("limit") or ["50"])[0]
                try:
                    limit = int(raw_lim)
                except ValueError:
                    limit = 50
                limit = max(1, min(limit, 500))

                def lst(c: sqlite3.Connection) -> bytes:
                    rows = list_tasks(c, limit=limit)
                    return json.dumps({"tasks": rows}, indent=2).encode("utf-8")

                self._send(200, server.with_lock(lst))
                return
            self._send(404, b'{"error":"not found"}')

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/v1/commands":
                body = self._read_json()
                if self._commands_require_internal(body) and not self._auth_internal():
                    self._send(401, b'{"error":"unauthorized"}')
                    return

                def ks_block(c: sqlite3.Connection) -> bool:
                    return is_kill_switch_engaged(c)

                if server.with_lock(ks_block):
                    ct = str(body.get("command_type") or body.get("type") or "")
                    if ct != "KillSwitchRelease":
                        self._send(
                            503,
                            b'{"ok":false,"error":"kill_switch_engaged"}',
                        )
                        return

                def run(c: sqlite3.Connection) -> bytes:
                    return json.dumps(handle_command(c, body), indent=2).encode("utf-8")

                out = server.with_lock(run)
                try:
                    result = json.loads(out.decode("utf-8"))
                except json.JSONDecodeError:
                    self._send(500, out)
                    return
                if result.get("ok") is True:
                    self._send(200, out)
                else:
                    err = str(result.get("error") or "").lower()
                    if "kill_switch_engaged" in err:
                        code = 503
                    elif "unknown task" in err:
                        code = 404
                    else:
                        code = 400
                    self._send(code, out)
                return
            if path == "/v1/internal/events":
                if not self._auth_internal():
                    self._send(401, b'{"error":"unauthorized"}')
                    return
                if server.with_lock(is_kill_switch_engaged):
                    self._send(
                        503,
                        b'{"ok":false,"error":"kill_switch_engaged"}',
                    )
                    return
                body = self._read_json()
                task_id = body.get("task_id")
                et = body.get("event_type")
                payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
                if not task_id or not et:
                    self._send(400, b'{"error":"task_id and event_type required"}')
                    return
                from .schema import validate_event_type
                from .store import append_event, task_exists

                def append(c: sqlite3.Connection) -> bytes:
                    if not task_exists(c, str(task_id)):
                        return json.dumps({"ok": False, "error": "unknown task"}).encode(
                            "utf-8"
                        )
                    try:
                        ev = validate_event_type(str(et))
                    except ValueError as e:
                        return json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                    seq = append_event(c, str(task_id), ev, payload)
                    return json.dumps({"ok": True, "seq": seq}).encode("utf-8")

                raw = server.with_lock(append)
                try:
                    result = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._send(500, raw)
                    return
                if result.get("ok") is True:
                    self._send(200, raw)
                else:
                    err = str(result.get("error") or "").lower()
                    code = 404 if "unknown" in err else 400
                    self._send(code, raw)
                return
            if path == "/v1/telegram/webhook":
                if server.with_lock(is_kill_switch_engaged):
                    self._send(
                        503,
                        b'{"ok":false,"error":"kill_switch_engaged"}',
                    )
                    return
                q = urllib.parse.parse_qs(parsed.query)
                sec = (q.get("secret") or [""])[0]
                hdr = self.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
                if not tg_adapt.verify_telegram_webhook(
                    sec,
                    hdr,
                    query_configured=server.telegram_secret,
                    header_configured=server.telegram_header_secret,
                ):
                    self._send(403, b'{"error":"forbidden"}')
                    return
                update = self._read_json()

                def work() -> None:
                    cmd = tg_adapt.update_to_command(update)
                    if not cmd:
                        return

                    def run(c: sqlite3.Connection) -> None:
                        handle_command(c, cmd)

                    server.with_lock(run)

                server.queue.put(work)
                self._send(200, b'{"ok":true}')
                return
            if path == "/v1/alexa":
                if server.with_lock(is_kill_switch_engaged):
                    self._send(
                        503,
                        b'{"ok":false,"error":"kill_switch_engaged"}',
                    )
                    return
                body = self._read_json()
                cmd, resp = alexa_adapt.parse_alexa_body(body)

                def work() -> None:
                    if not cmd:
                        return

                    def run(c: sqlite3.Connection) -> None:
                        handle_command(c, cmd)

                    server.with_lock(run)

                server.queue.put(work)
                out = alexa_adapt.build_response_json(resp)
                self._send(200, out, content_type="application/json;charset=utf-8")
                return
            self._send(404, b'{"error":"not found"}')

    return Handler


def serve_forever(host: str = "127.0.0.1", port: Optional[int] = None) -> None:
    p = port or int(os.environ.get("ANDREA_SYNC_PORT", "8765"))
    srv_state = SyncServer()
    handler = make_handler(srv_state)
    httpd = ThreadingHTTPServer((host, p), handler)
    print(f"andrea_sync listening on http://{host}:{p} db={srv_state.db_path}", flush=True)
    httpd.serve_forever()


def main() -> None:
    serve_forever()


if __name__ == "__main__":
    main()

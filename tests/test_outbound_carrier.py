"""The production outbox may record only a carrier confirmation.

These tests drive SyncServer and outbound.deliver. They do not replace the
delivery tool or the send method. A loopback carrier is the adapter.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import stat
import tempfile
import threading
from unittest import mock

import pytest


class Carrier(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        self.server.seen.append(raw)
        if self.server.mode == "down":
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            request = {}
        delivered = self.server.mode == "confirm"
        payload = {
            "body": request.get("body"),
            "carrier": self.server.carrier_id,
            "delivered": delivered,
            "digest": request.get("digest"),
            "target": request.get("target"),
            "tool": request.get("tool"),
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        return


def serve(mode):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Carrier)
    server.mode = mode
    server.seen = []
    server.carrier_id = "carrier-" + "a1b2c3d4"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _server(db_path, monkeypatch):
    monkeypatch.setenv("ANDREA_SYNC_DB", str(db_path))
    monkeypatch.setenv("ANDREA_SYNC_TELEGRAM_NOTIFIER", "0")
    monkeypatch.setenv("ANDREA_SYNC_BACKGROUND_ENABLED", "0")
    monkeypatch.setenv("ANDREA_SYNC_TELEGRAM_WEBHOOK_AUTOFIX", "0")
    monkeypatch.setenv("ANDREA_SYNC_PUBLIC_BASE", "")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANDREA_OUTBOUND_CARRIER", raising=False)
    from services.andrea_sync.server import SyncServer

    monkeypatch.setattr(SyncServer, "_run_queue", lambda self: None)
    return SyncServer()


def _submit(server, external_id, text, message_id):
    from services.andrea_sync.bus import handle_command
    from services.andrea_sync.schema import CommandType

    return str(handle_command(
        server.conn,
        {
            "command_type": CommandType.SUBMIT_USER_MESSAGE.value,
            "channel": "telegram",
            "external_id": external_id,
            "payload": {
                "text": text,
                "chat_id": 9104,
                "message_id": message_id,
                "from_user": 77,
            },
        },
    )["task_id"])


def _draft(server, external_id, message_id):
    drafted = _submit(server, external_id, "Tell Candace hi from you", message_id)
    with mock.patch.object(
        server,
        "_resolve_messaging_capability",
        return_value={
            "skill_key": "bluebubbles",
            "label": "text messaging",
            "truth": {"status": "verified_available"},
        },
    ):
        server._handle_task_followups(drafted)
    pending = server._load_pending_outbound_draft(drafted)
    assert pending.get("target") == "Candace"
    assert pending.get("message") == "hi from you"
    return drafted, pending


def _confirm(server, external_id, message_id):
    from services.andrea_sync.projector import project_task_dict

    confirmed = _submit(server, external_id, "send it", message_id)
    server._handle_task_followups(confirmed)
    projection = project_task_dict(server.conn, confirmed, "telegram")
    assistant = projection["meta"]["assistant"]
    return confirmed, assistant["reason"], assistant["last_reply"]


def _stored(server):
    path = server.outbound_outbox
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _successes(server):
    return [row for row in _stored(server) if row.get("delivered") is True]


def test_unconfirmed_delivery_is_not_recorded(tmp_path, monkeypatch):
    """No carrier, a refusal, and an HTTP failure must not look like a send."""
    db = Path(tempfile.mkstemp(suffix=".db")[1])
    try:
        server = _server(db, monkeypatch)
        assert server.outbound_delivery_tool.__func__.__name__ == "_deliver_outbound"
        drafted, pending = _draft(server, "tg-carrier-none", 901)
        from services.andrea_sync.outbound import deliver

        with pytest.raises(ValueError, match="carrier"):
            deliver(pending, server.outbound_outbox)
        _confirmed, reason, reply = _confirm(server, "tg-carrier-none-send", 902)
        assert reason != "outbound_message_sent"
        assert "I sent it" not in reply
        assert server._load_pending_outbound_draft(drafted).get("digest") == pending["digest"]
        assert _successes(server) == []

        denied = serve("deny")
        try:
            monkeypatch.setenv(
                "ANDREA_OUTBOUND_CARRIER",
                f"http://127.0.0.1:{denied.server_address[1]}/deliver",
            )
            with pytest.raises(ValueError, match="carrier"):
                deliver(pending, server.outbound_outbox)
            assert denied.seen
            request = json.loads(denied.seen[0])
            assert "delivered" not in request
            assert request.get("carrier") != denied.carrier_id
            _confirmed, reason, reply = _confirm(server, "tg-carrier-deny-send", 903)
            assert reason != "outbound_message_sent"
            assert "I sent it" not in reply
            assert server._load_pending_outbound_draft(drafted)
            assert _successes(server) == []
        finally:
            denied.shutdown()

        down = serve("down")
        try:
            monkeypatch.setenv(
                "ANDREA_OUTBOUND_CARRIER",
                f"http://127.0.0.1:{down.server_address[1]}/deliver",
            )
            with pytest.raises(ValueError, match="carrier"):
                deliver(pending, server.outbound_outbox)
            assert down.seen
            assert _successes(server) == []
        finally:
            down.shutdown()
    finally:
        db.unlink(missing_ok=True)


def _bound_draft():
    from services.andrea_sync.outbound import bind_draft, new_nonce

    return bind_draft(
        target="Candace",
        body="hi from you",
        principal="principal-1",
        origin={"channel": "telegram", "chat_id": "9104", "message_id": "1"},
        expires_at=1_900_000_000,
        nonce=new_nonce(),
        skill_key="bluebubbles",
        label="text messaging",
        created_at=1.0,
    )


def test_confirmed_send_is_not_retried_after_local_receipt_failure(tmp_path, monkeypatch):
    """A confirmed carrier response that is not durably recorded must not be sent again.

    This is the offline repro: the carrier returns one exact receipt, local
    serialization of that receipt raises, the serializer is restored, and the
    same draft is delivered again. The carrier must not be called twice.
    """
    import services.andrea_sync.outbound as outbound

    carrier = serve("confirm")
    try:
        monkeypatch.setenv(
            "ANDREA_OUTBOUND_CARRIER",
            f"http://127.0.0.1:{carrier.server_address[1]}/deliver",
        )
        draft = _bound_draft()
        outbox = tmp_path / "outbox.jsonl"
        real_dumps = outbound.json.dumps

        def dumps(payload, *args, **kwargs):
            if (isinstance(payload, dict) and payload.get("source") == "tool"
                    and payload.get("delivered") is True):
                raise RuntimeError("local receipt serialization failed")
            return real_dumps(payload, *args, **kwargs)

        monkeypatch.setattr(outbound.json, "dumps", dumps)
        with pytest.raises(outbound.DeliveryUncertain, match="manual check"):
            outbound.deliver(draft, outbox)
        monkeypatch.setattr(outbound.json, "dumps", real_dumps)
        pending_file = outbox.with_name(outbox.name + ".pending")
        assert draft["digest"] in pending_file.read_text(encoding="utf-8")
        assert stat.S_IMODE(pending_file.stat().st_mode) == 0o600
        assert len(carrier.seen) == 1
        with pytest.raises(outbound.DeliveryUncertain, match="manual check"):
            outbound.deliver(draft, outbox)
        assert len(carrier.seen) == 1
        request = json.loads(carrier.seen[0])
        assert request["digest"] == draft["digest"]
        assert "delivered" not in request
    finally:
        carrier.shutdown()


def test_a_pending_send_is_recorded_from_a_digest_query_without_another_post(tmp_path, monkeypatch):
    """When a later query can prove the digest, record that receipt and do not POST again."""
    import services.andrea_sync.outbound as outbound

    carrier = serve("confirm")
    try:
        monkeypatch.setenv(
            "ANDREA_OUTBOUND_CARRIER",
            f"http://127.0.0.1:{carrier.server_address[1]}/deliver",
        )
        draft = _bound_draft()
        outbox = tmp_path / "outbox.jsonl"
        real_dumps = outbound.json.dumps

        def dumps(payload, *args, **kwargs):
            if (isinstance(payload, dict) and payload.get("source") == "tool"
                    and payload.get("delivered") is True):
                raise RuntimeError("local receipt serialization failed")
            return real_dumps(payload, *args, **kwargs)

        monkeypatch.setattr(outbound.json, "dumps", dumps)
        with pytest.raises(outbound.DeliveryUncertain):
            outbound.deliver(draft, outbox)
        monkeypatch.setattr(outbound.json, "dumps", real_dumps)
        request = json.loads(carrier.seen[0])
        receipt = {
            "body": request["body"],
            "carrier": carrier.carrier_id,
            "delivered": True,
            "digest": request["digest"],
            "source": "tool",
            "target": request["target"],
            "tool": request["tool"],
        }
        monkeypatch.setattr(outbound, "_query_carrier", lambda _draft: receipt)
        found = outbound.deliver(draft, outbox)
        assert found["digest"] == draft["digest"]
        assert found["carrier"] == carrier.carrier_id
        assert len(carrier.seen) == 1
        again = outbound.deliver(draft, outbox)
        assert again["digest"] == draft["digest"]
        assert len(carrier.seen) == 1
    finally:
        carrier.shutdown()


def test_the_server_surfaces_an_unrecorded_confirmation_for_manual_check(tmp_path, monkeypatch):
    """The production confirmation path must not call the carrier again or say the message was sent."""
    import services.andrea_sync.outbound as outbound

    db = Path(tempfile.mkstemp(suffix=".db")[1])
    carrier = serve("confirm")
    try:
        server = _server(db, monkeypatch)
        monkeypatch.setenv(
            "ANDREA_OUTBOUND_CARRIER",
            f"http://127.0.0.1:{carrier.server_address[1]}/deliver",
        )
        drafted, _pending = _draft(server, "tg-uncertain", 921)
        real_dumps = outbound.json.dumps

        def dumps(payload, *args, **kwargs):
            if (isinstance(payload, dict) and payload.get("source") == "tool"
                    and payload.get("delivered") is True):
                raise RuntimeError("local receipt serialization failed")
            return real_dumps(payload, *args, **kwargs)

        monkeypatch.setattr(outbound.json, "dumps", dumps)
        _confirmed, reason, reply = _confirm(server, "tg-uncertain-send", 922)
        assert reason != "outbound_message_sent"
        assert "I sent it" not in reply
        assert "manual check" in reply
        assert server._load_pending_outbound_draft(drafted)
        assert len(carrier.seen) == 1
        monkeypatch.setattr(outbound.json, "dumps", real_dumps)
        _again, reason, reply = _confirm(server, "tg-uncertain-again", 923)
        assert reason != "outbound_message_sent"
        assert "I sent it" not in reply
        assert "manual check" in reply
        assert len(carrier.seen) == 1
        assert server._load_pending_outbound_draft(drafted)
    finally:
        carrier.shutdown()
        db.unlink(missing_ok=True)


def test_exact_confirmation_records_the_carrier_result_once(tmp_path, monkeypatch):
    """The stored receipt is the carrier response, and a second confirm does not send again."""
    db = Path(tempfile.mkstemp(suffix=".db")[1])
    carrier = serve("confirm")
    try:
        server = _server(db, monkeypatch)
        monkeypatch.setenv(
            "ANDREA_OUTBOUND_CARRIER",
            f"http://127.0.0.1:{carrier.server_address[1]}/deliver",
        )
        drafted, pending = _draft(server, "tg-carrier-yes", 911)
        from services.andrea_sync.outbound import receipt_matches

        confirmed, reason, reply = _confirm(server, "tg-carrier-yes-send", 912)
        assert reason == "outbound_message_sent"
        assert "Candace" in reply
        assert "I sent it" in reply
        assert server._load_pending_outbound_draft(confirmed) in (None, {})
        assert not server._load_pending_outbound_draft(drafted)
        stored = _stored(server)
        assert len(stored) == 1
        assert stored[0]["carrier"] == carrier.carrier_id
        assert stored[0]["delivered"] is True
        assert receipt_matches(pending, stored[0])
        request = json.loads(carrier.seen[0])
        assert request["body"] == "hi from you"
        assert request["target"] == "Candace"
        assert request["digest"] == pending["digest"]
        assert "delivered" not in request
        assert request.get("carrier") != carrier.carrier_id
        again = server.outbound_delivery_tool(pending)
        assert again["carrier"] == carrier.carrier_id
        assert len(carrier.seen) == 1
        _confirm(server, "tg-carrier-yes-again", 913)
        assert _stored(server) == stored
        assert len(carrier.seen) == 1
    finally:
        carrier.shutdown()
        db.unlink(missing_ok=True)

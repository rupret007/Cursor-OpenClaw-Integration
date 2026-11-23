"""Private HTTPS transport for the Captain standby lease and effect fence."""

import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
import ssl
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
import time

if __name__ == "__main__":
    # Start with Python isolated mode and resolve peers only from the pinned release.
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from standby import EFFECT_KINDS, Grant, LeaseRejected, PRIMARY, STANDBY, StandbyAuthority, read_private_json
from standby_queue import ProjectBusy, ProjectQueue, ProjectScopeRejected, ReviewQueue, ReviewScopeRejected
from standby_telegram import BotAPI, POLL_TRANSPORT_SECONDS, TelegramInbox, TelegramPoll, reply_operation


MAX_REQUEST_BYTES = 32_768
MAX_RESPONSE_BYTES = 100_000
REQUEST_TIMEOUT = 3


class BrokerUnavailable(OSError):
    pass


class EffectUnconfirmed(BrokerUnavailable):
    pass


class Client:
    def __init__(self, hostname, port, *, cert, key, ca):
        self.hostname = hostname
        self.port = port
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.tls.minimum_version = ssl.TLSVersion.TLSv1_2
        self.tls.hostname_checks_common_name = False
        self.tls.load_verify_locations(cafile=ca)
        self.tls.load_cert_chain(cert, key)

    def request(self, path, fields, *, timeout=REQUEST_TIMEOUT):
        raw = json.dumps(fields, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("Request exceeds the broker limit")
        conn = http.client.HTTPSConnection(self.hostname, self.port, context=self.tls, timeout=timeout)
        try:
            conn.request("POST", path, raw, {"Content-Type": "application/json"})
            response = conn.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("Oversized response")
            status = response.status
            result = json.loads(body)
        except (OSError, http.client.HTTPException, ValueError, RecursionError):
            raise BrokerUnavailable("Captain authority reply unavailable") from None
        finally:
            conn.close()
        if status == 409 and result == {"error": "lease-rejected"}:
            raise LeaseRejected("Captain lease expired or superseded")
        if status == 409 and result == {"error": "project-busy"}:
            raise ProjectBusy("An earlier coding run still holds the repository reservation")
        if status == 403 and result == {"error": "project-scope-required"}:
            raise ProjectScopeRejected("This coding run needs a current exact Nuc approval")
        if status == 503 and result == {"error": "effect-unconfirmed"}:
            raise EffectUnconfirmed("Captain effect needs reconciliation")
        if status != 200 or not isinstance(result, dict):
            raise BrokerUnavailable("Captain authority request was declined")
        return result

    def claim(self):
        result = self.request("/v1/claim", {})
        epoch = result.get("epoch")
        if result.keys() != {"epoch"} or (epoch is not None and (type(epoch) is not int or epoch < 1)):
            raise BrokerUnavailable("Captain authority returned an invalid lease")
        return epoch

    def renew(self, epoch):
        if self.request("/v1/renew", {"epoch": epoch}) != {"epoch": epoch}:
            raise BrokerUnavailable("Captain authority returned an invalid renewal")


    def next_telegram_message(self, epoch):
        return self.request("/v1/telegram/next", {"epoch": epoch})["message"]

    def send_telegram_reply(self, epoch, message_id, phase, text=None):
        payload = {"message_id": message_id, "phase": phase}
        if text is not None:
            payload["text"] = text
        return self.request("/v1/effect", {"epoch": epoch, "kind": "telegram-reply", "payload": payload})["result"]

    def telegram_typing(self, epoch, message_id):
        return self.request("/v1/effect", {"epoch": epoch, "kind": "telegram-typing",
                                          "payload": {"message_id": message_id}})["result"]

    def complete_telegram_message(self, epoch, message_id):
        return self.request("/v1/telegram/complete", {"epoch": epoch, "message_id": message_id})["result"]

    def submit_review(self, epoch, request):
        return self.request("/v1/review/submit", {"epoch": epoch, "request": request}, timeout=7)["receipt"]

    def review_receipt(self, epoch, request):
        return self.request("/v1/review/receipt", {"epoch": epoch, "request": request})["receipt"]

    def poll_review(self, epoch, request):
        return self.request("/v1/review/status", {"epoch": epoch, "request": request})["status"]


    def project_status(self, epoch):
        return self.request("/v1/project/active", {"epoch": epoch})["status"]

    def submit_project(self, epoch, request):
        return self.request("/v1/project/submit", {"epoch": epoch, "request": request})["receipt"]

    def project_receipt(self, epoch, request):
        return self.request("/v1/project/receipt", {"epoch": epoch, "request": request})["receipt"]

    def inspect_project(self, epoch, request):
        return self.request("/v1/project/inspect", {"epoch": epoch, "request": request}, timeout=4)["job"]

    def reconcile_project(self, epoch, request, run):
        return self.request("/v1/project/reconcile", {"epoch": epoch, "request": request, "run": run})["receipt"]


def client_fingerprint(path):
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(path.read_text("ascii"))).digest()


class Broker(HTTPServer):
    def __init__(self, address, authority, *, cert, key, ca, peers, effects=None, inbox=None, poll=None,
                 review=None, project=None):
        if set(peers.values()) != {PRIMARY, STANDBY} or len(peers) != 2:
            raise ValueError("Use distinct certificates for the two Captain hosts")
        self.authority = authority
        self.peers = dict(peers)
        self.effects = dict(effects or {})
        self.inbox = inbox
        self.poll = poll
        self.last_telegram_pull = None
        if poll is not None and "telegram-reply" in self.effects:
            poll.on_owner_update = self.notify_pending
        self.review = review
        self.project = project
        if self.effects.keys() - EFFECT_KINDS:
            raise ValueError("Unknown effect adapter")
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.minimum_version = ssl.TLSVersion.TLSv1_2
        self.tls.load_cert_chain(cert, key)
        self.tls.load_verify_locations(cafile=ca)
        self.tls.verify_mode = ssl.CERT_REQUIRED
        super().__init__(address, Request)

    def notify_pending(self, message_id, sent_at):
        # A recent authenticated pull gives the active gateway its normal progress path.
        last_pull = self.last_telegram_pull
        if (last_pull is not None and time.monotonic() - last_pull < 2
                or not -2 <= time.time() - sent_at <= 15):
            return
        with ThreadPoolExecutor(max_workers=2) as progress:
            acknowledgment = progress.submit(self.send_pending_progress, message_id, "ack")
            self.send_pending_progress(message_id, "typing")
            acknowledgment.result()

    def send_pending_progress(self, message_id, phase):
        try:
            if phase == "ack":
                _, send = self.effects["telegram-reply"]({"message_id": message_id, "phase": "ack"}, queued=True)
            else:
                typing = self.effects.get("telegram-typing")
                if typing is None:
                    return
                _, send = typing({"message_id": message_id})
            self.authority.send_queued_progress(message_id, phase, send)
        except (OSError, ValueError, sqlite3.Error):
            # Uncertain progress stays reserved and the pending turn can still run.
            pass

    def get_request(self):
        connection, address = super().get_request()
        try:
            connection.settimeout(REQUEST_TIMEOUT)
            return self.tls.wrap_socket(connection, server_side=True), address
        except OSError:
            connection.close()
            raise

    def handle_error(self, request, client_address):
        # Request paths, peer identities and payloads stay out of service logs.
        pass


class Request(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def reply(self, status, body):
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        lengths = self.headers.get_all("Content-Length", ())
        if (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,5}", lengths[0])
                or self.headers.get("Transfer-Encoding") is not None
                or self.headers.get_content_type() != "application/json"):
            raise ValueError("Invalid request envelope")
        length = int(lengths[0])
        if not 0 < length <= MAX_REQUEST_BYTES:
            raise ValueError("Invalid request size")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("Incomplete request")
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("Invalid request body")
        return body

    def do_POST(self):
        fingerprint = hashlib.sha256(self.connection.getpeercert(binary_form=True)).digest()
        node = self.server.peers.get(fingerprint)
        if node is None:
            self.reply(403, {"error": "peer-not-registered"})
            return
        try:
            status, result = self.dispatch(node, self.body())
        except LeaseRejected:
            status, result = 409, {"error": "lease-rejected"}
        except ReviewScopeRejected:
            status, result = 403, {"error": "review-scope-required"}
        except ProjectScopeRejected:
            status, result = 403, {"error": "project-scope-required"}
        except ProjectBusy:
            status, result = 409, {"error": "project-busy"}
        except EffectUnconfirmed:
            status, result = 503, {"error": "effect-unconfirmed"}
        except (ValueError, RecursionError):
            status, result = 400, {"error": "invalid-request"}
        except Exception:
            status, result = 503, {"error": "broker-unavailable"}
        self.reply(status, result)

    def dispatch(self, node, body):
        authority = self.server.authority
        if self.path == "/v1/claim" and not body:
            grant = authority.claim(node)
            return 200, {"epoch": grant.epoch if grant is not None else None}
        epoch = body.get("epoch")
        if type(epoch) is not int or not 0 < epoch < 2**63:
            raise ValueError("Invalid epoch")
        grant = Grant(node, epoch)
        if self.path == "/v1/renew" and body.keys() == {"epoch"}:
            authority.renew(grant)
            return 200, {"epoch": epoch}
        if self.path == "/v1/telegram/next" and body.keys() == {"epoch"} and self.server.inbox is not None:
            authority.require_grant(grant)
            update = self.server.inbox.next()
            authority.require_grant(grant)
            self.server.last_telegram_pull = time.monotonic()
            if update is None and self.server.poll is not None and not self.server.poll.available.is_set():
                return 503, {"error": "telegram-poll-unavailable"}
            return 200, {"message": update}
        if (self.path == "/v1/telegram/complete" and body.keys() == {"epoch", "message_id"}
                and self.server.inbox is not None):
            message_id = body["message_id"]
            operation = reply_operation(message_id, "final")
            state = authority.effect_status(grant, "telegram-reply", operation)
            if state is None:
                return 409, {"error": "reply-receipt-required"}
            self.server.inbox.complete(message_id, state)
            return 200, {"result": state}
        if self.path in ("/v1/review/submit", "/v1/review/receipt", "/v1/review/status") and body.keys() == {"epoch", "request"}:
            authority.require_grant(grant)
            review = self.server.review
            if review is None:
                return 403, {"error": "review-not-enabled"}
            request = body["request"]
            if self.path == "/v1/review/status":
                return 200, {"status": review.poll(request)}
            receipt = (review.submit(authority, grant, request) if self.path == "/v1/review/submit"
                       else review.receipt(request))
            return 200, {"receipt": receipt}
        if self.path == "/v1/project/active" and body.keys() == {"epoch"}:
            authority.require_grant(grant)
            project = self.server.project
            status = {"enabled": project is not None, "jobs": project.active() if project else []}
            authority.require_grant(grant)
            return 200, {"status": status}
        if self.path in ("/v1/project/submit", "/v1/project/receipt", "/v1/project/reconcile", "/v1/project/inspect"):
            authority.require_grant(grant)
            project = self.server.project
            if project is None:
                return 403, {"error": "project-not-enabled"}
            fields = {"epoch", "request", "run"} if self.path == "/v1/project/reconcile" else {"epoch", "request"}
            if body.keys() != fields:
                raise ValueError("Expected an exact project queue request")
            request = body["request"]
            if self.path == "/v1/project/inspect":
                job = project.inspect(request)
                authority.require_grant(grant)
                return 200, {"job": job}
            if self.path == "/v1/project/reconcile":
                receipt = project.reconcile(request, body["run"])
            elif self.path == "/v1/project/submit":
                receipt = project.submit(authority, grant, request)
            else:
                receipt = project.receipt(request)
            return 200, {"receipt": receipt}
        if self.path == "/v1/effect" and body.keys() == {"epoch", "kind", "payload"}:
            authority.require_grant(grant)
            kind, payload = body["kind"], body["payload"]
            if not isinstance(kind, str) or not isinstance(payload, dict):
                raise ValueError("Invalid effect")
            adapter = self.server.effects.get(kind)
            if adapter is None:
                return 403, {"error": "effect-not-enabled"}
            # The adapter performs only local validation and derives the stable
            # identity before supplying the broker-owned sender.
            operation, send = adapter(payload)

            def send_once():
                try:
                    send()
                except Exception:
                    raise EffectUnconfirmed("Effect outcome unconfirmed") from None

            outcome = authority.perform(grant, kind, operation, send_once)
            return 200, {"result": outcome}
        raise ValueError("Unknown command")


def telegram_components(state, config):
    settings = read_private_json(config)
    if not isinstance(settings, dict) or settings.keys() != {"token", "bot_id", "owner"}:
        raise ValueError("Invalid Telegram broker configuration")
    api = BotAPI(settings["token"], settings["bot_id"])
    api.verify()
    inbox = TelegramInbox(state, settings["owner"], settings["bot_id"])
    effects = {"telegram-reply": inbox.reply_adapter(api.send_message),
               "telegram-typing": inbox.typing_adapter(api.send_typing)}
    return inbox, effects, TelegramPoll(inbox, api.get_updates)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", type=ipaddress.IPv4Address, default=ipaddress.IPv4Address("127.0.0.1"))
    parser.add_argument("--port", type=int, default=4372)
    for option in ("state", "ca", "cert", "key", "nuc-cert", "mac-cert"):
        parser.add_argument("--" + option, type=Path, required=True)
    parser.add_argument("--telegram-config", type=Path)
    parser.add_argument("--review-queue-config", type=Path)
    parser.add_argument("--project-queue-config", type=Path)
    args = parser.parse_args()
    if args.listen.is_global or args.listen.is_unspecified or args.listen.is_multicast:
        parser.error("Select a specific private interface")
    try:
        peers = {client_fingerprint(args.nuc_cert): PRIMARY, client_fingerprint(args.mac_cert): STANDBY}
        with StandbyAuthority(args.state) as authority:
            inbox, effects, poll = None, {}, None
            if args.telegram_config is not None:
                inbox, effects, poll = telegram_components(args.state, args.telegram_config)
            review = (ReviewQueue(args.state, read_private_json(args.review_queue_config))
                      if args.review_queue_config is not None else None)
            project = (ProjectQueue(args.state, read_private_json(args.project_queue_config))
                       if args.project_queue_config is not None else None)
            with Broker((str(args.listen), args.port), authority, cert=args.cert, key=args.key, ca=args.ca,
                        peers=peers, effects=effects, inbox=inbox, poll=poll, review=review, project=project) as broker:
                stop = Event()
                tasks = [target for target in (poll.run if poll else None, project.refresh if project else None)
                         if target is not None]
                threads = [Thread(target=target, args=(stop,), daemon=True) for target in tasks]
                for thread in threads:
                    thread.start()
                try:
                    broker.serve_forever()
                finally:
                    stop.set()
                    for thread in threads:
                        thread.join(timeout=POLL_TRANSPORT_SECONDS + 1)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RecursionError, sqlite3.Error):
        print("Captain standby broker could not start.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Loopback Telegram API adapter for one broker-leased OpenClaw gateway."""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from threading import Event, Lock
import time
from urllib.parse import parse_qs, urlsplit

from standby import LeaseRejected
from standby_broker import BrokerUnavailable, EffectUnconfirmed, ProjectBusy, ProjectScopeRejected
from standby_telegram import positive_id


MAX_BODY = 32_768
INITIAL_TIMEOUT = 3


class Channel(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, broker, epoch, owner, bot_id, local_token, *, port=0, admit_project=None):
        if not all(map(positive_id, (epoch, owner, bot_id))) or not isinstance(local_token, str):
            raise ValueError("Expected a broker lease and a private loopback identity")
        if re.fullmatch(r"[1-9][0-9]{5,15}:[A-Za-z0-9_-]{30,}", local_token) is None:
            raise ValueError("Invalid local Telegram token")
        self.broker, self.epoch, self.owner = broker, epoch, owner
        self.bot = {"id": bot_id, "is_bot": True, "first_name": "Bob", "username": "captain_standby_bot"}
        self.chat = {"id": owner, "type": "private", "first_name": "Owner"}
        self.token_path = "/bot" + local_token + "/"
        self.offered = None
        self.offered_project = None
        self.admit_project = admit_project
        self.lock = Lock()
        self.idle = Event()
        super().__init__(("127.0.0.1", port), LocalRequest)

    def get_request(self):
        sock, peer = super().get_request()
        sock.settimeout(INITIAL_TIMEOUT)
        return sock, peer

    def handle_error(self, request, client_address):
        pass

    def updates(self):
        with self.lock:
            message = self.broker.next_telegram_message(self.epoch)
            if message is not None and message["message_id"] != self.offered:
                self.offered = message["message_id"]
                text = re.sub(r" +", " ", message["text"].strip().lower().rstrip("?.!"))
                project = re.fullmatch(r"captain this:\s*advance ([a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*) #([1-9]\d{0,7})", text)
                self.offered_project = project.groups() if project else None
                author = {**self.chat, "is_bot": False}
                return [{"update_id": self.offered, "message": {**message, "chat": self.chat, "from": author}}]
        self.idle.wait(0.25)
        return []

    def submit_project(self, fields):
        if fields.keys() != {"chat_id", "fo_message_id", "repository", "issue"} or str(fields["chat_id"]) != str(self.owner):
            raise ValueError("Invalid coding request context")
        repository, issue = fields["repository"], fields["issue"]
        if not isinstance(repository, str) or not positive_id(issue):
            raise ValueError("Invalid coding issue identity")
        with self.lock:
            message_id = fields["fo_message_id"]
            if not positive_id(message_id) or message_id != self.offered or self.offered_project != (repository, str(issue)):
                raise ValueError("The coding request must match the original owner command")
        if self.admit_project is None:
            return {"result": "runner-unconfigured"}
        try:
            return self.admit_project(repository, issue)
        except ProjectScopeRejected:
            return {"result": "scope-denied"}
        except ProjectBusy:
            return {"result": "active"}
        except LeaseRejected:
            return {"result": "stale"}
        except BrokerUnavailable:
            return {"result": "admission-unconfirmed"}

    def progress(self, fields):
        message_id = fields.get("fo_message_id")
        with self.lock:
            if (fields.keys() != {"chat_id", "fo_message_id"} or str(fields["chat_id"]) != str(self.owner)
                    or not positive_id(message_id) or message_id != self.offered):
                raise ValueError("Progress needs the original brokered owner message")
        return self.broker.telegram_progress(self.epoch, message_id)

    def typing(self, fields):
        if str(fields.get("chat_id")) != str(self.owner) or fields.get("action") != "typing":
            raise ValueError("Invalid Telegram action")
        message_id = fields.get("fo_message_id")
        with self.lock:
            if not positive_id(message_id) or message_id != self.offered:
                raise ValueError("Typing needs the original brokered message ID")
        if self.broker.telegram_typing(self.epoch, message_id) != "confirmed":
            raise EffectUnconfirmed("Telegram typing is unconfirmed")
        return True

    def send(self, fields):
        if str(fields.get("chat_id")) != str(self.owner):
            raise ValueError("Invalid Telegram recipient")
        if fields.get("reply_markup") not in (None, "", {}, "{}"):
            raise ValueError("Interactive replies need brokered callback support")
        reference = fields.get("reply_parameters")
        if isinstance(reference, str):
            reference = json.loads(reference)
        supplied = reference.get("message_id") if isinstance(reference, dict) else fields.get("reply_to_message_id")
        if isinstance(supplied, str) and re.fullmatch(r"[1-9][0-9]{0,15}", supplied):
            supplied = int(supplied)
        with self.lock:
            if not positive_id(supplied) or supplied != self.offered:
                raise ValueError("The reply needs the original brokered message ID")
            phase = fields.get("fo_phase", "final")
            text = fields.get("text")
            if not isinstance(text, str) or not text or phase not in ("ack", "final") or (
                    phase == "ack" and text != "On it."):
                raise ValueError("Invalid brokered reply")
            try:
                result = self.broker.send_telegram_reply(self.epoch, supplied, phase, text if phase == "final" else None)
            except EffectUnconfirmed:
                if phase == "final":
                    self.broker.complete_telegram_message(self.epoch, supplied)
                raise
            if phase == "final":
                self.broker.complete_telegram_message(self.epoch, supplied)
            if result != "confirmed":
                raise EffectUnconfirmed("Telegram receipt is unconfirmed")
            # The loopback response is only an acknowledgment to OpenClaw.
            # Real sends and receipt verification happen exclusively at the broker.
            return {"message_id": supplied, "date": int(time.time()), "from": self.bot, "chat": self.chat, "text": text}


class LocalRequest(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def reply(self, status, body):
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.do_POST()

    def fields(self, query):
        lengths = self.headers.get_all("Content-Length", ())
        if self.headers.get("Transfer-Encoding") or len(lengths) > 1:
            raise ValueError("Invalid Telegram request frame")
        length = 0
        if lengths:
            if not re.fullmatch(r"[0-9]{1,5}", lengths[0]):
                raise ValueError("Invalid Telegram request length")
            length = int(lengths[0])
        if length > MAX_BODY:
            raise ValueError("Telegram request is too large")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("Incomplete Telegram request")
        if length and self.headers.get_content_type() == "application/json":
            values = json.loads(raw)
            if not isinstance(values, dict):
                raise ValueError("Invalid Telegram request object")
            return values
        return {key: values[-1] for key, values in parse_qs(raw.decode() if raw else query,
                                                           max_num_fields=32).items()}

    def do_POST(self):
        channel = self.server
        target = urlsplit(self.path)
        prefix, _, method = target.path.rpartition("/")
        if not hmac.compare_digest((prefix + "/").encode(), channel.token_path.encode()):
            self.reply(401, {"ok": False, "error_code": 401, "description": "Loopback identity rejected"})
            return
        try:
            fields = self.fields(target.query)
            if method == "getMe":
                result = channel.bot
            elif method == "getWebhookInfo":
                result = {"url": "", "has_custom_certificate": False, "pending_update_count": 0}
            elif method in ("deleteWebhook", "setMyCommands", "deleteMyCommands"):
                # This local adapter has no webhook or command registration state.
                result = True
            elif method == "getUpdates":
                result = channel.updates()
            elif method == "foProjectStatus":
                if str(fields.get("chat_id")) != str(channel.owner):
                    raise ValueError("Invalid Telegram recipient")
                result = channel.broker.project_status(channel.epoch)
            elif method in ("foProjectDailyDue", "foProjectDailySave"):
                if str(fields.get("chat_id")) != str(channel.owner):
                    raise ValueError("Invalid daily briefing context")
                if method == "foProjectDailyDue" and fields.keys() == {"chat_id"}:
                    result = channel.broker.project_daily_due(channel.epoch)
                elif (method == "foProjectDailySave" and fields.keys() == {"chat_id", "date", "epoch", "snapshot"}
                      and type(fields["epoch"]) is int and fields["epoch"] == channel.epoch):
                    result = channel.broker.save_project_daily(channel.epoch, fields["date"], fields["snapshot"])
                else:
                    raise ValueError("Invalid daily briefing request")
            elif method == "foProjectInspect":
                if fields.keys() != {"chat_id", "request"} or str(fields["chat_id"]) != str(channel.owner):
                    raise ValueError("Invalid coding request context")
                result = channel.broker.inspect_project(channel.epoch, fields["request"])
            elif method == "foProjectSubmit":
                result = channel.submit_project(fields)
            elif method == "foProgress":
                result = channel.progress(fields)
            elif method == "sendChatAction":
                result = channel.typing(fields)
            elif method == "sendMessage":
                result = channel.send(fields)
            else:
                raise ValueError("Unsupported loopback operation")
        except (LeaseRejected, BrokerUnavailable):
            self.reply(503, {"ok": False, "error_code": 503, "description": "Brokered delivery unavailable"})
        except (ValueError, RecursionError):
            self.reply(400, {"ok": False, "error_code": 400, "description": "Loopback request declined"})
        else:
            self.reply(200, {"ok": True, "result": result})

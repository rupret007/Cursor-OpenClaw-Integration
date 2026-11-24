"""Private owner-only Telegram inbox for the standby broker."""

from contextlib import closing, contextmanager
import http.client
import json
from pathlib import Path
import re
import sqlite3
import ssl
from threading import Event
import time

from standby import open_private


MAX_PENDING = 512
MAX_TEXT_BYTES = 16_384
POLL_WAIT_SECONDS = 4
POLL_TRANSPORT_SECONDS = 10


def positive_id(value):
    return type(value) is int and 0 < value < 2**63 - 1


def reply_operation(message_id, phase):
    if not positive_id(message_id) or phase not in ("ack", "final"):
        raise ValueError("Invalid Telegram receipt identity")
    return f"message:{message_id}:{phase}"


class TelegramInbox:
    def __init__(self, directory: Path, owner: int, bot_id: int):
        if not positive_id(owner) or not positive_id(bot_id):
            raise ValueError("Use a single verified Telegram owner and bot")
        self.owner, self.bot_id = owner, bot_id
        self.directory = directory
        self.path = directory / "telegram-inbox.sqlite"
        with self.database() as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS inbox (
                    message_id INTEGER PRIMARY KEY,
                    sent_at INTEGER NOT NULL,
                    text TEXT,
                    outcome TEXT
                )
            """)
            db.execute("CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY CHECK (id = 1), "
                       "owner INTEGER NOT NULL, bot INTEGER NOT NULL)")
            db.execute("INSERT OR IGNORE INTO identity VALUES (1, ?, ?)", (owner, bot_id))
            if db.execute("SELECT owner, bot FROM identity WHERE id = 1").fetchone() != (owner, bot_id):
                raise ValueError("Telegram inbox belongs to a different owner or bot")

    @contextmanager
    def database(self):
        with open_private(self.directory, self.path.name), closing(sqlite3.connect(self.path, timeout=1)) as db:
            db.execute("PRAGMA secure_delete=ON")
            yield db

    def selected_message(self, update):
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        chat, author = message.get("chat"), message.get("from")
        if (not isinstance(chat, dict) or chat.get("type") != "private" or type(chat.get("id")) is not int
                or chat["id"] != self.owner or not isinstance(author, dict) or type(author.get("id")) is not int
                or author["id"] != self.owner or author.get("is_bot") is not False):
            return None
        message_id, sent_at, text = message.get("message_id"), message.get("date"), message.get("text")
        if (not positive_id(message_id) or not positive_id(sent_at) or not isinstance(text, str)
                or not text or len(text.encode("utf-8")) > MAX_TEXT_BYTES):
            return None
        return message_id, sent_at, text

    def ingest(self, updates):
        """Return the next upstream offset only after the accepted rows commit."""
        if not isinstance(updates, list) or len(updates) > 100:
            raise ValueError("Invalid Telegram update batch")
        ids, messages = [], []
        for update in updates:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if not positive_id(update_id):
                raise ValueError("Invalid Telegram update identity")
            ids.append(update_id)
            message = self.selected_message(update)
            if message is not None:
                messages.append(message)
        with self.database() as db, db:
            db.executemany("INSERT OR IGNORE INTO inbox (message_id, sent_at, text) VALUES (?, ?, ?)", messages)
            pending = db.execute("SELECT count(*) FROM inbox WHERE outcome IS NULL").fetchone()[0]
            if pending > MAX_PENDING:
                raise OverflowError("Telegram inbox needs attention")
        return max(ids) + 1 if ids else None

    def next(self):
        with self.database() as db:
            row = db.execute("SELECT message_id, sent_at, text FROM inbox WHERE outcome IS NULL "
                             "ORDER BY message_id LIMIT 1").fetchone()
        if row is None:
            return None
        return dict(zip(("message_id", "date", "text"), row))

    def pending(self, message_id):
        with self.database() as db:
            return db.execute("SELECT 1 FROM inbox WHERE message_id = ? AND outcome IS NULL",
                              (message_id,)).fetchone() is not None

    def reply_adapter(self, send_message):
        def prepare(payload, *, queued=False):
            if payload.keys() not in ({"message_id", "phase"}, {"message_id", "phase", "text"}):
                raise ValueError("Invalid Telegram reply")
            message_id, phase = payload["message_id"], payload["phase"]
            operation = reply_operation(message_id, phase)
            if not self.pending(message_id):
                raise ValueError("Telegram message is no longer pending")
            if phase == "ack" and "text" not in payload:
                text = "Queued. I’ll reply when the gateway is ready." if queued else "On it."
            else:
                text = payload.get("text")
                if phase != "final" or not isinstance(text, str) or not 0 < len(text.encode("utf-16-le")) <= 8192:
                    raise ValueError("Invalid Telegram reply text")

            def send():
                receipt = send_message(self.owner, text, message_id)
                chat = receipt.get("chat") if isinstance(receipt, dict) else None
                if (not isinstance(chat, dict) or type(chat.get("id")) is not int or chat["id"] != self.owner
                        or not positive_id(receipt.get("message_id"))):
                    raise ValueError("Telegram did not confirm delivery to the owner")

            return operation, send

        return prepare

    def typing_adapter(self, send_typing):
        def prepare(payload):
            message_id = payload.get("message_id")
            if payload.keys() != {"message_id"} or not positive_id(message_id) or not self.pending(message_id):
                raise ValueError("Typing needs a pending Telegram message")

            def send():
                if send_typing(self.owner) is not True:
                    raise ValueError("Telegram did not confirm typing")

            return f"message:{message_id}:typing", send

        return prepare

    def complete(self, message_id, outcome):
        if outcome not in ("confirmed", "unconfirmed"):
            raise ValueError("Telegram reply receipt required")
        with self.database() as db, db:
            row = db.execute("SELECT outcome FROM inbox WHERE message_id = ?", (message_id,)).fetchone()
            if row is None or row[0] not in (None, outcome):
                raise ValueError("Telegram inbox entry is unavailable")
            db.execute("UPDATE inbox SET text = NULL, outcome = ? WHERE message_id = ?", (outcome, message_id))


class TelegramPoll:
    def __init__(self, inbox, get_updates):
        self.inbox = inbox
        self.get_updates = get_updates
        self.offset = None
        self.available = Event()
        self.last_success = None
        self.on_owner_update = None

    def step(self):
        # The previous successful step durably staged everything below this
        # offset. Start without an offset after restart to handle old cursors.
        updates = self.get_updates(self.offset)
        self.offset = self.inbox.ingest(updates)
        self.last_success = time.monotonic()
        self.available.set()
        if self.on_owner_update is not None:
            for update in updates:
                message = self.inbox.selected_message(update)
                if message is not None:
                    self.on_owner_update(message[0], message[1])

    def recent(self):
        return (self.available.is_set() and self.last_success is not None
                and time.monotonic() - self.last_success <= 2 * POLL_TRANSPORT_SECONDS)

    def run(self, stop):
        while not stop.is_set():
            try:
                self.step()
            except (OSError, ValueError, OverflowError, sqlite3.Error):
                self.offset = None
                self.available.clear()
                # Credentials, links and raw update contents never reach logs.
                stop.wait(2)


class BotAPI:
    def __init__(self, token, bot_id):
        if (not isinstance(token, str) or not re.fullmatch(r"[1-9][0-9]{5,15}:[A-Za-z0-9_-]{30,}", token)
                or not positive_id(bot_id) or int(token.split(":", 1)[0]) != bot_id):
            raise ValueError("Invalid Telegram bot configuration")
        self._token = token
        self.bot_id = bot_id
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.tls.load_default_certs()

    def call(self, method, fields, *, timeout):
        conn = http.client.HTTPSConnection("api.telegram.org", 443, context=self.tls, timeout=timeout)
        try:
            conn.request("POST", f"/bot{self._token}/{method}", json.dumps(fields),
                         {"Content-Type": "application/json"})
            response = conn.getresponse()
            raw = response.read(2_000_001)
            if response.status != 200 or len(raw) > 2_000_000:
                raise ValueError
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("ok") is not True or "result" not in payload:
                raise ValueError
            return payload["result"]
        except (OSError, http.client.HTTPException, ValueError, RecursionError):
            raise OSError("Telegram request could not be confirmed") from None
        finally:
            conn.close()

    def verify(self):
        bot = self.call("getMe", {}, timeout=3)
        webhook = self.call("getWebhookInfo", {}, timeout=3)
        if (not isinstance(bot, dict) or bot.get("is_bot") is not True or type(bot.get("id")) is not int
                or bot["id"] != self.bot_id or not isinstance(webhook, dict) or webhook.get("url") != ""):
            raise ValueError("Telegram bot or polling mode could not be verified")

    def get_updates(self, offset):
        fields = {"allowed_updates": ["message"], "limit": 20, "timeout": POLL_WAIT_SECONDS}
        if offset is not None:
            fields["offset"] = offset
        # The server's long-poll hold is separate from connection and response latency.
        return self.call("getUpdates", fields, timeout=POLL_TRANSPORT_SECONDS)

    def send_typing(self, owner):
        return self.call("sendChatAction", {"chat_id": owner, "action": "typing"}, timeout=1.25)

    def send_message(self, owner, text, message_id):
        return self.call("sendMessage", {"chat_id": owner, "text": text,
                                        "reply_parameters": {"message_id": message_id}}, timeout=2.5)

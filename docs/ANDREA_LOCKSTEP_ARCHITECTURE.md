# Andrea lockstep architecture

Strict two-way sync between **user channels** (Telegram, Alexa, CLI), **OpenClaw**, and **Cursor** uses a **single local command/event store**. No channel is authoritative alone; projected **task state** is derived from append-only events.

## Components

| Piece | Path | Role |
|-------|------|------|
| Schema | `services/andrea_sync/schema.py` | Command types, event types, task status, idempotency rules |
| Store | `services/andrea_sync/store.py` | SQLite WAL (`data/andrea_sync.db` by default) |
| Bus | `services/andrea_sync/bus.py` | Accept commands, enforce idempotency, append events |
| Projector | `services/andrea_sync/projector.py` | Derive task JSON from events |
| HTTP API | `services/andrea_sync/server.py` | REST ingress for commands, Telegram webhook, Alexa skill |
| Server entry | `scripts/andrea_sync_server.py` | Run from repo root |
| Cursor CLI hook | `scripts/andrea_sync_cursor_report.py` | Emit lifecycle events (HTTP or `--db`) |
| Health | `scripts/andrea_sync_health.py` | Optional doctor probe |

## HTTP API (v1)

| Method | Path | Notes |
|--------|------|--------|
| GET | `/v1/health` | Liveness + db path |
| POST | `/v1/commands` | JSON command envelope (see schema) |
| GET | `/v1/tasks` | Recent tasks (`?limit=`) |
| GET | `/v1/tasks/{id}` | Projected state + event list |
| POST | `/v1/internal/events` | Append raw event; requires `Authorization: Bearer $ANDREA_SYNC_INTERNAL_TOKEN` |
| POST | `/v1/telegram/webhook?secret=...` | Telegram `Update` JSON; returns `200` immediately, processes async |
| POST | `/v1/alexa` | Alexa skill request JSON; returns skill response; enqueues command async |

## Environment

| Variable | Purpose |
|----------|---------|
| `ANDREA_SYNC_DB` | Override SQLite path (default: `<repo>/data/andrea_sync.db`) |
| `ANDREA_SYNC_PORT` | Listen port (default `8765`) |
| `ANDREA_SYNC_TELEGRAM_SECRET` | Query `secret` for Telegram webhook |
| `ANDREA_SYNC_INTERNAL_TOKEN` | Bearer token for `/v1/internal/events` |
| `ANDREA_SYNC_URL` | Base URL for health probe / `cursor_report` HTTP mode |
| `ANDREA_SYNC_DOCTOR` | Set `1` to run health during `andrea_doctor.sh` |
| `ANDREA_SYNC_REQUIRED` | If `1`, health probe fails when URL unreachable |
| `ANDREA_SYNC_VERBOSE` | `1` logs HTTP requests to stderr |

## Idempotency

Commands without `idempotency_key` use a deterministic hash of `channel`, `external_id`, and `command_type`. Duplicate deliveries append `CommandDeduped` and return the same `task_id`.

## Cursor / OpenClaw wiring

1. OpenClaw (or Telegram) creates a task with `CreateCursorJob` or `SubmitUserMessage` then `CreateCursorJob`.
2. After `cursor_handoff` / `cursor_openclaw` runs, shell wrappers or CI call `scripts/andrea_sync_cursor_report.py` with `JobStarted`, `JobProgress`, `JobCompleted`, or `JobFailed`.
3. Downstream notifiers (future) read `GET /v1/tasks/{id}` and post summaries back to Telegram/Alexa from **projected state**, not ad-hoc chat text.

## Security

- Do not expose `/v1/internal/events` without a strong random `ANDREA_SYNC_INTERNAL_TOKEN`.
- Telegram webhook URL should include a long random `secret` query param known only to Telegram `setWebhook`.
- Treat the SQLite file like a journal: backup with the rest of your operator secrets.

## Operations

Telegram ingest over the public internet: [ANDREA_TELEGRAM_LOCKSTEP_E2E.md](ANDREA_TELEGRAM_LOCKSTEP_E2E.md).

```bash
# Start server (repo root)
python3 scripts/andrea_sync_server.py

# Manual command
curl -sS -X POST http://127.0.0.1:8765/v1/commands \
  -H 'Content-Type: application/json' \
  -d '{"command_type":"CreateTask","channel":"cli","payload":{"summary":"demo"}}'
```

See [ANDREA_ALEXA_INTEGRATION.md](ANDREA_ALEXA_INTEGRATION.md) for voice.

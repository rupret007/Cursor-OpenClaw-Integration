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
| Policy | `services/andrea_sync/policy.py` | Verify-before-deny using published capability digest + TTL |
| Kill switch | `services/andrea_sync/kill_switch.py` | Env + flag file + meta; halts ingress when engaged |
| Server entry | `scripts/andrea_sync_server.py` | Run from repo root |
| Cursor CLI hook | `scripts/andrea_sync_cursor_report.py` | Emit lifecycle events (HTTP or `--db`) |
| Health | `scripts/andrea_sync_health.py` | Optional doctor probe |

## HTTP API (v1)

| Method | Path | Notes |
|--------|------|--------|
| GET | `/v1/health` | Liveness + db path + `kill_switch` summary + capability digest age |
| GET | `/v1/status` | Extended JSON: kill switch + full capability digest payload |
| GET | `/v1/capabilities` | Cached capability snapshot (from last `PublishCapabilitySnapshot`) |
| GET | `/v1/policy/skill-absence?skill=...` | Verify-before-deny: may a channel claim this skill is absent? (`max_age_seconds` optional) |
| POST | `/v1/commands` | JSON command envelope (see schema). Admin commands require `Authorization: Bearer $ANDREA_SYNC_INTERNAL_TOKEN` |
| GET | `/v1/tasks` | Recent tasks (`?limit=`) |
| GET | `/v1/tasks/{id}` | Projected state + event list |
| POST | `/v1/internal/events` | Append raw event; requires `Authorization: Bearer $ANDREA_SYNC_INTERNAL_TOKEN` |
| POST | `/v1/telegram/webhook?secret=...` | Telegram `Update` JSON; optional `X-Telegram-Bot-Api-Secret-Token` when `ANDREA_SYNC_TELEGRAM_WEBHOOK_SECRET` is set |
| POST | `/v1/alexa` | Alexa skill request JSON; returns skill response; enqueues command async |

**Admin command types** (must use `"channel":"internal"` + Bearer token on `/v1/commands`):

- `PublishCapabilitySnapshot` — body is typically `scripts/andrea_capabilities.py --json` output; sets canonical digest for policy.
- `KillSwitchEngage` / `KillSwitchRelease` — emergency halt / resume (see `scripts/andrea_kill_switch.sh`).

When the kill switch is engaged, Telegram/Alexa ingress and normal commands return **503**; only `KillSwitchRelease` is accepted (with token).

## Environment

| Variable | Purpose |
|----------|---------|
| `ANDREA_SYNC_DB` | Override SQLite path (default: `<repo>/data/andrea_sync.db`) |
| `ANDREA_SYNC_PORT` | Listen port (default `8765`) |
| `ANDREA_SYNC_TELEGRAM_SECRET` | Query `secret` for Telegram webhook (optional if header secret used) |
| `ANDREA_SYNC_TELEGRAM_WEBHOOK_SECRET` | Telegram `secret_token`; verified via `X-Telegram-Bot-Api-Secret-Token` |
| `ANDREA_SYNC_INTERNAL_TOKEN` | Bearer token for admin `/v1/commands` and `/v1/internal/events` |
| `ANDREA_SYNC_KILL_SWITCH` | If `1`/`true`, forces kill switch engaged (process env) |
| `ANDREA_SYNC_KILL_FILE` | Override path for kill flag file (default: `<db-file>.kill` beside active DB) |
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
- Prefer Telegram `setWebhook` **`secret_token`** + `X-Telegram-Bot-Api-Secret-Token` verification; query `secret=` remains supported as a fallback.
- Admin commands on `/v1/commands` require the same Bearer token; do not expose that endpoint publicly without TLS + network ACLs.
- Treat the SQLite file like a journal: backup with the rest of your operator secrets.

## Review notes

Design/gap analysis: [ANDREA_LOCKSTEP_REVIEW_FINDINGS.md](ANDREA_LOCKSTEP_REVIEW_FINDINGS.md).

## macOS auto-start

Templates + installer: `scripts/macos/install_andrea_launchagents.sh` (optional `cloudflared` + OpenClaw login refresh). Put secrets in `~/andrea-lockstep.env`.

## Operator full cycle

`bash scripts/andrea_full_cycle.sh` — git pull, `/v1/health` + `/v1/status`, capability publish, policy probe, optional `openclaw gateway restart`, communication smoke, kill-switch drill, optional Telegram `webhook-info`. Requires `ANDREA_SYNC_INTERNAL_TOKEN` and a running `andrea_sync` server at `ANDREA_SYNC_URL`.

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

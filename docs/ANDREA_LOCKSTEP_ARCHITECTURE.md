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
| `TELEGRAM_BOT_TOKEN` | Needed for Telegram ACK/progress/final replies |
| `CURSOR_API_KEY` | Needed for default Telegram -> Cursor executor flow |
| `ANDREA_CURSOR_REPO` | Optional repo path for Telegram-triggered Cursor handoff (default repo root) |
| `ANDREA_CURSOR_HANDOFF_MODE` | `auto` / `api` / `cli` for the built-in Telegram executor |
| `ANDREA_SYNC_PUBLIC_BASE` | Public HTTPS origin for Telegram webhook self-heal |

## Idempotency

Commands without `idempotency_key` use a deterministic hash of `channel`, `external_id`, and `command_type`. Duplicate deliveries append `CommandDeduped` and return the same `task_id`.

## Cursor / OpenClaw wiring

1. A normal Telegram message lands on `/v1/telegram/webhook`, becomes `SubmitUserMessage`, and is persisted into lockstep first.
2. `services/andrea_sync/server.py` applies Andrea-first routing:
   - direct Andrea reply for lightweight conversational/personal assistant turns
   - Cursor delegation for heavier repo, coding, debugging, or long-running work
   - direct Andrea replies can also look at recent Telegram chat history before answering
3. Delegated tasks are queued as `JobQueued`, then the built-in executor launches `skills/cursor_handoff/scripts/cursor_handoff.py` and polls agent status with `scripts/cursor_openclaw.py`.
4. Delegated lifecycle is appended back into lockstep as `JobStarted`, `JobCompleted`, or `JobFailed`.
5. The same server process posts Telegram replies from projected task state, not ad-hoc chat text.
6. Telegram replies are formatted as:
   - `Andrea:` user-facing answer first
   - `What happened:` compressed execution summary
   - `Cursor said:` short excerpt from agent output
   - `Technical details:` task id, status, PR, agent URL
7. Direct Andrea replies intentionally skip Cursor lifecycle noise.
8. When `ANDREA_SYNC_PUBLIC_BASE` is configured, the server also self-heals Telegram webhook registration if another process clears it.

## Security

- Do not expose `/v1/internal/events` without a strong random `ANDREA_SYNC_INTERNAL_TOKEN`.
- Prefer Telegram `setWebhook` **`secret_token`** + `X-Telegram-Bot-Api-Secret-Token` verification; query `secret=` remains supported as a fallback.
- Admin commands on `/v1/commands` require the same Bearer token; do not expose that endpoint publicly without TLS + network ACLs.
- Treat the SQLite file like a journal: backup with the rest of your operator secrets.

## Review notes

Design/gap analysis: [ANDREA_LOCKSTEP_REVIEW_FINDINGS.md](ANDREA_LOCKSTEP_REVIEW_FINDINGS.md).

## macOS auto-start

Templates + installer: `scripts/macos/install_andrea_launchagents.sh` (optional named `cloudflared`, optional OpenClaw login refresh, plus a post-login bootstrap step for capability publish + webhook ensure). The sync LaunchAgent sources repo `.env` first, then `~/andrea-lockstep.env` for per-machine overrides.

This keeps the same assistant persona available across text-first Telegram now and voice-first Alexa later: Andrea answers first, then delegates when the work needs a heavier technical lane.

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

# Telegram lockstep E2E (andrea_sync)

End-to-end path: **Telegram** → **HTTPS webhook** → **local `andrea_sync`** → **SQLite events** → **`GET /v1/tasks`**.

## Prerequisites

1. **Secrets in repo `.env`** (recommended) or exported in your shell. Optional second file: set `ANDREA_ENV_FILE=/path/to/override.env` — values there **override** repo `.env` for duplicate keys.

| Variable | Purpose |
|----------|---------|
| `TELEGRAM_BOT_TOKEN` | BotFather token for `setWebhook` / `getWebhookInfo` |
| `ANDREA_SYNC_TELEGRAM_SECRET` | Optional query param `?secret=` on the webhook URL (fallback) |
| `ANDREA_SYNC_TELEGRAM_WEBHOOK_SECRET` | Recommended: Telegram `secret_token` → header `X-Telegram-Bot-Api-Secret-Token` (set by `andrea_lockstep_telegram_e2e.py` when non-empty) |
| `ANDREA_SYNC_INTERNAL_TOKEN` | For `/v1/internal/events` (not required for Telegram ingest only) |

2. **`andrea_sync` running** on `ANDREA_SYNC_URL` (default `http://127.0.0.1:8765`):

```bash
cd /path/to/Cursor-OpenClaw-Integration
export ANDREA_SYNC_TELEGRAM_SECRET='long-random'
export ANDREA_SYNC_INTERNAL_TOKEN='long-random'
# optional: append same lines to .env
python3 scripts/andrea_sync_server.py
```

3. **`cloudflared` on PATH** (quick tunnel). Install:

```bash
brew install cloudflared
```

If Homebrew errors on permissions:

```bash
sudo chown -R "$(whoami)" /usr/local/Homebrew
```

Alternatively use **ngrok** or any HTTPS reverse proxy; then set `ANDREA_SYNC_PUBLIC_BASE` yourself (see below).

## One-shot: tunnel + webhook

With `.env` filled and the sync server already listening:

```bash
cd /path/to/Cursor-OpenClaw-Integration
python3 scripts/andrea_lockstep_telegram_e2e.py tunnel-and-webhook
```

This starts a **Cloudflare quick tunnel** to your local `ANDREA_SYNC_URL`, registers **Telegram `setWebhook`**, and keeps the tunnel in the foreground. **URLs change each run** — re-run this (or `set-webhook`) after restarting the tunnel.

## Step-by-step (manual public URL)

1. **Check env** (prints `OK` / `MISSING` only):

```bash
python3 scripts/andrea_lockstep_telegram_e2e.py check-env
python3 scripts/andrea_lockstep_telegram_e2e.py health
```

2. **Start tunnel** (separate terminal), note the `https://….trycloudflare.com` URL:

```bash
cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8765
```

3. **Register webhook**:

```bash
export ANDREA_SYNC_PUBLIC_BASE='https://YOUR_SUBDOMAIN.trycloudflare.com'
python3 scripts/andrea_lockstep_telegram_e2e.py set-webhook
python3 scripts/andrea_lockstep_telegram_e2e.py webhook-info
```

`webhook-info` prints a **redacted** URL. If Telegram shows `last_error_message`, fix TLS, secret mismatch, or local server down.

4. **Send a real message** to your bot in Telegram.

5. **Confirm lockstep**:

```bash
curl -sS "http://127.0.0.1:8765/v1/tasks?limit=20" | python3 -m json.tool
```

Or wait up to 120s for a `telegram` channel row:

```bash
python3 scripts/andrea_lockstep_telegram_e2e.py wait-telegram-task --timeout-sec 120
```

6. **Inspect one task** (replace `tsk_…`):

```bash
curl -sS "http://127.0.0.1:8765/v1/tasks/tsk_…" | python3 -m json.tool
```

## Automation (full cycle)

Use the repo orchestrator (runs from any cwd; it `cd`s to the repo):

```bash
cd /path/to/Cursor-OpenClaw-Integration
export ANDREA_SYNC_INTERNAL_TOKEN='…'
export ANDREA_SYNC_URL='http://127.0.0.1:8765'
# optional for webhook-info step:
export TELEGRAM_BOT_TOKEN='…'
export ANDREA_SYNC_TELEGRAM_SECRET='…'
bash scripts/andrea_full_cycle.sh
```

- **zsh:** always use **real paths**, not `/path/to/...`. URLs with `?` are quoted inside the script.
- **Optional wait for a Telegram message** in the DB: `ANDREA_FULL_CYCLE_WAIT_TELEGRAM=1 bash scripts/andrea_full_cycle.sh`

## Refresh / restart checklist

1. `git pull --ff-only origin main`
2. `bash scripts/test_integration.sh` (optional but recommended)
3. Start **`andrea_sync`** (same `ANDREA_SYNC_*` env as before)
4. If using a quick tunnel: start **`cloudflared`** again → **new URL**
5. Run **`set-webhook`** again with the new `ANDREA_SYNC_PUBLIC_BASE` (or `tunnel-and-webhook`)
6. `python3 scripts/andrea_lockstep_telegram_e2e.py webhook-info` — confirm no delivery errors
7. Send a test message → `wait-telegram-task` or `GET /v1/tasks`
8. Optional: `ANDREA_SYNC_DOCTOR=1 ANDREA_SYNC_URL=http://127.0.0.1:8765 bash scripts/andrea_doctor.sh`

## Operational notes

- **403** on webhook: `?secret=` must equal `ANDREA_SYNC_TELEGRAM_SECRET` exactly.
- **Stable hostname**: quick tunnels rotate; for a fixed URL use a **named Cloudflare tunnel** or your own TLS host.
- **OpenClaw / Cursor** visibility in Telegram is **separate** from lockstep ingest unless you add notifiers that read `GET /v1/tasks/{id}` and post back to Telegram.

## Reference

- [ANDREA_LOCKSTEP_ARCHITECTURE.md](ANDREA_LOCKSTEP_ARCHITECTURE.md)
- [ANDREA_OPERATIONS_PLAYBOOK.md](ANDREA_OPERATIONS_PLAYBOOK.md) §10

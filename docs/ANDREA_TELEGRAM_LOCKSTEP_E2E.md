# Telegram lockstep E2E (andrea_sync)

End-to-end path: **Telegram** → **HTTPS webhook** → **local `andrea_sync`** → **SQLite events** → **Andrea-first routing** → either **direct Andrea reply** or **Cursor/OpenClaw job** → **Telegram reply**.

## Prerequisites

1. **Secrets in repo `.env`** (recommended) or exported in your shell. Optional second file: set `ANDREA_ENV_FILE=/path/to/override.env` — values there **override** repo `.env` for duplicate keys.

| Variable | Purpose |
|----------|---------|
| `TELEGRAM_BOT_TOKEN` | BotFather token for `setWebhook` / `getWebhookInfo` |
| `ANDREA_SYNC_TELEGRAM_SECRET` | Optional query param `?secret=` on the webhook URL (fallback) |
| `ANDREA_SYNC_TELEGRAM_WEBHOOK_SECRET` | Recommended: Telegram `secret_token` → header `X-Telegram-Bot-Api-Secret-Token` (set by `andrea_lockstep_telegram_e2e.py` when non-empty) |
| `ANDREA_SYNC_INTERNAL_TOKEN` | For `/v1/internal/events` (not required for Telegram ingest only) |
| `CURSOR_API_KEY` | Required if the default Telegram executor should launch Cursor Cloud agents |
| `ANDREA_CURSOR_REPO` | Optional repo path the Telegram executor should hand to Cursor (default: current repo root) |
| `ANDREA_SYNC_PUBLIC_BASE` | Public HTTPS base used for webhook autofix/self-heal |

2. **`andrea_sync` running** on `ANDREA_SYNC_URL` (default `http://127.0.0.1:8765`):

```bash
cd /path/to/Cursor-OpenClaw-Integration
export ANDREA_SYNC_TELEGRAM_SECRET='long-random'
export ANDREA_SYNC_INTERNAL_TOKEN='long-random'
# optional: append same lines to .env
python3 scripts/andrea_sync_server.py
```

`scripts/andrea_sync_server.py` now loads repo `.env` automatically before startup-safe overrides from `~/andrea-lockstep.env`, so the same process can handle webhook ingest, Cursor execution, Telegram replies, and webhook self-heal.

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

There are now two Telegram lanes:

- **Direct Andrea lane**
  - lightweight conversational/personal assistant turns
  - no unnecessary Cursor task noise in the chat
  - reply is usually just:
    - `Andrea:` concise direct answer

- **Delegated Cursor lane**
  - repo, coding, debugging, test, or longer-running work
  - projected task should move through `queued` -> `running` -> `completed` or `failed`
  - Telegram should receive:
    - an immediate ACK in Andrea's voice
    - a running/status note once Cursor accepts the work
    - a final completion/failure reply with:
      - `Andrea:` concise user-facing answer first
      - `What happened:` short execution summary
      - `Cursor said:` compact excerpt of the agent result
      - `Technical details:` task id, status, PR, agent link

This reply shape is intentionally optimized for future voice/Alexa reuse: the first Andrea sentence should stand on its own if spoken aloud.

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
6. `python3 scripts/andrea_lockstep_telegram_e2e.py webhook-info` — confirm no delivery errors (or let the server reclaim the webhook automatically if `ANDREA_SYNC_PUBLIC_BASE` is set)
7. Send a test message and wait for both the lockstep row and the Telegram ACK/final reply
8. Optional: `ANDREA_SYNC_DOCTOR=1 ANDREA_SYNC_URL=http://127.0.0.1:8765 bash scripts/andrea_doctor.sh`

## Operational notes

- **403** on webhook: `?secret=` must equal `ANDREA_SYNC_TELEGRAM_SECRET` exactly.
- **Stable hostname**: quick tunnels rotate; for a fixed URL use a **named Cloudflare tunnel** or your own TLS host, then install `scripts/macos/install_andrea_launchagents.sh --with-cloudflared`.
- **Runtime persistence**: the sync LaunchAgent sources repo `.env` plus `~/andrea-lockstep.env`, so `TELEGRAM_BOT_TOKEN`, `CURSOR_API_KEY`, and lockstep secrets survive login.
- **Webhook self-heal**: if `ANDREA_SYNC_PUBLIC_BASE` is set, the running server checks Telegram webhook registration and re-applies it when another process clears it.

## Reference

- [ANDREA_LOCKSTEP_ARCHITECTURE.md](ANDREA_LOCKSTEP_ARCHITECTURE.md)
- [ANDREA_OPERATIONS_PLAYBOOK.md](ANDREA_OPERATIONS_PLAYBOOK.md) §10

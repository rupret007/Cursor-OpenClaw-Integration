# Telegram lockstep E2E (andrea_sync)

End-to-end path: **Telegram** → **HTTPS webhook** → **local `andrea_sync`** → **SQLite events** → **Andrea-first routing** → either **direct Andrea reply** or **Cursor/OpenClaw job** → **Telegram reply**.

## Prerequisites

1. **Secrets in repo `.env`** (recommended) or exported in your shell. Optional override files:
   - `~/andrea-lockstep.env`
   - `ANDREA_ENV_FILE=/path/to/override.env`
   Both override repo `.env` for duplicate keys.

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

`scripts/andrea_sync_server.py` now loads repo `.env`, then cwd `.env`, then startup-safe overrides from `~/andrea-lockstep.env`, and finally `ANDREA_ENV_FILE` when set. That lets the same process handle webhook ingest, Cursor execution, Telegram replies, and webhook self-heal without requiring a separate export step.

3. **`cloudflared` on PATH**. Install:

```bash
brew install cloudflared
```

If Homebrew errors on permissions:

```bash
sudo chown -R "$(whoami)" /usr/local/Homebrew
```

For true reboot-stable operation, prefer a **named Cloudflare tunnel** with `CLOUDFLARED_TUNNEL_TOKEN` and the macOS LaunchAgent flow below. If `cloudflared` is not available on the host, the repo also supports a managed `localtunnel` fallback via `ANDREA_LOCALTUNNEL_SUBDOMAIN`.

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
  - can use recent conversation history from the same Telegram chat before replying
  - no unnecessary Cursor task noise in the chat
  - reply is usually just:
    - `Andrea:` concise direct answer

- **Delegated Cursor lane**
  - repo, coding, debugging, test, or longer-running work
  - projected task should move through `queued` -> `running` -> `completed` or `failed`
  - Telegram should receive:
    - an immediate ACK in Andrea's voice
    - a running/status note once OpenClaw accepts the work (and it may mention Cursor only when OpenClaw escalates)
    - a final completion/failure reply with:
      - `Andrea:` concise user-facing answer first
      - `What happened:` short execution summary
      - `OpenClaw said:` for OpenClaw-only handling, or `Cursor said:` when OpenClaw escalates to Cursor
      - `Technical details:` task id, status, OpenClaw session when available, PR, agent link

This reply shape is intentionally optimized for future voice/Alexa reuse: the first Andrea sentence should stand on its own if spoken aloud.

### Direct addressing

The Telegram bridge now supports lightweight addressing hints in the message text:

- `@Andrea ...` tells the bot to keep the turn in Andrea's direct assistant lane when possible.
- `@Cursor ...` tells Andrea/OpenClaw to run a Cursor-first collaboration pass instead of replying directly.
- `@Andrea @Cursor ...` or natural phrases like `work together`, `team up`, or `double-check` tell the system to have OpenClaw and Cursor collaborate before the final answer.
- `@Gemini ...`, `@Minimax ...`, `@OpenAI ...`, or `@GPT ...` tell Andrea/OpenClaw to start from that preferred model lane when available and report the active provider/model back in Telegram when possible.
- Add phrases like `show the full dialogue`, `show all handoffs`, or `visible collaboration` when you want the Telegram thread to expose richer collaboration updates for an intentional sprint-style session.

These tags are stripped from the delegated prompt before it is sent downstream, so they control routing without polluting the actual work request.

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

## Reboot-ready macOS setup

Use the repo LaunchAgents when you want the suite to come back after login without opening a manual terminal:

```bash
cd /path/to/Cursor-OpenClaw-Integration
export CLOUDFLARED_TUNNEL_TOKEN='...'
bash scripts/macos/install_andrea_launchagents.sh --with-cloudflared --load
```

Fallback when `cloudflared` is not available:

```bash
cd /path/to/Cursor-OpenClaw-Integration
export ANDREA_LOCALTUNNEL_SUBDOMAIN='fine-monkeys-shake'
bash scripts/macos/install_andrea_launchagents.sh --with-localtunnel --load
```

This installs and loads:

- `com.andrea.andrea-sync`
- `com.andrea.andrea-cloudflared`
- `com.andrea.andrea-post-login-bootstrap`

The post-login bootstrap waits for `andrea_sync`, syncs `skills/cursor_handoff` into the OpenClaw workspace, restarts the gateway, publishes the capability snapshot, and re-runs `set-webhook` when `TELEGRAM_BOT_TOKEN` and `ANDREA_SYNC_PUBLIC_BASE` are present.

## Hybrid execution notes

- Default delegated lane: `ANDREA_TELEGRAM_DELEGATE_LANE=openclaw_hybrid`
- OpenClaw runner script: `python3 scripts/andrea_sync_openclaw_hybrid.py --task-id tsk_demo --prompt "Remind me to follow up tomorrow"`
- Direct Cursor fallback remains available if you explicitly set `ANDREA_TELEGRAM_DELEGATE_LANE=direct_cursor` or if `ANDREA_OPENCLAW_FALLBACK_TO_CURSOR=1` is allowed during an OpenClaw launch failure
- OpenClaw automation uses `openclaw agent --agent "${ANDREA_OPENCLAW_AGENT_ID:-main}"`, so the OpenClaw gateway and the mirrored `cursor_handoff` skill both need to stay healthy on the host
- When the user explicitly asks for Cursor collaboration (`@Cursor` or collaborative phrasing), the hybrid runner is expected to involve Cursor before it finalizes the answer; if OpenClaw returns without doing so, `andrea_sync` escalates to Cursor directly to honor that request
- For the most aggressive one-hour collaboration experiment, use [ANDREA_TELEGRAM_TRI_LLM_SPRINT.md](ANDREA_TELEGRAM_TRI_LLM_SPRINT.md)

## Refresh / restart checklist

1. `git pull --ff-only origin main`
2. `bash scripts/test_integration.sh` (optional but recommended)
3. Start **`andrea_sync`** (same `ANDREA_SYNC_*` env as before)
4. If using a quick tunnel: start **`cloudflared`** again → **new URL**
5. Run **`set-webhook`** again with the new `ANDREA_SYNC_PUBLIC_BASE` (or `tunnel-and-webhook`)
6. `python3 scripts/andrea_lockstep_telegram_e2e.py webhook-info` — confirm no delivery errors (or let the server reclaim the webhook automatically if `ANDREA_SYNC_PUBLIC_BASE` is set)
7. Send a test message and wait for both the lockstep row and the Telegram ACK/final reply
   - lightweight assistant turn: should stay direct Andrea
   - productivity / hybrid-skill turn: should show the OpenClaw lane
   - repo-heavy turn: should queue through OpenClaw first and only mention Cursor if OpenClaw escalates
8. Optional: `ANDREA_SYNC_DOCTOR=1 ANDREA_SYNC_URL=http://127.0.0.1:8765 bash scripts/andrea_doctor.sh`

## Operational notes

- **403** on webhook: `?secret=` must equal `ANDREA_SYNC_TELEGRAM_SECRET` exactly.
- **Stable hostname**: quick tunnels rotate; for a fixed URL use a **named Cloudflare tunnel** or your own TLS host, then install `scripts/macos/install_andrea_launchagents.sh --with-cloudflared --load`.
- **Runtime persistence**: the sync LaunchAgent sources repo `.env` plus `~/andrea-lockstep.env`, so `TELEGRAM_BOT_TOKEN`, `CURSOR_API_KEY`, and lockstep secrets survive login.
- **Webhook self-heal**: if `ANDREA_SYNC_PUBLIC_BASE` is set, the running server checks Telegram webhook registration and re-applies it when another process clears it.

## Reference

- [ANDREA_LOCKSTEP_ARCHITECTURE.md](ANDREA_LOCKSTEP_ARCHITECTURE.md)
- [ANDREA_OPERATIONS_PLAYBOOK.md](ANDREA_OPERATIONS_PLAYBOOK.md) §10

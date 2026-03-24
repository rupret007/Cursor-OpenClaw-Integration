# Deployment: `main` as the stable baseline

## Branch strategy

- **`main`** is the **default deployment branch**. Clone or pull `main` for the current production-style integration toolkit.
- Feature work may land on short-lived branches and is merged into `main` via pull request after tests pass.

## Requirements

- **Python 3.10+** (stdlib only; no pip packages required for the CLIs)
- **macOS** recommended for OpenClaw + local Cursor CLI workflows
- **Cursor User API key** from [Cursor settings](https://cursor.com/settings) (used with the [Cloud Agents API](https://cursor.com/docs/cloud-agent/api/endpoints))
- **Git** and network access to `https://api.cursor.com`

## Install (from `main`)

```bash
git clone https://github.com/rupret007/Cursor-OpenClaw-Integration.git
cd Cursor-OpenClaw-Integration
git checkout main
git pull origin main
```

**Guided credentials + optional OpenClaw skill:** run `bash scripts/setup_admin.sh` (writes `./.env`, gitignored). Or copy [.env.example](../.env.example) manually and fill values — still do not commit secrets.

## Environment

| Variable | Required | Description |
|----------|----------|-------------|
| `CURSOR_API_KEY` | Yes (for live API) | Cursor API key (export in shell and/or put in repo-root `.env`; `cursor_openclaw.py` and `cursor_handoff.py` auto-load `.env` without overriding existing exports) |
| `CURSOR_BASE_URL` | No | Default `https://api.cursor.com`; must be `http://` or `https://` if set |
| `CURSOR_AUTH_MODE` | No | `auto` (default), `basic`, or `bearer` |
| `OPENAI_API_KEY` | No | Optional OpenAI API key (not ChatGPT Plus). Never required for Cursor Cloud Agents. |
| `OPENAI_API_ENABLED` | No | `1` / `true` / `yes` (case-insensitive) or integrations that gate on this flag will ignore the key. Set via `bash scripts/setup_admin.sh`, `.env`, or `python3 scripts/dotenv_set_key.py OPENAI_API_KEY --enable-openai --skill`. See [OPENCLAW_SKILL.md](OPENCLAW_SKILL.md) (OpenAI section). |
| `GH_TOKEN` / `GITHUB_TOKEN` | No | Optional GitHub token for OpenClaw GitHub skills if env auth is used. |
| `GEMINI_API_KEY` | No | Optional Gemini key for Gemini skills/CLI. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | No | Optional Telegram bot credentials (skill/plugin dependent). |
| `ANDREA_SYNC_ALEXA_EDGE_TOKEN` | No | Recommended for Alexa rollout; shared secret the public Alexa edge forwards to local `/v1/alexa`. |
| `ANDREA_SYNC_ALEXA_SUMMARY_TO_TELEGRAM` / `ANDREA_SYNC_ALEXA_SUMMARY_CHAT_ID` | No | Alexa session summary controls; summary mirroring is on by default and can target a dedicated Telegram chat. |
| `ANDREA_SYNC_DELEGATED_EXECUTION_ENABLED` | No | Global kill switch for delegated Alexa/OpenClaw/Cursor execution (`1` by default). |
| `ANDREA_SYNC_PROACTIVE_SWEEP_ENABLED` / `ANDREA_SYNC_PROACTIVE_SWEEP_INTERVAL_SECONDS` | No | Enable the background reminder sweep and control how frequently due reminders are delivered. |
| `ANDREA_CURSOR_REPO` / `ANDREA_CURSOR_HANDOFF_MODE` | No | Default repo path and Cursor handoff mode used by the lockstep server for Telegram-triggered execution. |
| `ANDREA_SYNC_CURSOR_REPO` | No | Override repo path used by admin/autonomy helpers such as the local self-heal runner. |
| `ANDREA_SELF_HEAL_CURSOR_MODE` | No | Cursor backend override for the local auto-heal branch-prep flow (`auto`, `api`, `cli`). |
| `ANDREA_REPAIR_POST_CURSOR_VERIFY` | No | Default on: after repair Cursor handoff, verify the branch in a detached worktree before **`resolved`**. |
| `ANDREA_SELF_HEAL_POST_CURSOR_VERIFY` | No | When set, overrides verify for **optimizer auto-heal** only; when unset, follows `ANDREA_REPAIR_POST_CURSOR_VERIFY`. **`LOCAL_AUTO_HEAL_COMPLETED`** requires verification to pass. |
| `ANDREA_CURSOR_PLAN_FIRST_ENABLED` / `ANDREA_*_CURSOR_PLAN_FIRST` / planner+executor model vars | No | Optional two-pass Cursor (planner then executor) for repair, self-heal, and Telegram; see [ANDREA_LOCKSTEP_ARCHITECTURE.md](ANDREA_LOCKSTEP_ARCHITECTURE.md). |
| `ANDREA_SYNC_PUBLIC_BASE` | No | Public HTTPS origin for Telegram webhook self-heal and reboot-stable ingress. Required for persistent Telegram webhook recovery. |
| `CLOUDFLARED_TUNNEL_TOKEN` | No | Named Cloudflare tunnel token for reboot-stable `cloudflared` LaunchAgent startup. |
| `ANDREA_LOCALTUNNEL_SUBDOMAIN` | No | Fallback stable-ish localtunnel subdomain for hosts that do not have `cloudflared` available. |
| `BRAVE_SEARCH_API_KEY` / `BRAVE_ANSWERS_API_KEY` | No | Optional Brave Search keys (Brave skill expects these exact names). |
| `MINIMAX_API_KEY` | No | Optional MiniMax key for MiniMax integrations. |
| `SSL_CERT_FILE` | Sometimes on macOS | If Python reports `CERTIFICATE_VERIFY_FAILED`, set to certifi bundle (see README troubleshooting) |

Never commit `.env` or paste keys into chat logs.

## Verify after deploy

```bash
export CURSOR_API_KEY="..."   # use read -s in real use
python3 scripts/cursor_openclaw.py --json diagnose
python3 scripts/cursor_openclaw.py --json whoami
bash scripts/andrea_services.sh status all
python3 scripts/andrea_capabilities.py        # Andrea readiness snapshot
bash scripts/andrea_reliability_probes.sh       # deterministic probes (+ optional RUN_LIVE_PROBES=1)
bash scripts/test_integration.sh
bash skills/cursor_handoff/scripts/test_handoff.sh
# Optional: one closed-loop local autonomy pass after the stack is healthy
# export ANDREA_SYNC_URL='http://127.0.0.1:8765'
# export ANDREA_SYNC_INTERNAL_TOKEN='...'
# ANDREA_AUTONOMY_AUTO_APPLY_READY=0 bash scripts/andrea_autonomy_cycle.sh
```

**Andrea operator docs:** [ANDREA_OPERATIONS_PLAYBOOK.md](ANDREA_OPERATIONS_PLAYBOOK.md).

**Alexa rollout docs:** [ANDREA_ALEXA_USER_SETUP.md](ANDREA_ALEXA_USER_SETUP.md), [ANDREA_ALEXA_INTEGRATION.md](ANDREA_ALEXA_INTEGRATION.md), and [ALEXA_CLOUD_EDGE_TEMPLATE.md](ALEXA_CLOUD_EDGE_TEMPLATE.md).

## OpenClaw gateway

**Sync from this repo (typical):** from the repository root after `git pull`:

```bash
cp -R skills/cursor_handoff ~/.openclaw/workspace/skills/
openclaw gateway restart
openclaw skills list   # expect cursor_handoff ready if skill is installed in workspace
```

Optional clean replace if the skill had files removed upstream: `rm -rf ~/.openclaw/workspace/skills/cursor_handoff` then the same `cp -R`.

The **canonical skill copy** for day-to-day OpenClaw may live under `~/.openclaw/workspace/skills/cursor_handoff/`. This repository includes a **mirror** under `skills/cursor_handoff/` for version control and CI.

## Reboot-ready macOS startup

For a reboot-stable local operator setup, use:

- `andrea_sync` LaunchAgent
- named `cloudflared` tunnel LaunchAgent
- post-login Andrea bootstrap LaunchAgent

The bootstrap step waits for `andrea_sync`, syncs the repo `cursor_handoff` skill into the OpenClaw workspace, restarts the OpenClaw gateway, publishes a fresh capability snapshot, and re-asserts the Telegram webhook when the required env is present.

Example:

```bash
cd /path/to/Cursor-OpenClaw-Integration
export CLOUDFLARED_TUNNEL_TOKEN='...'
bash scripts/andrea_services.sh install-launchagents --with-cloudflared --load
```

Fallback when `cloudflared` is not available on the host:

```bash
cd /path/to/Cursor-OpenClaw-Integration
export ANDREA_LOCALTUNNEL_SUBDOMAIN='fine-monkeys-shake'
bash scripts/andrea_services.sh install-launchagents --with-localtunnel --load
```

Operator controls after install:

```bash
bash scripts/andrea_services.sh status all
bash scripts/andrea_services.sh restart all
bash scripts/andrea_services.sh bootstrap
```

Recommended env location for persistent secrets/runtime:

- repo `.env` for project-scoped values
- `~/andrea-lockstep.env` for machine-local overrides

Key reboot-ready variables:

- `TELEGRAM_BOT_TOKEN`
- `ANDREA_SYNC_INTERNAL_TOKEN`
- `ANDREA_SYNC_PUBLIC_BASE`
- `CLOUDFLARED_TUNNEL_TOKEN`
- `CURSOR_API_KEY`
- `OPENAI_API_KEY` with `OPENAI_API_ENABLED=1` when you want memory-aware direct replies to use OpenAI

The optional `--with-openclaw-refresh` LaunchAgent is now treated as legacy compatibility only. The normal login path is `andrea_sync` + tunnel + post-login bootstrap, and duplicate gateway restarts are debounced automatically if both paths exist.

## GitHub authentication (push / CI)

Use HTTPS with a Personal Access Token stored in the macOS keychain, or SSH keys. Do not embed tokens in repository files.

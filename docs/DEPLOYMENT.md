# Deployment model

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
python3 scripts/andrea_capabilities.py        # Andrea readiness snapshot
bash scripts/andrea_reliability_probes.sh       # deterministic probes (+ optional RUN_LIVE_PROBES=1)
bash scripts/test_integration.sh
bash skills/cursor_handoff/scripts/test_handoff.sh
```

**Andrea operator docs:** [ANDREA_OPERATIONS_PLAYBOOK.md](ANDREA_OPERATIONS_PLAYBOOK.md).

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
bash scripts/macos/install_andrea_launchagents.sh --with-cloudflared --load
```

Fallback when `cloudflared` is not available on the host:

```bash
cd /path/to/Cursor-OpenClaw-Integration
export ANDREA_LOCALTUNNEL_SUBDOMAIN='fine-monkeys-shake'
bash scripts/macos/install_andrea_launchagents.sh --with-localtunnel --load
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

## GitHub authentication (push / CI)

Use HTTPS with a Personal Access Token stored in the macOS keychain, or SSH keys. Do not embed tokens in repository files.

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
| `OPENAI_API_KEY` | No | Optional OpenAI API key (not ChatGPT Plus). Stored for optional/future features; never required for Cursor Cloud Agents. |
| `OPENAI_API_ENABLED` | No | `1` / `true` / `yes` (case-insensitive) to allow use of `OPENAI_API_KEY` when implemented; otherwise off. Set via `bash scripts/setup_admin.sh` or `.env`. |
| `SSL_CERT_FILE` | Sometimes on macOS | If Python reports `CERTIFICATE_VERIFY_FAILED`, set to certifi bundle (see README troubleshooting) |

Never commit `.env` or paste keys into chat logs.

## Verify after deploy

```bash
export CURSOR_API_KEY="..."   # use read -s in real use
python3 scripts/cursor_openclaw.py --json diagnose
python3 scripts/cursor_openclaw.py --json whoami
bash scripts/test_integration.sh
bash skills/cursor_handoff/scripts/test_handoff.sh
```

## OpenClaw gateway

**Sync from this repo (typical):** from the repository root after `git pull`:

```bash
cp -R skills/cursor_handoff ~/.openclaw/workspace/skills/
openclaw gateway restart
openclaw skills list   # expect cursor_handoff ready if skill is installed in workspace
```

Optional clean replace if the skill had files removed upstream: `rm -rf ~/.openclaw/workspace/skills/cursor_handoff` then the same `cp -R`.

The **canonical skill copy** for day-to-day OpenClaw may live under `~/.openclaw/workspace/skills/cursor_handoff/`. This repository includes a **mirror** under `skills/cursor_handoff/` for version control and CI.

## GitHub authentication (push / CI)

Use HTTPS with a Personal Access Token stored in the macOS keychain, or SSH keys. Do not embed tokens in repository files.

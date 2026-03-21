# Cursor-OpenClaw-Integration

Hardened **Cursor Cloud Agents** integration toolkit for **OpenClaw** and shell workflows.

**Deployment:** use the **`main`** branch as the default production-style baseline. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Contents

- [What this repository provides](#what-this-repository-provides)
- [Repository layout](#repository-layout)
- [Admin setup (guided)](#admin-setup-guided)
- [Quick start](#quick-start)
- [OpenClaw skill (`cursor_handoff`)](#openclaw-skill-cursor_handoff)
- [Documentation](#documentation)
- [CLI reference](#cli-reference)
- [Testing](#testing)
- [Environment variables](#environment-variables)
- [Security](#security)
- [Troubleshooting](#troubleshooting)

## What this repository provides

- **CLI** (`scripts/cursor_openclaw.py`) for Cursor Cloud Agents API operations:
  - Auth / health: `diagnose`, `whoami`, `models`
  - Agent lifecycle: `create-agent`, `list-agents`, `agent-status`, `followup`, `stop-agent`, `delete-agent`
  - Insights: `conversation`, `artifacts`, `artifact-download-url`
- **Auth fallback:** `--auth-mode auto` tries bearer then basic when the API or docs disagree.
- **Resilience:** retries with backoff on `429`, `5xx`, and transient **network/SSL** failures.
- **Unicode:** request JSON uses UTF-8 (`ensure_ascii=False`) so prompts stay readable end-to-end.
- **OpenClaw skill** (`skills/cursor_handoff/`): API-first handoff with CLI fallback, diagnostics, dry-run, tests.

## Repository layout

```text
.
├── LICENSE
├── .env.example
├── README.md
├── docs/
│   ├── DEPLOYMENT.md          # main branch, env, gateway, verify
│   ├── OPENCLAW_SKILL.md      # install skill, typical flows
│   └── CLI_REFERENCE.md       # flags and subcommands
├── openclaw-cursor-integration-proposal.md
├── openclaw-cursor-integration-roadmap.md
├── scripts/
│   ├── cursor_openclaw.py
│   ├── cursor_api_common.py # shared validation, HTTP helpers (mirrored under skills)
│   ├── env_loader.py        # auto-load .env (used by CLIs)
│   ├── setup_admin.sh       # interactive .env + optional OpenClaw skill install
│   ├── exhaustive_feature_check.sh  # offline sweep of both CLIs (+ optional live API)
│   └── test_integration.sh
├── skills/
│   └── cursor_handoff/        # vendored skill (sync to ~/.openclaw/workspace/skills/)
│       ├── SKILL.md
│       ├── .env.example
│       ├── scripts/         # includes env_loader.py, cursor_api_common.py (mirror)
│       └── tests/
└── tests/
    ├── test_cursor_openclaw.py
    ├── test_cursor_api_common.py
    └── test_env_loader.py
```

## Admin setup (guided)

For a new machine or operator, run the interactive wizard (writes a **local** `.env`, mode `600`, ignored by git; never commit it):

```bash
bash scripts/setup_admin.sh
```

**Non-interactive (e.g. your own terminal, key already exported):** writes `.env`, syncs skill, restarts gateway, runs `diagnose`. Refuses to overwrite `./.env` unless you pass **`--force`**.

Use your real key; **paste only the commands** (not prose or `# …` comment lines from chat), or zsh may error.

```bash
export CURSOR_API_KEY="…"
# Optional batch-only:
# export OPENAI_API_KEY="…"
# export OPENAI_API_ENABLED=1   # or true | yes
```

```bash
bash scripts/setup_admin.sh --batch
```

If `./.env` already exists and you want to replace it:

```bash
bash scripts/setup_admin.sh --batch --force
```

It will:

- Prompt for **CURSOR_API_KEY** (hidden input) and optional **CURSOR_BASE_URL** / **CURSOR_AUTH_MODE**
- Optional **CURSOR_EMAIL** and **OPENCLAW_CURSOR_DEFAULT_MODE** (`auto` \| `api` \| `cli`) for the handoff skill
- Optional **OPENAI_API_KEY** (hidden) and **OPENAI_API_ENABLED** (`[y/N]`; no key forces disabled)
- Write **`./.env`** with `set -a && source .env && set +a` usage hints
- Optionally install **`cursor_handoff`** under `~/.openclaw/workspace/skills/` (replaces that folder if present), write **`~/.openclaw/workspace/skills/cursor_handoff/.env`**, restart **`openclaw gateway`**, and run **`diagnose`**

The CLIs read the **process environment**. They also **auto-load** a repo-root `.env` (and the skill directory `.env` for `cursor_handoff`) if present, **without** overriding variables you already exported.

Optional: load the same file in your shell:

```bash
cd /path/to/Cursor-OpenClaw-Integration
set -a && source .env && set +a
```

## Quick start

### 1. Clone and use `main`

```bash
git clone https://github.com/rupret007/Cursor-OpenClaw-Integration.git
cd Cursor-OpenClaw-Integration
git checkout main && git pull origin main
```

### 2. Set your API key (do not commit)

**Easiest:** [Admin setup (guided)](#admin-setup-guided) — `bash scripts/setup_admin.sh`.

**Manual:**

```bash
read -s "CURSOR_API_KEY?Paste Cursor API key: "
echo
export CURSOR_API_KEY
```

If you only assign the variable without `export`, child processes (including Python) will not see it. If you use a `.env` file, the CLIs load it automatically from the repo (or skill) root; you can still `source .env` in the shell if you want non-Python tools to see the same variables.

### 3. Diagnostics

```bash
python3 scripts/cursor_openclaw.py --json diagnose
python3 scripts/cursor_openclaw.py --json whoami
python3 scripts/cursor_openclaw.py --json models
```

### 4. Launch an agent (example)

```bash
python3 scripts/cursor_openclaw.py --json create-agent \
  --prompt "Read-only audit of top 5 risks" \
  --repository "https://github.com/owner/repo" \
  --ref main \
  --branch-name "cursor/risk-audit" \
  --auto-create-pr false \
  --poll-attempts 3
```

More examples are in [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md).

## OpenClaw skill (`cursor_handoff`)

**Daily sync (recommended):** from your clone of this repo (ideally `main`), copy the skill into the OpenClaw workspace and restart the gateway so changes load:

```bash
cp -R skills/cursor_handoff ~/.openclaw/workspace/skills/
openclaw gateway restart
openclaw skills list   # expect cursor_handoff ready
```

That flow is enough for normal updates. If you ever remove or rename files inside the skill in git, you can do a clean replace first (`rm -rf ~/.openclaw/workspace/skills/cursor_handoff`) and then the same `cp -R` — either approach works; `cp -R` alone is fine day to day.

First-time install: ensure the directory exists — `mkdir -p ~/.openclaw/workspace/skills` — then use the same `cp -R` line.

Full steps and flow: [docs/OPENCLAW_SKILL.md](docs/OPENCLAW_SKILL.md).

## Documentation

| Doc | Purpose |
|-----|---------|
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | **`main` as deployment branch**, requirements, verify, gateway |
| [docs/OPENCLAW_SKILL.md](docs/OPENCLAW_SKILL.md) | Skill install, typical OpenClaw → Cursor flow |
| [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md) | Flags and subcommands for both CLIs |
| [openclaw-cursor-integration-roadmap.md](openclaw-cursor-integration-roadmap.md) | Phased integration plan |
| [openclaw-cursor-integration-proposal.md](openclaw-cursor-integration-proposal.md) | Design notes / ideas |

## CLI reference

See [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md) for full tables. Cursor API: [Cloud Agents API](https://cursor.com/docs/cloud-agent/api/endpoints).

## Testing

**Integration CLI + skill (recommended):**

```bash
bash scripts/test_integration.sh
bash skills/cursor_handoff/scripts/test_handoff.sh
```

`test_integration.sh` ends with **`scripts/exhaustive_feature_check.sh`** (every subcommand `--help`, validation paths, handoff diagnose/dry-run modes). Optional live API smoke:

```bash
RUN_LIVE_API=1 bash scripts/exhaustive_feature_check.sh
```

**Overnight / soak:** safe to loop `RUN_LIVE_API=1 bash scripts/exhaustive_feature_check.sh` or your own agent workflows; avoid high `list-agents` limits or tight polling against production so you don’t hit rate limits.

**Exit codes (`cursor_openclaw.py`):** `0` success, `2` usage/validation error, `4` HTTP/API failure.

**Unit tests only:**

```bash
python3 -m py_compile scripts/cursor_openclaw.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m unittest discover -s skills/cursor_handoff/tests -p 'test_*.py' -v
```

## Environment variables

See [.env.example](.env.example) and [skills/cursor_handoff/.env.example](skills/cursor_handoff/.env.example).

| Variable | Required | Notes |
|----------|----------|--------|
| `CURSOR_API_KEY` | For live API | Export in the shell that runs Python |
| `CURSOR_BASE_URL` | No | Default `https://api.cursor.com`; if set, must start with `http://` or `https://` |
| `CURSOR_AUTH_MODE` | No | `auto`, `basic`, `bearer` |
| `OPENAI_API_KEY` | No | Optional; API key from [OpenAI platform](https://platform.openai.com/). Does **not** use ChatGPT Plus — use an API key with billing enabled. |
| `OPENAI_API_ENABLED` | No | When `1`, `true`, or `yes` (case-insensitive), future OpenAI features may use `OPENAI_API_KEY`. Otherwise the key is stored but ignored. `bash scripts/setup_admin.sh` can set both. |

## Security

- Never commit API keys or paste them into assistant chats.
- `diagnose` redacts Cursor and OpenAI keys by default; avoid `--show-key` in shared logs.
- Prefer short-lived keys and rotate if exposed.
- Treat agent outputs and artifact URLs as sensitive until reviewed.

## Troubleshooting

| Symptom | What to try |
|---------|----------------|
| `CURSOR_API_KEY missing` in Python | Use `export CURSOR_API_KEY=...`, or create `./.env` (wizard writes one; CLIs auto-load it if the key is not already set in the environment). |
| `401 Unauthorized` | Wrong key type or revoked key; confirm key in Cursor settings. |
| `CERTIFICATE_VERIFY_FAILED` (Python) | On macOS, try `export SSL_CERT_FILE="$(python3 -c 'import certifi; print(certifi.where())')"` if `certifi` is installed. |
| Skill not listed after copy | `openclaw gateway restart`; confirm path `~/.openclaw/workspace/skills/cursor_handoff/SKILL.md`. |
| zsh: `command not found: #` / `no matches found` after paste | You pasted comment lines or broken lines into the shell. Run commands one at a time; avoid copying `#` comment lines from docs or chat. |
| `create-agent` validation errors | Use `--dry-run` first; use **either** `--repository` or `--pr-url`, not both; check `--ref` / `--branch-name` per API docs. |
| `Invalid --id format` | Pass only the agent id (e.g. `bc-…`), not a full URL. Allowed characters: letters, digits, `._:-`. |
| `Base URL must start with http:// or https://` | Fix `CURSOR_BASE_URL` / `--base-url` (no `ftp://`, bare hostnames, etc.). |

## Hardening details (summary)

- `--auth-mode auto` tolerates bearer vs basic inconsistencies.
- `--retries` + exponential backoff reduce transient failures (including transport-layer errors).
- `diagnose` redacts secrets.
- `create-agent --dry-run` validates payload without network calls.
- `cursor_handoff` supports `--dry-run` and read-only defaults for safer delegation.

## License

[MIT](LICENSE).

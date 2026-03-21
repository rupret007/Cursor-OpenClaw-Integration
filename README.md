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
│   ├── CLI_REFERENCE.md       # flags and subcommands
│   └── ANDREA_*.md            # Andrea max-autonomy: matrix, policy, runbooks, playbook
├── openclaw-cursor-integration-proposal.md
├── openclaw-cursor-integration-roadmap.md
├── scripts/
│   ├── cursor_openclaw.py
│   ├── cursor_api_common.py # shared validation, HTTP helpers (mirrored under skills)
│   ├── env_loader.py        # auto-load .env (used by CLIs)
│   ├── setup_admin.sh       # interactive .env + optional OpenClaw skill install
│   ├── exhaustive_feature_check.sh  # offline sweep of both CLIs (+ optional live API)
│   ├── andrea_capabilities.py      # Andrea runtime capability matrix (live readiness)
│   ├── andrea_readiness_grade.py   # A/B/C grade from capability JSON
│   ├── andrea_security_sanity.sh     # repo secret-pattern sanity checks
│   ├── andrea_slo_check.sh         # grade + optional OpenClaw model probe
│   ├── andrea_doctor.sh            # one-pass: security + grade + probes + probe
│   ├── andrea_model_guard.sh       # automatic profile failover + reprobe loop
│   ├── andrea_openclaw_enforce.sh  # sync skill + required skills + probe/guard
│   ├── andrea_release_gate.sh      # STRICT security + grade not C + test_integration
│   ├── andrea_slo_telegram.sh      # timed Telegram getMe SLO (token from env only)
│   ├── handoff_context.py          # shared intent templates + repo triage text
│   ├── andrea_reliability_probes.sh # deterministic probes + capability snapshot
│   ├── dotenv_set_key.py     # merge one .env key without full wizard overwrite
│   ├── openclaw_apply_openai_key.sh  # openclaw onboard --openai-api-key from .env
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
# export GH_TOKEN="…"
# export GEMINI_API_KEY="…"
# export TELEGRAM_BOT_TOKEN="…"
# export TELEGRAM_CHAT_ID="…"
# export BRAVE_SEARCH_API_KEY="…"
# export BRAVE_ANSWERS_API_KEY="…"
# export MINIMAX_API_KEY="…"
# export SSL_CERT_FILE="…"   # optional TLS CA bundle path
```

```bash
bash scripts/setup_admin.sh --batch
```

If `./.env` already exists and you want to replace it:

```bash
bash scripts/setup_admin.sh --batch --force
```

**Persist a single secret without re-running the full wizard** (merges into `./.env`, keeps other keys; sets both `GH_TOKEN` and `GITHUB_TOKEN` by default):

```bash
python3 scripts/dotenv_set_key.py GH_TOKEN --skill
# hidden prompt on TTY, or:  python3 scripts/dotenv_set_key.py GH_TOKEN --value "$GH_TOKEN" --skill
```

**OpenAI (platform API key + enable flag for this repo’s CLIs/skills):**

```bash
python3 scripts/dotenv_set_key.py OPENAI_API_KEY --enable-openai --skill
openclaw gateway restart
```

See [docs/OPENCLAW_SKILL.md](docs/OPENCLAW_SKILL.md) for how `OPENAI_API_ENABLED` gates usage and how this relates to OpenClaw’s own provider settings.

If OpenClaw rejects the key, see **[docs/OPENCLAW_OPENAI_TROUBLESHOOTING.md](docs/OPENCLAW_OPENAI_TROUBLESHOOTING.md)** and run **`bash scripts/openclaw_apply_openai_key.sh`** (uses `openclaw onboard --openai-api-key` per upstream docs).

It will:

- Prompt for **CURSOR_API_KEY** (hidden input) and optional **CURSOR_BASE_URL** / **CURSOR_AUTH_MODE**
- Optional **CURSOR_EMAIL** and **OPENCLAW_CURSOR_DEFAULT_MODE** (`auto` \| `api` \| `cli`) for the handoff skill
- Optional **OPENAI_API_KEY** (hidden) and **OPENAI_API_ENABLED** (`[y/N]`; no key forces disabled)
- **Optional integrations block** (`[Y/n]`): skip entirely for Cursor-only setups, or enter **GH_TOKEN** (also writes **GITHUB_TOKEN**), **GEMINI_API_KEY**, Telegram bot + chat id, Brave keys, **MINIMAX_API_KEY**, **SSL_CERT_FILE** — each field skippable with Enter
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

## Andrea (max-autonomy operator)

**Andrea** is the hardened operator profile for this stack: capability baseline, execute-first policy, DevOps/Telegram/productivity runbooks, and reliability probes.

| Doc | Purpose |
|-----|---------|
| [docs/ANDREA_OPERATIONS_PLAYBOOK.md](docs/ANDREA_OPERATIONS_PLAYBOOK.md) | **Start here** — autonomy scope, verification, recovery |
| [docs/ANDREA_CAPABILITY_MATRIX.md](docs/ANDREA_CAPABILITY_MATRIX.md) | Live readiness matrix (`scripts/andrea_capabilities.py`) |
| [docs/ANDREA_AUTONOMY_POLICY.md](docs/ANDREA_AUTONOMY_POLICY.md) | Execute-first + confirm-required boundaries |
| [docs/ANDREA_DEVOPS_RUNBOOK.md](docs/ANDREA_DEVOPS_RUNBOOK.md) | Task → branch → test → PR + GitHub fallbacks |
| [docs/ANDREA_COMMS_PRODUCTIVITY.md](docs/ANDREA_COMMS_PRODUCTIVITY.md) | Telegram + productivity routines |
| [docs/ANDREA_READINESS_REPORT.md](docs/ANDREA_READINESS_REPORT.md) | Readiness template / sign-off |
| [docs/ANDREA_SECURITY.md](docs/ANDREA_SECURITY.md) | Secrets, redaction, gateway token, rotation |
| [docs/ANDREA_MODEL_POLICY.md](docs/ANDREA_MODEL_POLICY.md) | Model profiles + fallbacks + rate limits |
| [docs/ANDREA_LOCKSTEP_ARCHITECTURE.md](docs/ANDREA_LOCKSTEP_ARCHITECTURE.md) | Telegram / Alexa / Cursor shared lockstep bus + SQLite store |
| [docs/ANDREA_TELEGRAM_LOCKSTEP_E2E.md](docs/ANDREA_TELEGRAM_LOCKSTEP_E2E.md) | Telegram webhook + `cloudflared` + `scripts/andrea_lockstep_telegram_e2e.py` |
| [docs/ANDREA_LOCKSTEP_REVIEW_FINDINGS.md](docs/ANDREA_LOCKSTEP_REVIEW_FINDINGS.md) | Lockstep awareness / kill-switch / webhook review notes |
| [docs/ANDREA_ALEXA_INTEGRATION.md](docs/ANDREA_ALEXA_INTEGRATION.md) | Alexa Custom Skill HTTPS endpoint notes |

**Startup self-check:**

```bash
python3 scripts/andrea_capabilities.py
```

**Masterclass health (one command):** security sanity + capability snapshot + **A/B/C** grade + reliability probes + optional `openclaw models status --probe`. OpenClaw **`--probe-timeout` is in milliseconds** (e.g. 30s → `30000`).

```bash
bash scripts/andrea_doctor.sh
# SKIP_OPENCLAW_PROBE=1 bash scripts/andrea_doctor.sh
# STRICT_SECURITY=1 bash scripts/andrea_doctor.sh   # fail on backup warnings too
# MODEL_GUARD_ON_FAIL=1 bash scripts/andrea_doctor.sh  # auto-remediate failed model probe
# OPENCLAW_ENFORCE=1 bash scripts/andrea_doctor.sh  # enforce OpenClaw baseline first
```

**Model guard** (explicit remediation loop across profiles):

```bash
bash scripts/andrea_model_guard.sh
# dry-run:
# bash scripts/andrea_model_guard.sh --dry-run
```

**OpenClaw baseline enforcer** (skill sync + required skills + model probe):

```bash
bash scripts/andrea_openclaw_enforce.sh
# dry-run:
# bash scripts/andrea_openclaw_enforce.sh --dry-run
```

**SLO gate** (grade + probe):

```bash
bash scripts/andrea_slo_check.sh
# Optional Telegram latency (needs TELEGRAM_BOT_TOKEN): TELEGRAM_SLO=1 bash scripts/andrea_slo_check.sh
```

**Pre-release gate** (strict security warnings fail, readiness not Grade C, full integration):

```bash
bash scripts/andrea_release_gate.sh
```

**Cursor handoff intents** (same templates as `create-agent --intent`): `code-review`, `refactor`, `release-notes`, `brief`; optional `--triage` on local repo — see [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md) and `skills/cursor_handoff/SKILL.md`.

**Reliability probes** (deterministic env for `diagnose` + JSON shape checks):

```bash
bash scripts/andrea_reliability_probes.sh
# optional: RUN_LIVE_PROBES=1 for gh + openclaw
```

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

`test_integration.sh` runs **`scripts/andrea_security_sanity.sh`**, **`scripts/andrea_reliability_probes.sh`**, a non-fatal **`andrea_readiness_grade.py`** smoke, then **`scripts/exhaustive_feature_check.sh`** (every subcommand `--help`, validation paths, handoff diagnose/dry-run modes). Optional live API smoke:

```bash
RUN_LIVE_API=1 bash scripts/exhaustive_feature_check.sh
```

**Live operator cycle** (needs `andrea_sync` running + `ANDREA_SYNC_INTERNAL_TOKEN`):

```bash
export ANDREA_SYNC_INTERNAL_TOKEN='…'
export ANDREA_SYNC_URL='http://127.0.0.1:8765'
bash scripts/andrea_full_cycle.sh
```

See [docs/ANDREA_OPERATIONS_PLAYBOOK.md](docs/ANDREA_OPERATIONS_PLAYBOOK.md) for skip flags (`SKIP_GIT`, `SKIP_KILL_DRILL`, etc.).

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
| `OPENAI_API_ENABLED` | No | When `1`, `true`, or `yes` (case-insensitive), code paths that respect this flag may use `OPENAI_API_KEY`; otherwise the key is ignored. Use `bash scripts/setup_admin.sh` or `python3 scripts/dotenv_set_key.py OPENAI_API_KEY --enable-openai --skill`. See [docs/OPENCLAW_SKILL.md](docs/OPENCLAW_SKILL.md). |
| `GH_TOKEN` / `GITHUB_TOKEN` | No | Optional GitHub token for `gh`/GitHub-related OpenClaw skills (if they use env-based auth). |
| `GEMINI_API_KEY` | No | Optional Gemini key for Gemini skills/CLIs. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | No | Optional Telegram bot credentials (skill-dependent). |
| `BRAVE_SEARCH_API_KEY` / `BRAVE_ANSWERS_API_KEY` | No | Optional Brave Search skill keys (`brave-api-search` expects both names; answers key may reuse search key). |
| `MINIMAX_API_KEY` | No | Optional MiniMax provider key for MiniMax integrations. |
| `SSL_CERT_FILE` | No | Optional path to CA bundle for Python TLS (macOS `CERTIFICATE_VERIFY_FAILED`); see README troubleshooting. |

## Security

- Never commit API keys or paste them into assistant chats.
- Full operator checklist: **[docs/ANDREA_SECURITY.md](docs/ANDREA_SECURITY.md)** — env-first secrets, redaction-safe diagnostics, gateway token rotation, `bash scripts/andrea_security_sanity.sh`.
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

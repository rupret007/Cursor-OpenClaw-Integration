# Cursor-OpenClaw-Integration

Hardened **Cursor Cloud Agents** integration toolkit for **OpenClaw** and shell workflows.

**Deployment:** use the **`main`** branch as the default production-style baseline. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Contents

- [What this repository provides](#what-this-repository-provides)
- [Repository layout](#repository-layout)
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
- **Resilience:** retries with backoff on `429` and `5xx`.
- **OpenClaw skill** (`skills/cursor_handoff/`): API-first handoff with CLI fallback, diagnostics, dry-run, tests.

## Repository layout

```text
.
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
│   └── test_integration.sh
├── skills/
│   └── cursor_handoff/        # vendored skill (sync to ~/.openclaw/workspace/skills/)
│       ├── SKILL.md
│       ├── .env.example
│       ├── scripts/
│       └── tests/
└── tests/
    └── test_cursor_openclaw.py
```

## Quick start

### 1. Clone and use `main`

```bash
git clone https://github.com/rupret007/Cursor-OpenClaw-Integration.git
cd Cursor-OpenClaw-Integration
git checkout main && git pull origin main
```

### 2. Set your API key (do not commit)

```bash
read -s "CURSOR_API_KEY?Paste Cursor API key: "
echo
export CURSOR_API_KEY
```

If you only assign the variable without `export`, child processes (including Python) will not see it.

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

Install into your OpenClaw workspace and restart the gateway:

```bash
cp -R skills/cursor_handoff ~/.openclaw/workspace/skills/
openclaw gateway restart
openclaw skills list
```

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
| `CURSOR_BASE_URL` | No | Default `https://api.cursor.com` |
| `CURSOR_AUTH_MODE` | No | `auto`, `basic`, `bearer` |

## Security

- Never commit API keys or paste them into assistant chats.
- `diagnose` redacts keys by default; avoid `--show-key` in shared logs.
- Prefer short-lived keys and rotate if exposed.
- Treat agent outputs and artifact URLs as sensitive until reviewed.

## Troubleshooting

| Symptom | What to try |
|---------|----------------|
| `CURSOR_API_KEY missing` in Python | Use `export CURSOR_API_KEY=...` (not only `CURSOR_API_KEY=...` in the same shell). |
| `401 Unauthorized` | Wrong key type or revoked key; confirm key in Cursor settings. |
| `CERTIFICATE_VERIFY_FAILED` (Python) | On macOS, try `export SSL_CERT_FILE="$(python3 -c 'import certifi; print(certifi.where())')"` if `certifi` is installed. |
| Skill not listed after copy | `openclaw gateway restart`; confirm path `~/.openclaw/workspace/skills/cursor_handoff/SKILL.md`. |
| `create-agent` validation errors | Use `--dry-run` first; check `--repository` / `--ref` / `--branch-name` / `--pr-url` per API docs. |

## Hardening details (summary)

- `--auth-mode auto` tolerates bearer vs basic inconsistencies.
- `--retries` + exponential backoff reduce transient failures.
- `diagnose` redacts secrets.
- `create-agent --dry-run` validates payload without network calls.
- `cursor_handoff` supports `--dry-run` and read-only defaults for safer delegation.

## License

See repository default or add a `LICENSE` file if you want an explicit SPDX license on `main`.

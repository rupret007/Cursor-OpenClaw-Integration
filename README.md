# Cursor-OpenClaw-Integration

Hardened Cursor Cloud Agents integration toolkit for OpenClaw workflows.

## What this repository provides

- A production-friendly CLI for Cursor Cloud Agents API operations:
  - auth/health checks (`whoami`, `models`, `diagnose`)
  - agent lifecycle (`create-agent`, `list-agents`, `agent-status`, `followup`, `stop-agent`, `delete-agent`)
  - agent insights (`conversation`, `artifacts`, `artifact-download-url`)
- Built-in auth fallback (`auto` mode tries bearer then basic).
- Retry handling for transient API errors (`429`, `5xx`).
- Extensive local validation scripts and unit tests.

## Layout

```text
.
├── .env.example
├── README.md
├── scripts/
│   ├── cursor_openclaw.py
│   └── test_integration.sh
└── tests/
    └── test_cursor_openclaw.py
```

## Quick start

1. Export your key:

```bash
read -s "CURSOR_API_KEY?Paste Cursor API key: "
echo
export CURSOR_API_KEY
```

2. Run diagnostics:

```bash
python3 scripts/cursor_openclaw.py --json diagnose
python3 scripts/cursor_openclaw.py --json whoami
python3 scripts/cursor_openclaw.py --json models
```

3. Launch an agent:

```bash
python3 scripts/cursor_openclaw.py --json create-agent \
  --prompt "Read-only audit of top 5 risks" \
  --repository "https://github.com/owner/repo" \
  --ref main \
  --branch-name "cursor/risk-audit" \
  --auto-create-pr false \
  --poll-attempts 3
```

## API command examples

List recent agents:

```bash
python3 scripts/cursor_openclaw.py --json list-agents --limit 5
```

Get agent status:

```bash
python3 scripts/cursor_openclaw.py --json agent-status --id bc-xxxxxxxx
```

Send followup:

```bash
python3 scripts/cursor_openclaw.py --json followup \
  --id bc-xxxxxxxx \
  --prompt "Also add a concise test plan."
```

Fetch artifacts:

```bash
python3 scripts/cursor_openclaw.py --json artifacts --id bc-xxxxxxxx
python3 scripts/cursor_openclaw.py --json artifact-download-url \
  --id bc-xxxxxxxx \
  --path "/opt/cursor/artifacts/screenshot.png"
```

## Hardening details

- `--auth-mode auto` (default) handles docs/auth inconsistencies gracefully.
- `--retries` + exponential backoff reduce transient network/API failures.
- `diagnose` redacts keys before output.
- `create-agent --dry-run` validates payload without making network calls.

## Testing

Run full local suite:

```bash
bash scripts/test_integration.sh
```

Or directly:

```bash
python3 -m py_compile scripts/cursor_openclaw.py
python3 -m unittest tests/test_cursor_openclaw.py
```

## Environment variables

See `.env.example`.

Minimum required:

- `CURSOR_API_KEY`

Optional:

- `CURSOR_BASE_URL` (default: `https://api.cursor.com`)
- `CURSOR_AUTH_MODE` (`auto`, `basic`, `bearer`)
- Runtime flags via CLI: timeout, retries, backoff

## OpenClaw usage pattern

This repository is designed to be called by an OpenClaw skill wrapper.
Typical flow:

1. `diagnose`
2. `whoami`
3. `create-agent`
4. `agent-status` polling
5. `conversation` and `artifacts`

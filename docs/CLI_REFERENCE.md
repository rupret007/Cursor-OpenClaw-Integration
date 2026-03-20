# CLI reference — `cursor_openclaw.py`

Global options (before subcommand):

| Flag | Default | Description |
|------|---------|-------------|
| `--base-url` | `https://api.cursor.com` | API base |
| `--auth-mode` | `auto` | `auto`, `bearer`, or `basic` |
| `--timeout-seconds` | `30` | Per-request timeout (> 0) |
| `--retries` | `2` | Retries on 429/5xx (>= 0) |
| `--retry-backoff-seconds` | `0.5` | Exponential backoff base (>= 0) |
| `--json` | off | JSON output |

Subcommands:

| Command | Notes |
|---------|--------|
| `diagnose` | Env summary; optional `--show-key` for redacted preview |
| `whoami` | `GET /v0/me` |
| `models` | `GET /v0/models` |
| `list-agents` | `--limit` 1–100, optional `--cursor`, `--pr-url` |
| `agent-status` | `--id` |
| `conversation` | `--id` |
| `artifacts` | `--id` |
| `artifact-download-url` | `--id`, `--path` |
| `create-agent` | `--prompt`, `--branch-name`, repo **or** `--pr-url`; `--dry-run`, polling flags |
| `followup` | `--id`, `--prompt` |
| `stop-agent` | `--id` |
| `delete-agent` | `--id` |

API contract aligns with [Cursor Cloud Agents API](https://cursor.com/docs/cloud-agent/api/endpoints).

---

# CLI reference — `skills/cursor_handoff/scripts/cursor_handoff.py`

| Flag | Description |
|------|-------------|
| `--repo` | Local path, `https://github.com/...`, or `owner/repo` |
| `--prompt` | Task text (omit only with `--diagnose`) |
| `--read-only` | `true` / `false` |
| `--mode` | `auto`, `api`, `cli` |
| `--branch` | Optional; default generated `openclaw/task-YYYYMMDD-HHMMSS` |
| `--pr-url` | API: `source.prUrl` |
| `--auto-create-pr` etc. | API PR targets |
| `--poll-max-attempts`, `--poll-interval-seconds` | Post-create polling |
| `--timeout-seconds`, `--api-retries`, `--api-retry-backoff-seconds` | API resilience |
| `--diagnose` | No handoff; env + optional `/me` and `/agents?limit=1` |
| `--dry-run` | Validate and show payload; works even if no backend configured (`backend: unavailable`) |

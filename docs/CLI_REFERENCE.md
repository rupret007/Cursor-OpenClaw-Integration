# CLI reference — `cursor_openclaw.py`

Global options (before subcommand):

| Flag | Default | Description |
|------|---------|-------------|
| `--base-url` | `https://api.cursor.com` | API base (**must** be `http://` or `https://`) |
| `--auth-mode` | `auto` | `auto`, `bearer`, or `basic` |
| `--timeout-seconds` | `30` | Per-request timeout (> 0) |
| `--retries` | `2` | Retries on 429/5xx (>= 0) |
| `--retry-backoff-seconds` | `0.5` | Exponential backoff base (>= 0) |
| `--json` | off | JSON output |
| `--version` / `-V` | — | Print version and exit (no subcommand required) |

`--id` values must be plain agent identifiers (letters, digits, `._:-` only) — not URLs — to avoid ambiguous paths.

`create-agent` accepts **either** `--repository` **or** `--pr-url`, not both. Provide **`--prompt` and/or `--intent` and/or `--triage-repo`** (at least one): `--intent` is one of `code-review`, `refactor`, `release-notes`, `brief` (scaffolded task); `--triage-repo` prepends a non-secret repo snapshot (local path).

Subcommands:

| Command | Notes |
|---------|--------|
| `diagnose` | Env summary; optional `--show-key` for redacted Cursor + OpenAI key previews; includes `cli_version`, `dotenv_files_loaded`, `openai_api_key_present`, `openai_api_enabled`, `openai_api_key_redacted` |
| `whoami` | `GET /v0/me` |
| `models` | `GET /v0/models` |
| `list-agents` | `--limit` 1–100, optional `--cursor`, `--pr-url` |
| `agent-status` | `--id` |
| `conversation` | `--id` |
| `artifacts` | `--id` |
| `artifact-download-url` | `--id`, `--path` |
| `create-agent` | `--branch-name`, repo **or** `--pr-url`; **`--prompt` / `--intent` / `--triage-repo`** (see above); `--dry-run`, polling flags |
| `followup` | `--id`, `--prompt` |
| `stop-agent` | `--id` |
| `stop-all-jobs` | Stop all matching agents for a repo scope (defaults to `--repo .`). Safety guard: dry-run unless `--yes`. |
| `delete-agent` | `--id` |

Transient **network/SSL errors** are surfaced as retriable failures (same backoff as `5xx`). Outbound JSON bodies preserve Unicode (UTF-8, not `\\u` escapes).

API contract aligns with [Cursor Cloud Agents API](https://cursor.com/docs/cloud-agent/api/endpoints).

---

# CLI reference — `skills/cursor_handoff/scripts/cursor_handoff.py`

| Flag | Description |
|------|-------------|
| `--repo` | Local path, `https://github.com/...`, or `owner/repo` |
| `--prompt` | Task text (optional if `--intent` or `--triage` + local `--repo`) |
| `--intent` | `code-review`, `refactor`, `release-notes`, or `brief` — prepends a structured scaffold |
| `--triage` | Prepend repo triage block; **requires local** `--repo` path (not URL-only) |
| `--read-only` | `true` / `false` |
| `--mode` | `auto`, `api`, `cli` |
| `--branch` | Optional; default generated `openclaw/task-YYYYMMDD-HHMMSS` |
| `--pr-url` | API: `source.prUrl` |
| `--auto-create-pr` etc. | API PR targets |
| `--poll-max-attempts`, `--poll-interval-seconds` | Post-create polling |
| `--timeout-seconds`, `--api-retries`, `--api-retry-backoff-seconds` | API resilience |
| `--cli-timeout-seconds` | CLI backend only; subprocess limit (`0` = none). Default `3600` |
| `--version` / `-V` | Print version and exit |
| `--diagnose` | No handoff; env + optional `/me` and `/agents?limit=1`; JSON `checks` includes `dotenv_files_loaded`, `openai_api_key_present`, `openai_api_enabled`, `openai_api_key_redacted` |
| `--show-key` | With `--diagnose` only: redacted previews for Cursor/OpenAI keys in JSON |
| `--dry-run` | Validate and show payload; works even if no backend configured (`backend: unavailable`) |

---

## Related integration env vars

The CLIs above directly require Cursor credentials. The setup wizard can also write optional env vars used by other OpenClaw skills/tools:

- `GH_TOKEN` / `GITHUB_TOKEN`
- `GEMINI_API_KEY`
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
- `BRAVE_SEARCH_API_KEY` / `BRAVE_ANSWERS_API_KEY`
- `MINIMAX_API_KEY`

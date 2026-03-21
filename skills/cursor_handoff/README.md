# cursor_handoff (OpenClaw Local Skill)

`cursor_handoff` is a local OpenClaw skill for safely delegating heavy coding and repository tasks to Cursor.

It is designed for production-style use on macOS with explicit backend selection, clear failure modes, and audit-friendly output.

## Why This Exists

OpenClaw is great for direct operations, but some tasks are better delegated to a coding agent with stronger repository execution flow:

- Large multi-file changes
- Deep repo analysis and refactoring
- Branch-oriented implementation work
- Test-fix and PR-oriented workflows

This skill gives OpenClaw an explicit handoff path:

1. Preferred: Cursor Cloud Agents API
2. Fallback: local Cursor CLI wrapper

## File Layout

```text
~/.openclaw/workspace/skills/cursor_handoff/
├── SKILL.md
├── README.md
├── .env.example
└── scripts/
    ├── cursor_handoff.py
    ├── cursor_cli_fallback.sh
    └── test_handoff.sh
```

## Installation

1. Place files exactly under:
   `~/.openclaw/workspace/skills/cursor_handoff/`
2. Make scripts executable:
   - `chmod +x ~/.openclaw/workspace/skills/cursor_handoff/scripts/*.py`
   - `chmod +x ~/.openclaw/workspace/skills/cursor_handoff/scripts/*.sh`
3. (Optional) create `.env` from `.env.example` and set credentials.
4. Restart OpenClaw gateway and verify the skill appears.

## Environment Variables

Defined in `.env.example`:

- `CURSOR_API_KEY`  
  Cursor API key used for cloud-agent handoff.
- `CURSOR_EMAIL`  
  Optional legacy/contact field; not currently required for auth.
- `CURSOR_BASE_URL`  
  Cursor API base URL (default: `https://api.cursor.com`).
- `OPENCLAW_CURSOR_DEFAULT_MODE`  
  Default mode when `--mode` is omitted (`auto`, `api`, `cli`).
- `OPENAI_API_KEY` / `OPENAI_API_ENABLED`  
  Optional OpenAI API credentials for future features. `OPENAI_API_ENABLED` must be `1`, `true`, or `yes` (case-insensitive) for the key to be considered active. Set via `bash scripts/setup_admin.sh` in the main repo or edit `.env` manually.
- `GH_TOKEN` / `GITHUB_TOKEN`, `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `BRAVE_SEARCH_API_KEY`, `BRAVE_ANSWERS_API_KEY`, `MINIMAX_API_KEY`  
  Optional integration keys that the main repo wizard can write into this `.env` for other OpenClaw skills on the same machine.

## API Mode vs CLI Mode

### API Mode (Preferred)

- Uses Cursor Cloud Agents endpoint: `POST /v0/agents`
- Supports status polling via `GET /v0/agents/{id}`
- Requires `CURSOR_API_KEY`
- Requires GitHub repository URL (local repo path is resolved via `git remote get-url origin`)
- Supports optional PR-oriented submission via `--pr-url`
- Supports optional `target.autoCreatePr` via `--auto-create-pr true`
- Supports retry/backoff and timeout controls:
  - `--timeout-seconds`
  - `--api-retries`
  - `--api-retry-backoff-seconds`

### CLI Mode (Fallback)

- Uses `scripts/cursor_cli_fallback.sh`
- Detects local `agent` first, then `cursor-agent`
- Uses safe quoting and no GUI automation
- Requires local repo path

### Auto Mode

- Uses API when credentials exist
- Falls back to CLI wrapper otherwise
- Fails clearly if neither backend is available

## Architecture

- `SKILL.md`: Routing policy for when OpenClaw should delegate to Cursor.
- `scripts/cursor_handoff.py`: Main orchestrator (validation, backend selection, submission, polling).
- `scripts/cursor_cli_fallback.sh`: Minimal safe wrapper for local CLI agent tools.
- `scripts/test_handoff.sh`: Smoke + syntax checks.

## How OpenClaw Should Use It

OpenClaw should select this skill for heavy coding/repo-aware requests and avoid it for small direct tasks.

Recommended behavior:

1. Detect task complexity and repo context
2. Summarize request into a precise implementation prompt
3. Resolve repo path
4. Choose branch (user-specified or generated `openclaw/task-YYYYMMDD-HHMMSS`)
5. Use read-only mode for analysis/review/planning
6. Use edit mode only when user explicitly requests changes
7. Return concise result metadata to chat

## Manual Usage

### Show help

```bash
python3 ~/.openclaw/workspace/skills/cursor_handoff/scripts/cursor_handoff.py --help
```

### Dry run (safe)

```bash
python3 ~/.openclaw/workspace/skills/cursor_handoff/scripts/cursor_handoff.py \
  --repo "/path/to/local/repo" \
  --prompt "Analyze architecture and propose refactor plan" \
  --mode auto \
  --read-only true \
  --json \
  --dry-run
```

### Diagnostics mode (safe)

Use this when auth/env/SSL behavior is unclear:

```bash
python3 ~/.openclaw/workspace/skills/cursor_handoff/scripts/cursor_handoff.py \
  --diagnose \
  --mode auto \
  --json
```

### Real API submission

```bash
python3 ~/.openclaw/workspace/skills/cursor_handoff/scripts/cursor_handoff.py \
  --repo "https://github.com/your-org/your-repo" \
  --prompt "Fix failing tests and summarize changes" \
  --mode api \
  --read-only false \
  --json
```

### Real API submission (PR-based source)

```bash
python3 ~/.openclaw/workspace/skills/cursor_handoff/scripts/cursor_handoff.py \
  --repo "your-org/your-repo" \
  --pr-url "https://github.com/your-org/your-repo/pull/123" \
  --prompt "Review this PR and propose minimal safe fixes" \
  --mode api \
  --read-only true \
  --json
```

### Real CLI fallback

```bash
python3 ~/.openclaw/workspace/skills/cursor_handoff/scripts/cursor_handoff.py \
  --repo "/path/to/local/repo" \
  --prompt "Review repo and produce implementation plan" \
  --mode cli \
  --read-only true \
  --json
```

## Testing

Run:

```bash
~/.openclaw/workspace/skills/cursor_handoff/scripts/test_handoff.sh
```

The test script checks:

- required files exist
- Python help command runs
- syntax checks for Python and shell wrappers
- unit tests in `tests/test_cursor_handoff.py`
- dry-run invocation works
- diagnostics invocation works

## Troubleshooting

- **Error: missing API key**
  - Set `CURSOR_API_KEY` and retry API/auto mode.
- **Error: local repo has no GitHub remote**
  - API mode needs a GitHub URL. Add `origin` remote or pass URL directly via `--repo`.
- **CLI fallback not found**
  - Install/verify `agent` or `cursor-agent` is in `PATH`.
- **403/401 from API**
  - Verify API key validity and account access.
  - Script tries Bearer first and then Basic due current docs/OpenAPI mismatch.
- **SSL cert verify errors (`CERTIFICATE_VERIFY_FAILED`)**
  - Run diagnostics and check `ssl` block output.
  - Typical fix on macOS:
    - `python3 -m pip install --user --upgrade certifi`
    - `export SSL_CERT_FILE="$(python3 -c 'import certifi; print(certifi.where())')"`
- **Repo argument rejected**
  - Use one of: local path, full GitHub URL, or `owner/repo`.
- **Want PR creation from API**
  - Use `--auto-create-pr true`; note this is only available in API mode.

## Security Notes

- No secrets are hardcoded.
- Prompt is passed safely without shell interpolation in Python mode.
- CLI wrapper uses strict bash mode (`set -euo pipefail`) and quoted args.
- Read-only mode is explicit and encoded into prompt instructions.

## API Contract Notes (Current Docs)

- Base URL: `https://api.cursor.com`
- Launch endpoint: `POST /v0/agents`
- Status endpoint: `GET /v0/agents/{id}`
- Source supports:
  - `source.repository` (+ optional `source.ref`)
  - or `source.prUrl`
- Target supports:
  - `target.branchName`
  - `target.autoCreatePr`
  - `target.openAsCursorGithubApp`
  - `target.skipReviewerRequest`
- There is no explicit API field for read-only execution intent. This skill communicates read-only intent via prompt policy text.

## Verify Skill Loaded

After restart:

```bash
openclaw gateway restart
openclaw skills list
```

Look for:
- `cursor_handoff`

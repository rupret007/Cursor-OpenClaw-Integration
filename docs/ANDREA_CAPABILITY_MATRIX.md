# Andrea capability matrix

Live readiness for the **Andrea** operator stack (OpenClaw + this repo + host tools).  
Use this before taking tasks so you know what is **ready**, **ready_with_limits**, or **blocked**.

## Generate the matrix

From the repository root:

```bash
python3 scripts/andrea_capabilities.py
```

**JSON** (for automation / logs):

```bash
python3 scripts/andrea_capabilities.py --json
```

**Markdown table** (paste into reports):

```bash
python3 scripts/andrea_capabilities.py --markdown-table
```

**Strict gate** (exit `1` if any *critical* row is `blocked` — e.g. missing `python3`, `openclaw`, `gh`, broken `cursor_openclaw diagnose`, missing `CURSOR_API_KEY`):

```bash
python3 scripts/andrea_capabilities.py --strict
```

Override repo root (e.g. in tests):

```bash
ANDREA_REPO_ROOT=/path/to/Cursor-OpenClaw-Integration python3 scripts/andrea_capabilities.py --json
```

## What is checked

| Area | Source of truth | Notes |
|------|-----------------|--------|
| Binaries | `PATH` | `python3`, `openclaw`, `gh`, `git`, `curl`, optional `gemini` |
| OpenClaw | `openclaw skills list` | Plus name-match for expected skills (see script constant `EXPECTED_OPENCLAW_SKILLS`) |
| GitHub auth | `gh auth status` + env | `GH_TOKEN` / `GITHUB_TOKEN` counts as **ready_with_limits** if CLI session missing |
| Cursor CLI | `python3 scripts/cursor_openclaw.py --json diagnose` | Probes CLI health; **never** prints keys |
| Secrets | Boolean only | Keys from `.env.example` family: present in process env **or** repo `.env` **or** `~/.openclaw/workspace/skills/cursor_handoff/.env` — **values are never shown** |

## Status meanings

- **ready** — Can use this path without extra setup.
- **ready_with_limits** — Partial / optional / degraded (e.g. optional CLI missing, token-only GitHub).
- **blocked** — Missing dependency or failed check; fix before relying on that lane.

## Related automation

- Reliability probes: `bash scripts/andrea_reliability_probes.sh` (deterministic env for CLI checks + capability snapshot).
- Full integration: `bash scripts/test_integration.sh`.

## Operations context

See [ANDREA_OPERATIONS_PLAYBOOK.md](ANDREA_OPERATIONS_PLAYBOOK.md) for how this fits startup self-checks and recovery.

# OpenClaw + `cursor_handoff` skill

## Roles

| Component | Role |
|-----------|------|
| **`scripts/cursor_openclaw.py`** | Full Cursor Cloud Agents API surface from the shell (diagnostics, lifecycle, artifacts). |
| **`skills/cursor_handoff/`** | OpenClaw Agent Skill: policy + `cursor_handoff.py` for **delegating heavy repo work** to Cursor (API preferred, CLI fallback). |

## Installing the skill in OpenClaw

1. Copy the skill directory to your OpenClaw workspace (run from the repo root):

   ```bash
   mkdir -p ~/.openclaw/workspace/skills
   cp -R skills/cursor_handoff ~/.openclaw/workspace/skills/
   ```

2. Optional: copy `.env.example` to `.env` inside the skill folder and set `CURSOR_API_KEY` (or rely on gateway/shell env).  
   The setup wizard can also write additional integration keys (`GH_TOKEN`, `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `BRAVE_SEARCH_API_KEY`, `MINIMAX_API_KEY`, etc.) into both repo and skill `.env` for cross-skill convenience.

## OpenAI API (`OPENAI_API_KEY` + `OPENAI_API_ENABLED`)

This integration repo and **`cursor_handoff`** treat OpenAI as an **optional platform API** (from [platform.openai.com](https://platform.openai.com) — billing-enabled API key, **not** ChatGPT Plus).

| Mechanism | Role |
|-----------|------|
| **`OPENAI_API_KEY`** | Stored in repo `.env` and/or `~/.openclaw/workspace/skills/cursor_handoff/.env` when you use the wizard or `scripts/dotenv_set_key.py`. |
| **`OPENAI_API_ENABLED`** | Must be **`1`**, **`true`**, or **`yes`** (case-insensitive) or tools **must not** use the key. This is enforced in `cursor_openclaw.py` / `cursor_handoff.py` diagnostics (`parse_openai_enabled`). |

**One-shot merge (repo + skill `.env`, key + enabled):**

```bash
python3 scripts/dotenv_set_key.py OPENAI_API_KEY --enable-openai --skill
openclaw gateway restart
```

**OpenClaw product note:** Gateway routing, model pickers, and any first-party OpenClaw “use OpenAI” toggles may live in **OpenClaw’s own** workspace or UI in addition to these env vars. If OpenClaw still says the key is wrong, use **[OPENCLAW_OPENAI_TROUBLESHOOTING.md](OPENCLAW_OPENAI_TROUBLESHOOTING.md)** and `bash scripts/openclaw_apply_openai_key.sh` (reads `.env`, runs `openclaw onboard --openai-api-key`).

3. Restart gateway:

   ```bash
   openclaw gateway restart
   openclaw skills list
   ```

   Confirm **`cursor_handoff`** shows as **ready**.

### Updating / daily sync

When you pull a newer `main`, repeat the copy and restart — no need to remove the old folder unless you want a guaranteed clean tree (e.g. after files were deleted in the repo):

```bash
cp -R skills/cursor_handoff ~/.openclaw/workspace/skills/
openclaw gateway restart
```

## Typical OpenClaw → Cursor flow

1. **Preflight:** `python3 .../cursor_handoff.py --diagnose --json` (JSON includes `tool_version`, `dotenv_files_loaded`, OpenAI env summary fields, and optional `/v0/me` checks)
2. **Handoff (read-only audit):** `--read-only true --dry-run` first, then real run without `--dry-run`.
3. **Handoff (implementation):** `--read-only false` only when the user explicitly wants code changes.
4. **Deep operations:** use `cursor_openclaw.py` for `list-agents`, `conversation`, `followup`, `artifacts`, etc.

## Skill scripts

| Script | Purpose |
|--------|---------|
| `scripts/cursor_handoff.py` | Main orchestrator (API / CLI / auto, retries, diagnose). |
| `scripts/cursor_cli_fallback.sh` | Safe wrapper around `agent` or `cursor-agent`. |
| `scripts/test_handoff.sh` | Smoke + unit tests for the skill. |

## Documentation in repo

- [OpenClaw hybrid skills + BlueBubbles runbook](ANDREA_OPENCLAW_HYBRID_SKILLS.md) — install/verify BlueBubbles, Andrea structured messaging paths, and OpenClaw-first delegation.
- [Roadmap / phased integration plan](../openclaw-cursor-integration-roadmap.md) (repo root)
- [Integration proposal / ideas](../openclaw-cursor-integration-proposal.md) (repo root)

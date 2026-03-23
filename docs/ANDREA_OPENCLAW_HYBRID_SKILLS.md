# OpenClaw hybrid personal-assistant skills (Apple + Google + execution)

This doc supports the **Masterclass / hybrid expansion**: enable productivity lanes first, then execution/automation, then optional voice — without destabilizing the core OpenClaw baseline.

## Verification commands (after each wave)

```bash
cd /path/to/Cursor-OpenClaw-Integration
openclaw skills check
OPENCLAW_ENFORCE=1 MODEL_GUARD_ON_FAIL=1 bash scripts/andrea_doctor.sh
bash scripts/andrea_release_gate.sh
bash scripts/test_integration.sh
bash skills/cursor_handoff/scripts/test_handoff.sh
```

**Strict eligibility** (requires `jq` + satisfied skill requirements): set `ANDREA_OPENCLAW_ELIGIBLE_SKILLS` to a CSV of skill keys, then run the enforcer — see `.env.example` comments.

Remove the **GitHub Grade‑C blocker** first when possible: `gh auth login` (or `GH_TOKEN` / `GITHUB_TOKEN` in `.env`).

---

## Wave 1 — Productivity (Apple + Google + meta)

| Skill | Skill key | Typical install / auth |
|------|-----------|-------------------------|
| BlueBubbles / iMessage | `bluebubbles` | Install/verify through `openclaw skills info bluebubbles`; keep the BlueBubbles bridge healthy and grant any required local messaging permissions before relying on outbound send flows. |
| Apple Notes | `apple-notes` | Install **`memo`** CLI (Homebrew: `brew install memo` — confirm with `openclaw skills info apple-notes`). Grant macOS automation/Notes permissions as prompted. |
| Apple Reminders | `apple-reminders` | Install **`remindctl`** (common tap: `brew install steipete/tap/remindctl`; always verify with `openclaw skills info apple-reminders`). |
| Things 3 | `things-mac` | Install the **`things`** CLI for Things 3 per upstream instructions (`openclaw skills info things-mac` — may not be in core Homebrew). |
| Google Workspace | `gog` | Install **`gog`** CLI; complete its OAuth / config flow per upstream docs (`openclaw skills info gog`). |
| Summarize | `summarize` | Install **`summarize`** binary per skill metadata (`openclaw skills info summarize`). |
| Session logs | `session-logs` | Requires **`jq`** + **`rg`** (ripgrep) on `PATH` (`openclaw skills info session-logs`). |

**Observability:** `python3 scripts/andrea_capabilities.py --json` includes `skill:*` rows for hybrid skills. Missing requirements surface as **`ready_with_limits`**, not silent **`ready`**.

---

## Wave 2 — Execution and automation

| Skill | Skill key | Typical install / auth |
|------|-----------|-------------------------|
| Coding agents | `coding-agent` | At least one of **`claude`**, **`codex`**, **`opencode`**, **`pi`** on `PATH` (see `openclaw skills info coding-agent`). |
| Terminal mux | `tmux` | `brew install tmux` (or distro package). |
| UI capture | `peekaboo` | Install **`peekaboo`** per skill metadata (macOS). |
| Gemini CLI (optional) | `gemini` | Install **`gemini`** CLI if you want the CLI lane beyond API-only usage (`openclaw skills info gemini`). |

---

## Wave 3 — Voice (nice-to-have)

| Skill / plugin | Skill key | Notes |
|-----------------|-----------|--------|
| Voice calls | `voice-call` | Requires OpenClaw config: `plugins.entries.voice-call.enabled` (see `openclaw skills info voice-call --json`). |
| Local STT/TTS (optional) | `openai-whisper`, etc. | Only if you explicitly want offline audio; validate with `openclaw skills check` after install. |

**Success criteria:** stable call startup/teardown; **no regression** on doctor + release gate; keep a one-page runbook note in [ANDREA_OPERATIONS_PLAYBOOK.md](ANDREA_OPERATIONS_PLAYBOOK.md) §9.

---

## Recommended environment knobs

| Variable | Purpose |
|----------|---------|
| `ANDREA_REQUIRED_OPENCLAW_SKILLS` | CSV passed to `andrea_openclaw_enforce.sh` — default includes hybrid keys (catalog presence). |
| `ANDREA_OPENCLAW_ELIGIBLE_SKILLS` | CSV; when non-empty, enforcer verifies each skill is **eligible** via `openclaw skills info <name> --json` + `jq`. |
| `ANDREA_OPENCLAW_SKILLS_CHECK` | Set to `1` to run `openclaw skills check` during enforce (informational; does not fail the script). |

---

## Refresh skill + gateway (after repo changes)

```bash
cp -R skills/cursor_handoff ~/.openclaw/workspace/skills/
openclaw gateway restart
openclaw skills check
```

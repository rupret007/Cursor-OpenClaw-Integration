# Andrea DevOps / GitHub runbook

Repeatable **task → branch → test → commit → PR** flow for Andrea (OpenClaw + host tools + this repo). Includes auth checks and **fallbacks** when one path fails.

---

## 0. Preconditions

```bash
python3 scripts/andrea_capabilities.py
gh auth status
```

- If `gh` session is missing but `GH_TOKEN` / `GITHUB_TOKEN` is set: many API flows still work via env (**ready_with_limits**).
- If both fail: refresh login (`gh auth login`) or update token in `.env` (never commit).

---

## 1. Standard task workflow

1. **Sync**
   - `git fetch origin && git checkout main && git pull origin main`
2. **Branch**
   - `git checkout -b andrea/<short-task-slug>`
3. **Implement**
   - Smallest change that satisfies the task; prefer tests/docs alongside code.
4. **Verify (local)**
   - `bash scripts/test_integration.sh`
   - Optional live API: `RUN_LIVE_API=1 bash scripts/exhaustive_feature_check.sh`
5. **Commit**
   - `git add -A && git commit -m "type: concise summary"`
6. **Push**
   - `git push -u origin HEAD`
7. **PR**
   - `gh pr create --fill` or use GitHub UI with body template (what/why/test plan).

---

## 2. Cursor Cloud Agents lane (API)

Use when the task is best handed to Cursor Cloud Agents:

```bash
python3 scripts/cursor_openclaw.py --json diagnose
python3 scripts/cursor_openclaw.py --json create-agent \
  --prompt "…" \
  --repository "https://github.com/owner/repo" \
  --ref main \
  --branch-name "cursor/task-slug" \
  --auto-create-pr false \
  --poll-attempts 5
```

**Fallbacks**

- **401 / auth**: rotate `CURSOR_API_KEY`; confirm `CURSOR_AUTH_MODE` / `CURSOR_BASE_URL`.
- **Network / SSL**: see README troubleshooting (`SSL_CERT_FILE` + `certifi`).
- **Rate limits**: reduce polling; backoff; use `list-agents --limit` sparingly.

---

## 3. OpenClaw `cursor_handoff` lane

For OpenClaw-driven handoff (API-first, CLI fallback):

```bash
python3 skills/cursor_handoff/scripts/cursor_handoff.py --repo "$(pwd)" --prompt "…" --dry-run
```

Remove `--dry-run` only after the human expects a real launch.

**Fallbacks**

- **API path fails**: skill may try CLI mode (`--mode auto` / `cli`) per `SKILL.md`.
- **Missing key**: run `bash scripts/setup_admin.sh` or export `CURSOR_API_KEY`.

---

## 4. GitHub CLI patterns

| Goal | Command |
|------|---------|
| Repo context | `gh repo view` |
| Issues | `gh issue list`, `gh issue view N` |
| PRs | `gh pr list`, `gh pr view N`, `gh pr checks N` |
| Clone fork workflow | `gh repo clone owner/repo` |

**Fallback:** If `gh` errors but token exists, use GitHub web UI or REST with `curl` + `GH_TOKEN` (keep tokens out of logs).

---

## 5. Failure modes → actions

| Symptom | First action |
|---------|----------------|
| `gh: command not found` | Install GitHub CLI; or use HTTPS + token per org policy |
| `not logged in` | `gh auth login` or set `GH_TOKEN` |
| `CURSOR_API_KEY missing` | Export key or write `.env` via `setup_admin.sh` |
| `openclaw` missing | Install OpenClaw; confirm `openclaw skills list` |
| Tests fail on `main` | Stop; don’t merge; open issue or fix forward on branch |

---

## 6. Related docs

- [ANDREA_CAPABILITY_MATRIX.md](ANDREA_CAPABILITY_MATRIX.md) — live readiness
- [ANDREA_AUTONOMY_POLICY.md](ANDREA_AUTONOMY_POLICY.md) — when to confirm vs execute
- [docs/CLI_REFERENCE.md](CLI_REFERENCE.md) — Cursor CLI flags
- [docs/OPENCLAW_SKILL.md](OPENCLAW_SKILL.md) — skill install & flows

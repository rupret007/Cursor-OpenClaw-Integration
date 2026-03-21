# Andrea security & trust hardening

Masterclass operators treat **secrets, logs, and backups** as first-class failure modes.

---

## 1. SecretRef / env-first (OpenClaw + hosts)

- **Prefer** OpenClaw’s **environment variable references** and **SecretRef** patterns over **plaintext API keys** inside `~/.openclaw/openclaw.json` when your OpenClaw version supports it. See upstream onboarding docs for `secret-input-mode ref` and gateway token SecretRef.
- **Repo + skill `.env`**: keep real values only in **gitignored** files (`./.env`, `skills/cursor_handoff/.env`, `~/.openclaw/workspace/skills/cursor_handoff/.env`). Never commit.
- **Rotate** any credential that may have appeared in **chat logs**, **screenshots**, **terminal scrollback**, or **shared diagnostics**.

---

## 2. Redaction-safe diagnostics (never paste raw secrets)

When asking for help:

| Do | Don’t |
|----|--------|
| Paste **exit codes** and **error class** (`401`, `rate_limit`, `model_not_found`) | Paste **API keys**, **bot tokens**, **gateway URLs with `#token=`** |
| Use `python3 scripts/andrea_capabilities.py` (boolean presence only) | Paste full `openclaw.json` or `.env` |
| Use `openclaw models status --json` and **strip** `auth` blobs if sharing | Paste `openclaw models status --probe` tables that echo env prefixes if your tool redacts poorly |

**OpenClaw model probes:** `--probe-timeout` is in **milliseconds**. Example: 30s → `--probe-timeout 30000` (see [README](../README.md) Andrea / OpenClaw section).

---

## 3. Automated sanity check (repo)

From the repository root:

```bash
bash scripts/andrea_security_sanity.sh
```

This verifies (among other checks) that `.env` is not tracked, scans tracked code for high-signal secret patterns, and warns on common OpenClaw backup files in `$HOME`.

---

## 4. Gateway token hygiene

- **View** (only on your machine): `openclaw config get gateway.auth.token`
- **Rotate**: `openclaw doctor --generate-gateway-token` (per OpenClaw docs), then update clients / bookmarked Control UI URLs.
- **Rule**: treat **any URL with `#token=`** as a **password** — don’t drop it into assistants or tickets.

---

## 5. Backup file hygiene

`openclaw` config edits often create **`openclaw.json.bak`** (and similar). Those files can contain **the same secrets** as the live config.

- Prefer storing backups **outside** synced folders, or **encrypt** backup disks.
- Periodically **delete stale** `*.bak` under `~/.openclaw/` after you confirm the active config works.

---

## 6. Rotation checklist (after any exposure)

1. OpenAI: rotate **platform** API key; update `.env` + OpenClaw auth profile.
2. Google / Gemini: rotate **API key** in Google AI Studio; update `GEMINI_API_KEY`.
3. MiniMax: rotate key in provider console; update `MINIMAX_API_KEY`.
4. Telegram: **revoke** bot token with BotFather; set new token in OpenClaw channel config.
5. Brave / other search keys: rotate in provider dashboard.
6. GitHub: revoke PAT; issue new fine-scoped token.
7. Run `bash scripts/andrea_doctor.sh` and confirm **Grade A** after rotation.

---

## 7. Related docs

- [ANDREA_OPERATIONS_PLAYBOOK.md](ANDREA_OPERATIONS_PLAYBOOK.md)
- [OPENCLAW_OPENAI_TROUBLESHOOTING.md](OPENCLAW_OPENAI_TROUBLESHOOTING.md)
- [DEPLOYMENT.md](DEPLOYMENT.md)

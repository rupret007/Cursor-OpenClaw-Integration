# Andrea operations playbook

Single handoff doc for **who Andrea is**, **what she can do autonomously**, **what needs human approval**, and **how to recover** from common failures.

---

## 1. Role

**Andrea** is a high-autonomy personal operator built on **OpenClaw** + **Cursor-OpenClaw-Integration**: coding/DevOps, Telegram/comms patterns, and productivity routines — with **execute-first** behavior and **safety guardrails** for destructive work.

---

## 2. Autonomous (default — no extra ask)

- Read/search code and docs; propose patches; run **local** tests and linters.
- Use **dry-run** and **diagnose** paths for Cursor / handoff CLIs.
- Git workflow on a **feature branch**: commit, push, open PR when the user’s norm allows.
- Non-destructive `gh` queries; capability / reliability probes documented here.
- Telegram **ack / status** patterns in [ANDREA_COMMS_PRODUCTIVITY.md](ANDREA_COMMS_PRODUCTIVITY.md).

Full policy: [ANDREA_AUTONOMY_POLICY.md](ANDREA_AUTONOMY_POLICY.md).

---

## 3. Human approval required

- Irreversible deletion, production data changes, security weakening, billing, org-wide Git settings.
- Force-push to shared branches; merging with broken CI (unless user explicitly overrides).
- External commitments or legal-sensitive messaging.

See **confirm-required** table in [ANDREA_AUTONOMY_POLICY.md](ANDREA_AUTONOMY_POLICY.md).

---

## 4. Startup self-check (before big sessions)

**One command (recommended):** security sanity + capability snapshot + readiness grade + reliability probes + optional OpenClaw model probe:

```bash
cd /path/to/Cursor-OpenClaw-Integration
bash scripts/andrea_doctor.sh
# CI / headless: skip live OpenClaw probe
# SKIP_OPENCLAW_PROBE=1 bash scripts/andrea_doctor.sh
# Treat security warnings as failures:
# STRICT_SECURITY=1 bash scripts/andrea_doctor.sh
```

Manual steps (same ingredients):

```bash
python3 scripts/andrea_capabilities.py
python3 scripts/andrea_readiness_grade.py   # A/B/C; exits 1 on C
bash scripts/andrea_security_sanity.sh
```

Optional strict gate (fails if critical capabilities blocked):

```bash
python3 scripts/andrea_capabilities.py --strict
```

SLO-style gate (grade + optional `openclaw models status --probe`; **probe timeout is ms**):

```bash
bash scripts/andrea_slo_check.sh
# SKIP_OPENCLAW_PROBE=1 bash scripts/andrea_slo_check.sh
```

---

## 5. Verification stack

| Step | Command |
|------|---------|
| Unit + integration | `bash scripts/test_integration.sh` |
| Masterclass doctor | `bash scripts/andrea_doctor.sh` |
| Security sanity (repo) | `bash scripts/andrea_security_sanity.sh` |
| Readiness grade (A/B/C) | `python3 scripts/andrea_readiness_grade.py` |
| SLO check (grade + probe) | `bash scripts/andrea_slo_check.sh` |
| Reliability probes | `bash scripts/andrea_reliability_probes.sh` |
| Live API (optional) | `RUN_LIVE_API=1 bash scripts/exhaustive_feature_check.sh` |
| Live host tools (optional) | `RUN_LIVE_PROBES=1 bash scripts/andrea_reliability_probes.sh` |

---

## 6. Recovery cheat sheet

| Symptom | Fix |
|---------|-----|
| `CURSOR_API_KEY missing` | `export CURSOR_API_KEY=…` or `bash scripts/setup_admin.sh` |
| `401` from Cursor API | Rotate key; check `CURSOR_BASE_URL` / `CURSOR_AUTH_MODE` |
| `gh` not logged in | `gh auth login` or set `GH_TOKEN` / `GITHUB_TOKEN` in `.env` (merge without full wizard: `python3 scripts/dotenv_set_key.py GH_TOKEN --skill`) |
| `openclaw` / skill missing | Install OpenClaw; `cp -R skills/cursor_handoff ~/.openclaw/workspace/skills/`; `openclaw gateway restart` |
| SSL errors in Python | See README: `SSL_CERT_FILE` + `certifi` |
| Tests fail | Fix on a branch; do not merge to `main` until green |

---

## 7. Doc map

| Document | Purpose |
|----------|---------|
| [ANDREA_SECURITY.md](ANDREA_SECURITY.md) | Secrets, redaction, gateway token, rotation |
| [ANDREA_MODEL_POLICY.md](ANDREA_MODEL_POLICY.md) | fast/balanced/deep profiles + rate-limit playbook |
| [ANDREA_CAPABILITY_MATRIX.md](ANDREA_CAPABILITY_MATRIX.md) | Live readiness matrix |
| [ANDREA_AUTONOMY_POLICY.md](ANDREA_AUTONOMY_POLICY.md) | Execute-first + boundaries |
| [ANDREA_DEVOPS_RUNBOOK.md](ANDREA_DEVOPS_RUNBOOK.md) | Branch/PR + GitHub + fallbacks |
| [ANDREA_COMMS_PRODUCTIVITY.md](ANDREA_COMMS_PRODUCTIVITY.md) | Telegram + routines + memory policy |
| [ANDREA_READINESS_REPORT.md](ANDREA_READINESS_REPORT.md) | Final readiness template / last run |
| [docs/DEPLOYMENT.md](DEPLOYMENT.md) | Branch + deployment baseline |

---

## 8. Maintenance

When adding a new secret key to `.env.example`, update `SECRET_KEYS` in `scripts/andrea_capabilities.py` so the matrix stays accurate.

Run `bash scripts/andrea_security_sanity.sh` before merging changes that touch env or provider wiring; use `STRICT=1` locally if you want backup-file warnings to fail the check.

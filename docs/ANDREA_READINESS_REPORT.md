# Andrea readiness report

**Last updated:** 2026-03-20  
**Repo / branch:** Cursor-OpenClaw-Integration @ `main`  
**Operator:** _(your name)_

This file records the **latest verified state** of the Andrea max-autonomy stack. Refresh after material changes (new machine, token rotation, OpenClaw upgrade).

## 0. Max-autonomy rollout (implementation)

The following artifacts landed on **2026-03-20** (extended with masterclass hardening):

- `scripts/andrea_capabilities.py` — live capability matrix (`--json`, `--markdown-table`, `--strict`) + `meta` pointers (model policy, probe units, doctor scripts)
- `scripts/andrea_readiness_grade.py` — **A/B/C** readiness grade from capability JSON (`--json`; exit `1` on **C**)
- `scripts/andrea_security_sanity.sh` — repo secret-pattern + tracked-file checks (`STRICT=1` fails on backup warnings)
- `scripts/andrea_slo_check.sh` — grade + optional `openclaw models status --probe` (**timeout in ms**)
- `scripts/andrea_doctor.sh` — single operator health pass (security → capabilities + grade → reliability probes → optional OpenClaw probe)
- `scripts/andrea_reliability_probes.sh` — deterministic `diagnose` probe + capability JSON shape
- `docs/ANDREA_SECURITY.md`, `ANDREA_MODEL_POLICY.md`, `ANDREA_CAPABILITY_MATRIX.md`, `ANDREA_AUTONOMY_POLICY.md`, `ANDREA_DEVOPS_RUNBOOK.md`, `ANDREA_COMMS_PRODUCTIVITY.md`, `ANDREA_OPERATIONS_PLAYBOOK.md`
- `README.md` — Andrea section + integration hook (`test_integration.sh` includes security sanity + readiness grade smoke)

Re-run verification on **your** machine and paste outputs into §1 below.

---

## 1. Commands run

Paste outputs or attach logs:

```bash
bash scripts/andrea_doctor.sh
# or headless: SKIP_OPENCLAW_PROBE=1 bash scripts/andrea_doctor.sh
```

```bash
python3 scripts/andrea_readiness_grade.py
python3 scripts/andrea_readiness_grade.py --json
```

```bash
python3 scripts/andrea_capabilities.py --json
```

```bash
bash scripts/andrea_reliability_probes.sh
bash scripts/andrea_slo_check.sh
```

```bash
bash scripts/test_integration.sh
```

Optional:

```bash
RUN_LIVE_API=1 bash scripts/exhaustive_feature_check.sh
RUN_LIVE_PROBES=1 bash scripts/andrea_reliability_probes.sh
python3 scripts/andrea_capabilities.py --strict
STRICT_SECURITY=1 bash scripts/andrea_doctor.sh
```

## 1.1 Readiness grade (SLO gate)

Record the letter grade and reasons:

| Grade | Meaning |
|-------|---------|
| **A** | No blocked capabilities; limited degradation |
| **B** | No blockers; many `ready_with_limits` or GitHub auth degraded |
| **C** | One or more blocked rows (or capabilities script failed) — **no-go** for autonomous ops until fixed |

---

## 2. Summary (human)

- **Readiness grade (A/B/C):** _(letter + reasons from `andrea_readiness_grade.py`)_  
- **Cursor Cloud Agents:** ready / limits / blocked — _(note)_  
- **GitHub (`gh` + token):** ready / limits / blocked — _(note)_  
- **OpenClaw + skills:** ready / limits / blocked — _(note)_  
- **Telegram:** ready / limits / blocked — _(note)_  
- **Gemini / Brave / MiniMax (optional):** ready / limits / blocked — _(note)_  

---

## 3. Blockers

_List open blockers and owners._

1. …

---

## 4. Sign-off

- **Safe for autonomous execute-first ops:** yes / no — _(why)_  
- **Next review date:** _(date)_

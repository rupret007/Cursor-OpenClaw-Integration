# Andrea readiness report

**Last updated:** 2026-03-20  
**Repo / branch:** Cursor-OpenClaw-Integration @ `main`  
**Operator:** _(your name)_

This file records the **latest verified state** of the Andrea max-autonomy stack. Refresh after material changes (new machine, token rotation, OpenClaw upgrade).

## 0. Max-autonomy rollout (implementation)

The following artifacts landed on **2026-03-20**:

- `scripts/andrea_capabilities.py` — live capability matrix (`--json`, `--markdown-table`, `--strict`)
- `scripts/andrea_reliability_probes.sh` — deterministic `diagnose` probe + capability JSON shape
- `docs/ANDREA_CAPABILITY_MATRIX.md`, `ANDREA_AUTONOMY_POLICY.md`, `ANDREA_DEVOPS_RUNBOOK.md`, `ANDREA_COMMS_PRODUCTIVITY.md`, `ANDREA_OPERATIONS_PLAYBOOK.md`
- `README.md` — Andrea section + integration hook (`test_integration.sh` step 6/7)

Re-run verification on **your** machine and paste outputs into §1 below.

---

## 1. Commands run

Paste outputs or attach logs:

```bash
python3 scripts/andrea_capabilities.py --json
```

```bash
bash scripts/andrea_reliability_probes.sh
```

```bash
bash scripts/test_integration.sh
```

Optional:

```bash
RUN_LIVE_API=1 bash scripts/exhaustive_feature_check.sh
RUN_LIVE_PROBES=1 bash scripts/andrea_reliability_probes.sh
python3 scripts/andrea_capabilities.py --strict
```

---

## 2. Summary (human)

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

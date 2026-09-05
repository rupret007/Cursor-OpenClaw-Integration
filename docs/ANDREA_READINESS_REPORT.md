# Andrea readiness report

**Last updated:** 2026-03-20  
**Repo / branch:** Cursor-OpenClaw-Integration @ `main`  
**Operator:** _(your name)_

This file records the **latest verified state** of the Andrea max-autonomy stack. Refresh after material changes (new machine, token rotation, OpenClaw upgrade).

Workflow note (September 5, 2026; **not** a new host sign-off): explicit offline
doctor checks now skip external probes and inherited live/remediation options.
Unexecuted critical capabilities are `not_run` and keep Grade C/no-go without
claiming missing software or failed authentication. Record the evidence mode
alongside any grade. See [OPERATOR_RECOVERY.md](OPERATOR_RECOVERY.md).

## 0. Max-autonomy rollout (implementation)

The following artifacts landed on **2026-03-20** (extended with masterclass hardening):

- `scripts/andrea_capabilities.py` — live capability matrix (`--json`, `--markdown-table`, `--strict`) + `meta` pointers (model policy, probe units, doctor scripts)
- `scripts/andrea_readiness_grade.py` — **A/B/C** readiness grade plus a redaction-safe prioritized action plan from capability JSON (`--json`; exit `1` on **C**)
- `scripts/andrea_security_sanity.sh` — repo secret-pattern + tracked-file checks (`STRICT=1` fails on backup warnings)
- `scripts/andrea_slo_check.sh` — grade + optional `openclaw models status --probe` (**timeout in ms**)
- `scripts/andrea_doctor.sh --offline` — operator health pass (security → readiness recap → reliability probes; reprints Andrea/Bob/owner next steps even on Grade C)
- `scripts/andrea_doctor_receipt.py` — allowlisted, mode-`600` JSON receipt builder plus `--verify` / `--consume` / `--summary` for Bob, Codex, Grok, Claude, and dashboards; fingerprints stage and actor/action/hold truth without raw probe output; failed stages override leftover Grade A next steps
- `scripts/andrea_reliability_probes.sh` — deterministic `diagnose` probe + capability JSON shape
- `docs/ANDREA_SECURITY.md`, `ANDREA_MODEL_POLICY.md`, `ANDREA_CAPABILITY_MATRIX.md`, `ANDREA_AUTONOMY_POLICY.md`, `ANDREA_DEVOPS_RUNBOOK.md`, `ANDREA_COMMS_PRODUCTIVITY.md`, `ANDREA_OPERATIONS_PLAYBOOK.md`
- `README.md` — Andrea section + integration hook (`test_integration.sh` includes security sanity + readiness grade smoke)

Re-run verification on **your** machine and paste outputs into §1 below.

---

## 1. Commands run

Paste outputs or attach logs:

```bash
bash scripts/andrea_doctor.sh --offline
# cross-agent/dashboard artifact from the same offline run:
bash scripts/andrea_doctor.sh --offline \
  --receipt /tmp/andrea-doctor-receipt.json
# live model probe (owner-approved host only):
# bash scripts/andrea_doctor.sh
# optional auto-remediation if model probe fails:
# MODEL_GUARD_ON_FAIL=1 bash scripts/andrea_doctor.sh
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
bash scripts/andrea_model_guard.sh --dry-run
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

Also record `Who acts first`, the first `Next action`, plus `Next for Andrea`,
`Next for the coding agent (Bob)`, and `Next for the owner` from the marked
operator recap. Machine consumers should read
`readiness_plan.safe_for_autonomous_ops`, `blocker_count`, `who_acts_first`,
`next_action`, `andrea_next_action`, `coding_agent_next_action`,
`owner_next_action`, `holds`, `routing`, and `actions` from
`python3 scripts/andrea_readiness_grade.py --json`; action text is code-owned
and never copies free-form probe notes or secret values. Grade A still emits a
concrete next action instead of leaving the field empty. Offline doctor still
completes and reprints that recap when the grade is C.

The optional doctor receipt adds deterministic stage outcomes and the same
actor contract under `handoff`, plus `blocked_reason` and `failed_stages`.
Do not trust the file by inspection. Validate and branch through:

```bash
python3 scripts/andrea_doctor_receipt.py --verify /tmp/andrea-doctor-receipt.json
python3 scripts/andrea_doctor_receipt.py --consume /tmp/andrea-doctor-receipt.json --audience dashboard
```

`blocked` is a hard stop. A failed security/reliability stage also replaces
Grade A “continue offline” copy with owner-first actions so leftover receipts
cannot authorize work. The artifact never includes raw probe output, capability
notes, environment values, or `repo_root`, and the doctor refuses `--receipt`
without `--offline`.

The existing local dashboard can consume this contract without a second
validator or store. Generate `data/andrea-doctor-receipt.json` with the offline
doctor, then read the top-level **Operator readiness** panel. The API summary
returns only allowlisted decision fields and no source path/fingerprint/raw
JSON. Missing or invalid receipts fail closed; verified receipts older than 24
hours are marked stale and cannot authorize current operation until rerun.
Previously verified owner holds/failed stages remain historical until new
verified evidence replaces them; expiration or page refresh does not clear a
blocker. Use the panel's stable `data/` refresh command for this dashboard, not
the generic CLI `/tmp/` destination. The panel never executes or copies it.

| Grade | Meaning |
|-------|---------|
| **A** | No blocked capabilities; limited degradation |
| **B** | No blockers; many `ready_with_limits` or GitHub auth degraded |
| **C** | Blocked rows, an unverified critical check (`not_run`), or a failed capabilities script — **no-go** for autonomous ops until resolved/verified |

---

## 1.2 Service level objectives (targets)

Tune numbers to your environment; record **observed** values in §2 after each run.

| SLO | Target (default) | How to measure |
|-----|------------------|----------------|
| **Readiness grade** | **A** preferred; **B** acceptable if no blockers | `python3 scripts/andrea_readiness_grade.py` |
| **`andrea_doctor` wall time** | &lt; 120s with `--offline` | `time bash scripts/andrea_doctor.sh --offline` |
| **OpenClaw model probe** | Completes within CLI `--probe-timeout` (ms) | `bash scripts/andrea_slo_check.sh` — note `openclaw_probe_wall_ms=…` line |
| **Telegram Bot API `getMe`** | &lt; 8000ms round-trip | `TELEGRAM_BOT_TOKEN=… bash scripts/andrea_slo_telegram.sh` or `TELEGRAM_SLO=1 bash scripts/andrea_slo_check.sh` |
| **Integration suite** | Green on `main` | `bash scripts/test_integration.sh` |
| **Strict pre-release** | Gate passes before shipping | `bash scripts/andrea_release_gate.sh` |

**Environment knobs:** `TELEGRAM_SLO_MAX_MS`, `OPENCLAW_PROBE_MS`, `TELEGRAM_SLO_SKIP=1`, `SKIP_OPENCLAW_PROBE=1`.

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

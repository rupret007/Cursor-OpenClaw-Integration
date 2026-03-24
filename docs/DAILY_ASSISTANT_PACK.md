# Trusted Daily Assistant Continuity and Productivity Pack (Stage A)

Operator-facing summary of the first low-risk **daily assistant** rollout pack: receipts, continuation records, domain repair signals, and onboarding defaults.

## Pack scope (`trusted_daily_continuity_v1`)

| Scenario ID | Role |
|-------------|------|
| `statusFollowupContinue` | Status / “what’s next” continuity |
| `noteOrReminderCapture` | Notes + reminders (receipt-backed) |
| `recentMessagesOrInboxLookup` | Read-only recent message / inbox style lookup |
| `goalContinuationAcrossSessions` | Resume work across sessions |

## Defaults

- **Direct-first**: scenarios default to onboarding state `live_direct`, which **blocks live collaboration advisory** until an operator moves a scenario to `live_advisory` via `/v1/internal/rollout` (`action=scenario_onboarding`).
- **Receipts**: user-facing outcome receipts persist to `user_outcome_receipts` and emit `UserOutcomeReceiptRecorded` (toggle with `ANDREA_DAILY_PACK_RECEIPTS_ENABLED`).
- **Continuation**: Telegram thread continuation writes `continuation_records` + `ContinuationRecorded` on the linked task.
- **Bounded repair observability**: missing reminder delivery target records a **non-executed** domain repair outcome (`resolve_missing_reminder_target`) — no automatic outbound sends.

## Operator surfaces

- Dashboard JSON: `GET /v1/dashboard/summary` → `daily_assistant_pack`, `daily_assistant_optimizer_hints`.
- HTML monitor: **Daily Assistant pack** panel (receipt metrics, onboarding, continuations, repairs).
- Internal API: `GET /v1/internal/daily-assistant-pack` (snapshot + evidence), `POST` with `{"action":"snapshot"|"record_decision",...}` (same auth as other internal routes).
- Collaboration summary: `collaboration_policy.daily_assistant_pack` mirrors the pack snapshot for tooling.

## Evaluation gates (receipt-truth)

Evidence helper `daily_pack_live_evidence_report` encodes plan thresholds (7-day window):

- ≥ **30** receipt events for the pack.
- Receipt **pass** rate ≥ **0.95** (`pass_hint` on stored receipts).
- Plan also calls for **0** privacy / false-receipt incidents and low continuation/delivery failure rates — wire those as separate metrics as delivery health instrumentation matures.

These gates **do not auto-promote** behavior; they inform operators and dashboards.

## Deferred and high-risk domains {#deferred-and-high-risk-domains}

Stay **measured-only, shadow-only, or blocked** for this package (do not treat as part of the first live daily slice):

| Scenario / domain | Why deferred |
|-------------------|--------------|
| `researchSummary` | Citation / proof boundary still draft; needs stronger adapters before live trust. |
| `approvalRequiredOutboundAction` | Outbound writes; requires provider receipts + approval UX. |
| `multiStepTroubleshoot` | Draft catalog; multi-step diagnostics need clearer draft boundary. |
| `mixedResourceGoal` | Draft; mixed lanes need clearer pack boundaries. |
| `verificationSensitiveAction` | Proof-bound; keep under existing verification + promotion flows (not “daily direct” pack). |
| Repo-heavy collaboration subjects | Continue to use collaboration rollout / verifier paths — not merged into this daily pack. |

## Live rollout slice (first)

**Live today (when enabled):** direct assistant replies for the four pack scenarios **with persisted receipts**, default **no live collaboration advisory** on those scenarios, Telegram continuation logging, and reminder capture receipts (including `awaiting_delivery_channel` with repair hints).

**Shadow / later:** richer compare loops, selective collaboration inside daily domains, and executed bounded repairs — only after evidence + explicit operator approval (see product plan slices 4–5).

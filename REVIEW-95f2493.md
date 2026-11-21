FAIL

# Independent release review — 95f2493

Reviewed exact head `95f24939c49715efabe242541f62da79a01c1618` on
`codex-candace/fo-finish-20261120`, draft PR #36, against
`origin/codex/first-officer-segment-1` at
`3d350baac7636fe068d5be6299991f5e776259b5`.

No live gateway, bobserver, Telegram, carrier, private-message service, or
production endpoint was contacted or changed. No secret, network address, or
personal contact value is reproduced here.

## Findings

### MEDIUM — Deleting every owner-writable audit copy still blesses a replacement chain

Locations: `first-officer/acceptance/cycle.py:223-239`,
`first-officer/acceptance/cycle.py:415-432`,
`first-officer/acceptance/cycle.py:641-647`, `INSTALL.md:115-120`

The new `root.id` does stop the earlier three-directory deletion: `initialize()`
refuses a replacement while that identity survives. It is another
owner-writable copy, however. Deleting the acceptance root, trust directory,
witness directory, and identity directory removes every indication that the
root existed. `initialize()` then creates a new key, heads, and identity, and
`verify()` accepts the empty replacement chain.

Safe offline repro: initialize a temporary root, append one routine, delete
`root`, `trust_dir(root)`, `witness_dir(root)`, and `identity_dir(root)`, then
initialize and verify the same root. The executed repro reported
`total_reset_verifies_empty True`.

This is **PARTIAL**, not fixed. The code and `INSTALL.md` disclose the residual,
but a separately protected durable root identity/checkpoint is still required
for the audit history to survive a malicious or accidental owner-level reset.

### MEDIUM — The enforced secret gate misses fine-grained GitHub PATs and lowercase Bearer auth

Locations: `first-officer/privacy/scan.py:38-43`,
`first-officer/privacy/scan.py:149-159`,
`first-officer/release.py:52-54`, `scripts/test_integration.sh:74-76`

The candidate and checkout scanners use the same `CREDENTIAL` and `BEARER`
expressions. `CREDENTIAL` recognizes the legacy short GitHub prefixes but not
the current `github_pat_` fine-grained PAT prefix. `BEARER` is case-sensitive
even though the HTTP authentication scheme is case-insensitive. A bare
fine-grained PAT or an authorization line using lowercase `bearer` can
therefore pass both the staged candidate scan and the integration/history
gate.

Safe offline repro (prints booleans only):

```python
cases = {
    "fine_grained_pat": "github" + "_pat_" + "A" * 40,
    "lowercase_bearer": "bearer " + "a" * 24,
    "temporary_cloud_key": "AS" + "IA" + "A" * 16,
    "alphabetic_bearer": "Bearer " + "a" * 24,
}
for name, value in cases.items():
    print(name, bool(scan.findings_in_text(value)))
```

Observed: the first two were `False`; the last two were `True`. Thus the exact
temporary-key and alphabetic-bearer repros from the prior review are repaired,
but the broader common-credential finding remains **PARTIAL**. Add both forms
to the source, history, and files-only regression coverage.

## Prior finding disposition

| Prior finding | Status | Production evidence |
| --- | --- | --- |
| Failed repeated rollback reactivates the candidate | **FIXED** | `first-officer/install.py:489-509` captures the complete pair observed after shutdown as `recovery`; an already-original repeated rollback therefore recovers original, not candidate. `tests/first_officer/test_install.py:137-177` exercises readiness failure from every original/candidate pair. |
| Total audit deletion can be blessed as a new chain | **PARTIAL** | `first-officer/acceptance/cycle.py:415-432` refuses reinitialization while `root.id` survives, and `verify()` checks it at lines 641-647. Deleting `identity_dir(root)` too still reproduces the replacement-chain acceptance above. |
| Hand-edited Playwright events can turn the gate green | **FIXED** | The production `evaluate` command passes both artifacts at `first-officer/qa/flaky.py:282-295`; `produce_events()` regenerates events from the Playwright report and checks its digest at lines 319-323. Editing the events file alone no longer controls attempts or outcome. |
| Secret gate misses temporary cloud keys and alphabetic bearer tokens | **PARTIAL** | The two reported forms are detected at `first-officer/privacy/scan.py:38-42` and 153-159, and release build invokes that scanner at `first-officer/release.py:52-54`. The fine-grained-PAT and lowercase-Bearer bypass above keeps the broader finding partial. |
| Local write failure can orphan active LiteLLM lane keys | **FIXED** | `first-officer/litellm/configure.py:329-339` generates and durably writes the pending key before calling `/key/generate`, then reuses that same key after a crash and verifies the remote policy before promotion. |
| Fetched text is exported outside its fence | **FIXED** | `first-officer/guard/boundary.py:120-151` fences `summary`, `user_summary`, and `raw_text` whenever pages/messages/state contributed. The hybrid runner short-circuits lookup routes before unrestricted OpenClaw at `scripts/andrea_sync_openclaw_hybrid.py:567-577`; `services/andrea_sync/server.py:210-229` unwraps only for the displayed reply. |
| INSTALL.md puts verification before read-only permissions | **FIXED** | Production order is publish, confirm move, chmod read-only, verify, switch at `first-officer/release.py:95-105`; `INSTALL.md:222-229` now matches it. |

## Whole-diff release conclusions

### Build, activation, rollback, and dry-run

- `first-officer/stage.py:156-168` binds the overlay to the full reviewed HEAD
  and a clean checkout. `release.py:42-58` gates the staged candidate, scans it,
  refuses archive replacement, and packs only afterward.
- Activation checks the running service, configured GitHub owner, and existing
  Andrea bridge before extraction (`first-officer/release.py:68-76`). Digest,
  inventory, and private rollback preparation precede publication at lines
  77-89. Read-only permissions and installed verification precede the switch at
  lines 95-105.
- `first-officer/install.py:397-421` removes a rejected candidate only when the
  complete original pair is live and no switch journal exists. It does not
  delete an active or half-switched candidate.
- A successful rollback restores the exact saved config and pin and waits for
  readiness. On switch failure, lines 489-509 restore the complete pair observed
  at entry when one existed. Repeating rollback after original is already live
  therefore keeps original live.
- All three actual `release.py --dry-run` command paths exited zero without
  invoking a plan command. They create only local temporary directories.

No new release-blocking release/install defect was found. The log label
`Verify the candidate with the installed verifier before publish` at
`first-officer/release.py:101` is stale because publication already occurred;
the ordering and operator docs are nevertheless correct.

### Prompt injection, approval, and delivery

- Lookup routes get only their route allowlist at
  `services/andrea_sync/server.py:4579-4584` and return from the restricted
  boundary before the unrestricted agent starts. Fetched pages, local messages,
  assistant-state lines, and durable memory are fenced; unfenceable memory is
  omitted at `services/andrea_sync/memory_engine.py:30-79`.
- The displayed summary is not fed back into the structured action parser.
  Sending is reached only from a current user message that fully matches the
  three confirmation phrases at `services/andrea_sync/server.py:336-342` and
  4308-4314.
- Draft binding covers action, exact target/body, principal, origin chat,
  expiry, and nonce at `services/andrea_sync/outbound.py:26-93`. The server
  rechecks principal, chat, binding, and expiry at
  `services/andrea_sync/server.py:4058-4074`.
- `services/andrea_sync/outbound.py:302-362` durably records pending before the
  carrier POST and refuses another POST after an ambiguous outcome. Only an
  exact tool receipt clears the draft.

No page, note, exported lookup field, model status, or tool result was found
that can create or confirm a send.

### LiteLLM, queue failover, and Playwright

- Routed models need explicit, consistent, finite pricing and a positive charge
  dimension at `first-officer/litellm/configure.py:53-104`. Every lane must
  include reachable fallback aliases at lines 78-98, and fail-closed budget and
  fallback settings are written at lines 107-120.
- Lane-key crash recovery uses a durable caller-selected key before remote
  creation (`first-officer/litellm/configure.py:315-344`). Runtime and management
  HTTPS calls require an allowlisted private destination and use connection
  pinning.
- Hatchet falls back only on definite refusal; timeouts, redirects, HTTP 408,
  server errors, and ambiguous transport failures remain unknown and are not
  sent to Restate (`first-officer/lanes/queue.py:43-49`, 196-233, 352-377).
- Active, expired, and orphaned quarantine entries remain red. The production
  Playwright CLI re-derives results from the source report, so a hand-edited
  events file cannot turn the gate green.

The full PostgreSQL/LiteLLM acceptance fixture was not run locally because
LiteLLM, Prisma, and PostgreSQL binaries are absent. Its unit paths ran in the
offline suite.

### Operator documentation

`JEFF-ACTIONS.md`, `LIVE-TEST.md`, and `INSTALL.md` accurately separate offline
checks, activation, Telegram acceptance, and live rollback. They do not direct
the reviewer to contact a live service. `INSTALL.md:115-120` also accurately
states that deleting `root.id` with every other audit copy remains undetected.
The installed verifier and loopback carrier implementation remain external to
this checkout, so their production behavior was not proven here.

## Verification

- `OPENAI_API_ENABLED=0 RUN_COMM_SMOKE=0 bash scripts/test_integration.sh`:
  passed; secret scan clean, 1028 Python tests passed, 22 handoff tests passed,
  and live communication was skipped.
- `node --test tests/first_officer/segment1.test.mjs tests/first-officer/captain.test.mjs`:
  23 passed.
- Eight exact regressions named in the `FIXES` section of
  `REVIEW-e8a23ac.md`: passed.
- Build, activate, and rollback `release.py --dry-run`: passed.
- `git diff --check origin/codex/first-officer-segment-1...HEAD`: passed before
  this report was written.
- Safe audit-reset and scanner-coverage repros: reproduced as described above.

## Review status

- Changed by this review: `REVIEW-95f2493.md` only. The pre-existing untracked
  `REVIEW-fdc9d05.md` was not changed.
- Tests added or updated: none; this was a read-only review with temporary
  offline repros.
- Final release status: **FAIL**.
- Remaining risks: complete owner-level audit reset, incomplete credential
  detection, and external production verifier/carrier behavior.

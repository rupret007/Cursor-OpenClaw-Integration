FAIL

# Independent release review — 935480b

Reviewed exact head `935480baca0fc85100d3971059ed1930a0bb54cc` on
`codex-candace/fo-finish-20261120`, draft
[PR #36](https://github.com/rupret007/Cursor-OpenClaw-Integration/pull/36),
against `origin/codex/first-officer-segment-1` at
`3d350baac7636fe068d5be6299991f5e776259b5`.

No live gateway, bobserver, Telegram, carrier, private-message service, or
production endpoint was contacted or changed. No secret, network address, or
personal contact value is reproduced here.

## Findings

### HIGH — A failed rollback from an interrupted pair reactivates the candidate

Locations: `first-officer/install.py:265-266`,
`first-officer/install.py:464-493`, `first-officer/install.py:500-510`,
`tests/first_officer/test_install.py:167-177`, `INSTALL.md:243-252`

The repeated-rollback fix is real only when the complete original pair is
already present. `switch()` also deliberately accepts either mixed saved pair
so an interrupted switch can be resumed. During rollback, `before` is the
candidate. If the observed config and pin are mixed, line 492 therefore chooses
the candidate as `recovery`. When starting the restored original fails its
readiness check, the exception path writes and starts that candidate pair.

The command exits nonzero but says that the prior configuration and service
were restored. The candidate is actually active again. The existing test
explicitly expects this candidate recovery for both mixed rollback entry pairs,
so the green suite codifies the unsafe behavior.

Safe offline repro executed with the existing installer fixture:

1. Prepare the saved original and candidate pairs.
2. Put the original config and candidate pin live.
3. Make the first `wait_ready()` fail and the recovery attempt pass.
4. Call `switch(..., restore=True)`.

Observed: `failed_mixed_rollback_reactivated_candidate True`.

The complete-original repeated rollback in
`test_readiness_failure_keeps_the_complete_pair_observed_at_entry` does stay on
the original pair. The production requirement is therefore **PARTIAL**, not
fixed for the interrupted states that `switch()` accepts.

### MEDIUM — Deleting/replacing the audit anchor still blesses a zero-line chain

Locations: `first-officer/acceptance/cycle.py:73-77`,
`first-officer/acceptance/cycle.py:252-277`,
`first-officer/acceptance/cycle.py:355-387`,
`first-officer/acceptance/cycle.py:423-438`,
`first-officer/acceptance/cycle.py:539-564`,
`first-officer/acceptance/cycle.py:716-785`,
`tests/first_officer/test_cycle.py:110-219`,
`JEFF-ACTIONS.md:67-72`, `INSTALL.md:115-124`

The new default anchor blocks the earlier four-directory reset while that file
survives. It is still another owner-writable copy. Deleting the root, trust,
witness, identity, and default anchor lets `initialize()` create a new
checkpoint and lets `verify()` return zero lines.

`FO_AUDIT_ANCHOR` fails closed when its configured file is missing or
mismatched. Replacing that file with a new empty owner file is treated as
provisioning and permits the same replacement chain. Unsetting the variable
also removes the configured path from the verification decision.

Safe offline repro results:

```
default_anchor_kept_blocks_reset True
default_anchor_deleted_accepts_zero_lines True
configured_missing_blocks_reset True
configured_empty_replacement_accepts_zero_lines True
```

The operator documents disclose these results. Their proposed durable placement
is not implemented by this program on a normal filesystem: reading requires the
file and directory to be owned by this user, and every audit append replaces
the anchor in that directory. A location this user cannot replace or delete
cannot be updated by the current writer. An administrator-owned helper, remote
witness, or other independently protected monotonic store is still needed.

### MEDIUM — Uppercase bearer credentials bypass every enforced scan mode

Locations: `first-officer/privacy/scan.py:38-43`,
`first-officer/privacy/scan.py:141-159`,
`tests/first_officer/test_privacy.py:20-29`,
`tests/first_officer/test_privacy.py:242-258`, `INSTALL.md:152-160`

The bearer scheme is now case-insensitive, but the later token filter ignores
every token made only of uppercase letters, digits, and underscores. That
exempts a documented environment-variable fixture, but it also exempts valid
all-uppercase alphabetic or alphanumeric bearer credentials. Source,
history, and files-only candidate scans all share this filter.

The safe generated-value repro printed no credential:

```
uppercase_alpha_bearer_detected False
uppercase_alnum_bearer_detected False
lowercase_alpha_bearer_detected True
```

The fine-grained GitHub token fix, mixed/lowercase bearer schemes with a
lowercase token, temporary cloud session keys, and the prior lowercase
alphabetic bearer all pass their regressions. The broader bearer credential
claim remains partial because the fixture exemption is shape-based rather than
an exact fixture value.

### MEDIUM — `--llm-eval` removes the LiteLLM configuration it claims to use

Locations: `scripts/andrea_experience_cycle.py:40-45`,
`services/andrea_sync/experience_assurance.py:310-396`,
`services/andrea_sync/experience_assurance.py:3500-3504`,
`services/andrea_sync/conversation_eval.py:1638-1653`,
`services/andrea_sync/conversation_eval.py:3234-3254`

The CLI now says `--llm-eval` calls the verifier through Andrea's LiteLLM lane.
`run_experience_assurance()` requests preservation for that mode, but
`ExperienceHarness` preserves only `OPENAI_*`; it removes every `ANDREA_*`
variable, including the LiteLLM URL, key, and model. It does not put those
three values into the harness environment. `run_llm_evaluator()` consequently
returns `None`, and the case adds no LLM observation or failure.

Safe offline repro: configure a synthetic local LiteLLM target, confirm
`litellm_configured()` before the harness, then enter
`ExperienceHarness(preserve_openai_for_eval=True)` and check again.

Observed:

```
litellm_configured_before_harness True
litellm_configured_inside_llm_eval_harness False
```

There is no regression test for this production CLI path. The flag silently
does less verification than its help text promises.

### MEDIUM — The scanner fix was not applied to production credential guards

Locations: `first-officer/acceptance/cycle.py:35-36`,
`first-officer/acceptance/cycle.py:567-598`,
`first-officer/qa/flaky.py:23-33`,
`first-officer/qa/flaky.py:61-69`, `first-officer/qa/flaky.py:127-133`,
`first-officer/usage/track.py:23-28`,
`first-officer/usage/track.py:115-137`,
`first-officer/usage/track.py:146-165`

The enforced checkout/history/artifact scanner now detects fine-grained
GitHub tokens, case-insensitive bearer schemes, temporary cloud session keys,
and alphabetic bearer values. Three other production guards still use the old
expression:

- acceptance memory accepts those values into `pending.json` and can save and
  recall them despite claiming that credentials are refused;
- the Playwright producer copies them from error text into its events artifact
  instead of redacting them;
- the usage handoff accepts a fine-grained token in a failure field and can
  commit it to `HANDOFF.md`.

The safe generated-value repro printed booleans only. Every tested shape was
accepted by memory and retained by the Playwright redactor; the fine-grained
token was also retained in a generated usage handoff.

This is independent of the enforced scanner's uppercase bearer bypass: even
credential forms that the scanner does catch can be persisted through these
newly added data-leak and repository-contamination paths.

### LOW — Activation advertises rollback before a rollback backup exists

Locations: `first-officer/release.py:179-186`,
`first-officer/release.py:228-234`,
`tests/first_officer/test_release.py:303-312`

`activate()` prints the private rollback path before running any plan step.
Failures in inventory, preflight, archive verification, or digest verification
therefore leave that path missing or incomplete, while the generic failure
message still points at it. The existing early-inventory-failure test requires
both messages even though `install.py prepare` was never reached.

The hint is conditional on the gateway having been switched, and the actual
switch cannot precede backup preparation, so this does not create another
unsafe restore path. It is nevertheless misleading recovery guidance during a
failed production activation.

## Prior finding disposition

| Prior finding / FIXES claim | Status | Production evidence |
| --- | --- | --- |
| Failed repeated rollback reactivates the candidate | **PARTIAL** | `first-officer/install.py:489-509` preserves a complete pair observed at entry, and the exact complete-original regression passes. For an accepted mixed pair, rollback still selects candidate as recovery and reactivates it after original readiness fails; see the HIGH finding. |
| Deleting every owner-writable audit copy blesses a replacement chain | **OPEN** | `first-officer/acceptance/cycle.py:539-564` refuses reset while the new anchor survives. Deleting that owner-writable anchor as well, or replacing a configured anchor with an empty file, still produces a verified zero-line chain. |
| Hand-edited Playwright events can turn the gate green | **FIXED** | The production `evaluate` command passes both artifacts at `first-officer/qa/flaky.py:289-299`; `produce_events()` re-derives from the report and checks its digest at lines 307-323. |
| Secret gate misses temporary cloud keys and alphabetic bearer tokens, then fine-grained tokens and lowercase bearer schemes | **PARTIAL** | `first-officer/privacy/scan.py:38-43` has the requested prefixes and case-insensitive scheme, and source, artifact, and history all call `findings_in_text()` at lines 141-175, 200-246, and 360-407. Both named regressions pass. Lines 154-159 still exempt every uppercase-only/alphanumeric token, so a valid bearer credential bypasses every mode. The separate stale guards are another finding above. |
| Local write failure can orphan active LiteLLM lane keys | **FIXED** | `first-officer/litellm/configure.py:315-340` locks the directory and durably creates/reuses the pending caller-selected key before remote creation, verifies remote policy, then promotes it. |
| Fetched text is exported outside its fence | **FIXED** | `first-officer/guard/boundary.py:120-151` fences all exported summaries when untrusted data contributed. The hybrid runner returns through that boundary before unrestricted OpenClaw at `scripts/andrea_sync_openclaw_hybrid.py:567-577`; the server unwraps only for display at `services/andrea_sync/server.py:210-229` and `3949-4010`. |
| Install documentation verifies before read-only permissions | **FIXED** | Publication, move confirmation, read-only permissions, installed verification, and switch are ordered at `first-officer/release.py:92-105`; `INSTALL.md:225-233` matches. |

## Whole-diff release conclusions

### Build, activation, rollback, and dry-run

- `first-officer/stage.py:156-168` binds the overlay to the exact reviewed HEAD
  and a clean checkout. The build scans the staged candidate before packing at
  `first-officer/release.py:37-58`.
- Activation checks the running service, owner login, existing reader, digest,
  inventory, and private rollback before publication at
  `first-officer/release.py:61-98`. The candidate is readable and read-only
  before installed verification.
- Candidate validation preserves an active, mixed, or journaled candidate at
  `first-officer/install.py:397-443`.
- Successful rollback restores the saved original config and pin and waits for
  readiness. Failed mixed-pair rollback is the HIGH blocker above.
- Build, activation, and rollback dry-runs each exited zero and invoked no plan
  command. They created only local temporary directories.

The release step label at `first-officer/release.py:101` still says “before
publish” even though the store-path publication happened at line 96. This is a
wording nit; verification still precedes the gateway switch.

### Prompt injection, approval, audit, budgets, queues, and quarantine

- Lookup routes get only their route allowlist at
  `services/andrea_sync/server.py:4579-4584` and return before the unrestricted
  agent at `scripts/andrea_sync_openclaw_hybrid.py:567-577`. Fetched pages,
  local messages, state, and durable memory are fenced. No lookup-to-send path
  was found.
- Draft binding covers action, target, exact body, principal, origin chat,
  expiry, and nonce at `services/andrea_sync/outbound.py:26-93`. The server
  rechecks context at `services/andrea_sync/server.py:4058-4074`. Pending is
  durable before carrier I/O, and ambiguous replies remain pending rather than
  being posted twice.
- The hash chain binds each line and multiple checkpoints, but all local
  checkpoints remain owner-resettable as described above.
- LiteLLM policy requires finite consistent prices, positive charge, explicit
  fallback access, and fail-closed budget settings at
  `first-officer/litellm/configure.py:53-120`. Management and runtime requests
  are destination-checked and connection-pinned. Key crash recovery is fixed.
  The separate evaluation harness integration is broken.
- Hatchet fails over only on definite refusal; ambiguous transport and response
  outcomes stay blocked on the primary at `first-officer/lanes/queue.py:196-233`
  and `324-377`.
- Playwright evaluation re-derives attempts, report errors, and inventory from
  the source report. Quarantine cannot make the gate green. Its separate
  credential redactor is incomplete.
- The release privacy gate catches the newly tested forms, but its broad
  uppercase bearer carve-out remains a candidate and history bypass.

### Operator documentation

`LIVE-TEST.md` safely separates approved live chat checks from offline review
and explicitly excludes rollback and local infrastructure. `INSTALL.md`
matches the current activation order. `JEFF-ACTIONS.md` accurately discloses
the owner-writable audit reset, but the suggested protected anchor placement
cannot be maintained by the current owner-only replacement writer. The
installed verifier and carrier remain external to this checkout, so their
production implementations were not proven.

Two operator nits remain. `JEFF-ACTIONS.md:11-14` shows an activation variable
without creating its parent directory or repeating the required gateway lane
key prerequisite from `INSTALL.md:50-51`. Also, `INSTALL.md:235-243` describes
rejected-candidate deletion unconditionally, while
`first-officer/install.py:397-421` deliberately preserves the directory if the
complete prior pair is not live or a switch journal exists. Both cases fail
safe, but the short runbooks are not self-contained descriptions of production
behavior.

## Verification

- `OPENAI_API_ENABLED=0 RUN_COMM_SMOKE=0 bash scripts/test_integration.sh`:
  passed; scanner clean across 395 files and 350 reachable commits, 1033
  Python tests passed, 22 handoff tests passed, exhaustive checks passed, and
  live communication was skipped.
- Focused audit, scanner, installer, release, guard, LiteLLM, queue,
  Playwright, and outbound suite: 137 passed.
- `node --test tests/first_officer/segment1.test.mjs tests/first_officer/captain.test.mjs`:
  23 passed.
- Build, activate, and rollback `release.py --dry-run`: all exited zero without
  running their plans.
- `git diff --check origin/codex/first-officer-segment-1...HEAD`: passed before
  this report was written.
- Safe targeted repros confirmed the mixed-pair rollback, audit reset,
  uppercase bearer scan bypass, stripped LiteLLM evaluation configuration, and
  stale credential guards.
- The real LiteLLM/PostgreSQL and tracing acceptance programs could not be run
  locally because their pinned packages and database binaries are absent.
  Their unit paths ran in the offline suite.

## Review status

- Changed file: `REVIEW-935480b.md` only.
- Tests added or updated: none; this was a read-only review with temporary
  offline repros.
- Final release status: **FAIL**.
- Remaining risks: failed mixed-pair rollback, owner-resettable audit history,
  bearer scan bypasses, silently disabled LiteLLM evaluation, incomplete
  secondary credential guards, and external verifier/carrier behavior.

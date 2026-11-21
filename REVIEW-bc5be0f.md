FAIL

# Independent release re-review — bc5be0f

Reviewed exact head `bc5be0f54e4f6303fae316de27c5df699c0370cb` on
`codex-candace/fo-finish-20261120`, draft PR #36, against
`origin/codex/first-officer-segment-1` at
`3d350baac7636fe068d5be6299991f5e776259b5`.

No live gateway, bobserver, Telegram, carrier, or production action was
invoked. No secret, network address, or personal contact value is reproduced
here.

## Findings

### HIGH — A confirmed carrier send can be repeated after local receipt failure

Locations: `services/andrea_sync/outbound.py:194-225`,
`services/andrea_sync/server.py:4051-4084`

The normal approval path is bound to the exact target, body, principal, origin,
expiry, and nonce, and the server clears the draft only after an exact carrier
receipt. That part of the prior fix is real.

The failure ordering is still unsafe. `deliver()` calls the carrier at
`outbound.py:216`, then serializes, appends, flushes, and syncs the local
receipt at `outbound.py:217-221`. A process exit or local serialization/write
failure after the carrier has delivered but before that record is durable
leaves no receipt. The production server catches the failure and keeps the same
draft at `server.py:4055-4082`. A later confirmation calls the carrier again.
The request includes the digest, but neither the checked-in protocol nor the
absent carrier implementation requires an idempotent lookup or send.

Safe offline repro: inject a carrier that returns one exact successful receipt,
raise during local serialization immediately after that response, restore the
serializer, and call `deliver()` again with the same draft. The carrier is
called twice. The executed repro printed
`confirmed_send_retried_after_record_failure True`.

This can send the same real message twice while the first attempt was reported
as failed. Persist a durable pending operation before carrier I/O and require
the carrier to deduplicate or query by the approval digest.

### MEDIUM — Deleting all owner-writable audit anchors still resets the chain

Locations: `first-officer/acceptance/cycle.py:178-227`,
`first-officer/acceptance/cycle.py:240-275`,
`tests/first_officer/test_cycle.py:103-127`

The claimed fix adds `witness_dir(root)` beside the prior trust base and checks
both copies. Both copies, the acceptance root, and the HMAC key remain
owner-writable local state. `_check_head()` still accepts a chain as fresh when
the rows, local head, key, trusted head, and witness are all absent.

The regression test deletes the root and one anchor while deliberately keeping
the other; it separately deletes only the witness while keeping the trust
anchor. It never deletes all actual owner-writable state.

Safe offline repro: run one routine in a temporary root, remove the root,
`trust_dir(root)`, and `witness_dir(root)`, then call `verify(root)`. It returns
`0`; the executed repro printed `complete_owner_writable_reset_accepted True`.
The prior complete-reset finding remains **OPEN**. Replicating a deletable
checkpoint does not create a trust anchor.

### MEDIUM — Redirect authorization is not scoped to the active lookup route

Locations: `first-officer/guard/restricted_fetch.py:219-238`,
`first-officer/guard/boundary.py:217-257`,
`tests/first_officer/test_guard.py:620-727`

`_redirect_ok()` searches every source list and accepts a hop when the current
and target URLs occur together in any one of them. `fetch()` is not passed the
active route. If page A is a news source and is also listed for research, page
A may redirect a news lookup to page B that is listed only for research.
`boundary.run_lookup()` passes the complete allowlist, so this reaches the live
lookup call site.

The new regression test keeps route lists disjoint and therefore does not cover
the overlap. Safe offline repro: list A for news, list A and B for research,
make A redirect to B, and run `fetch(A, document)` with an offline opener. Both
pages are opened and B is returned; the executed repro printed
`cross_route_redirect_allowed True`.

The page remains address-checked and fenced, so this is not an outbound-send
bypass. It is nevertheless a real violation of the documented exact-source-
for-the-same-route boundary. The prior redirect finding is **PARTIAL**.

### MEDIUM — The production candidate verifier still runs only after publication

Locations: `first-officer/release.py:73-99`,
`first-officer/install.py:133-180`,
`first-officer/install.py:210-246`,
`first-officer/install.py:359-420`,
`tests/first_officer/test_install.py:310-330`

Digest comparison, complete inventory verification, and private rollback
preparation correctly precede publication. The new verifier command also uses
the service pin's three-argument contract.

However, `verify_config(..., candidate=root)` deliberately skips
`installed_verifier()` at `install.py:150-161`. `prepare()` therefore verifies
the prior release with the installed verifier but checks the candidate only
with repository code and the OpenClaw config CLI. `release.py` then publishes
the candidate at `release.py:90-95`. The installed candidate verifier runs in
`switch()` at `install.py:390`, after publication but before the service is
stopped.

The focused test explicitly proves there is no candidate verifier call during
prepare and adds that call only after copying the candidate into the store.
An installed-verifier rejection leaves the gateway untouched, but it also
leaves the immutable candidate directory published; the same activation then
fails the no-republish check. Thus the prior “verify candidate before publish”
finding remains **PARTIAL**. The installed verifier itself is absent from this
checkout, so its production behavior remains unreviewable.

### MEDIUM — The secret/PII history gate skips merge-resolution-only content

Locations: `first-officer/privacy/scan.py:315-373`,
`scripts/test_integration.sh:73-78`,
`tests/first_officer/test_privacy.py:51-119`

`scan_history()` uses `git log --all -p` at `scan.py:342`. Git does not emit
ordinary merge-commit patches for that command. A credential introduced only
while resolving one merge and removed only while resolving a later merge is
absent from all scanned non-merge patches. Existing tests cover normal commits,
renames, and quoted paths but no merge resolution.

Safe offline repro: create two conflicting merges in a temporary repository,
put a synthetic credential only in the first merge result, remove it only in
the second merge result, and call `scan_history()`. It returns no credential
finding; the executed repro printed `merge_only_history_credential_missed
True`.

This repository has reachable merge commits. A separate offline scan that
included per-parent merge diffs found the current 313-commit history clean, so
no exposed value was found in this head. The enforced scanner is still
bypassable and must inspect merge results.

## Prior finding disposition

| Prior finding / claimed fix | Disposition | Production evidence |
| --- | --- | --- |
| Restricted lookup routes / prompt-injection fence | **FIXED** | The server derives the route tools at `services/andrea_sync/server.py:4545-4552`; the hybrid runner returns through `boundary.lookup_result()` without launching the unrestricted agent at `scripts/andrea_sync_openclaw_hybrid.py:567-577`; page and message data stay fenced at `first-officer/guard/boundary.py:217-296`. The cross-route redirect above is a source-scope gap, not a tool or send bypass. |
| Exact outbound approval and verified delivery | **PARTIAL** | Exact draft and receipt binding is enforced at `services/andrea_sync/outbound.py:34-111`, and the live server requires that receipt at `services/andrea_sync/server.py:4033-4084`. The carrier/receipt ordering can repeat a confirmed send as described above. |
| Playwright report-level errors | **FIXED** | Producer errors survive at `first-officer/qa/flaky.py:120-134`; report errors and exact inventory make evaluation fail at `first-officer/qa/flaky.py:185-251`; the CLI evaluates the validated producer output at `first-officer/qa/flaky.py:270-295`. This remains a manual CLI and is not invoked by the PR workflow. |
| Verify candidate before publish | **PARTIAL** | Manifest, inventory, and rollback preparation precede publish at `first-officer/release.py:73-95`, but the installed candidate verifier runs only afterward at `first-officer/install.py:390`. |
| Complete audit reset | **OPEN** | Two external copies are checked at `first-officer/acceptance/cycle.py:258-275`, but deleting the root and both owner-writable copies restores fresh-state success. |
| Unsafe LiteLLM pricing | **FIXED** | Routed prices must be present, consistent, numeric, finite, and nonnegative, with a positive charge dimension, at `first-officer/litellm/configure.py:53-103`. |
| Private LiteLLM HTTPS | **FIXED** | Management and runtime require the exact private host list and pass checked addresses into the pinned transport at `first-officer/litellm/configure.py:139-177`, `first-officer/litellm/configure.py:208-229`, `services/andrea_sync/litellm_api.py:33-81`, and `services/andrea_sync/litellm_api.py:152-170`. |
| Directory symlinks and named data/artifact trees | **FIXED** | Directory symlinks are findings and named trees are walked at `first-officer/privacy/scan.py:156-181`; the scan is enforced before pytest at `scripts/test_integration.sh:73-78`. |
| Ambiguous Hatchet failover | **FIXED** | Only definite refusal statuses fail over; timeouts, server errors, and ambiguous transport outcomes stop dispatch at `first-officer/lanes/queue.py:43-49`, `first-officer/lanes/queue.py:196-233`, and `first-officer/lanes/queue.py:352-374`. |
| Queue credential destination | **FIXED** | Named and literal private HTTPS endpoints require the queue allowlist and connect only to checked addresses at `first-officer/lanes/queue.py:92-184` and `first-officer/lanes/queue.py:242-263`. |
| Historical pathname, rename, and quoted-path scan | **FIXED** | Both patch sides and rename/copy metadata are decoded and scanned at `first-officer/privacy/scan.py:224-373`. The merge-result bypass above is a separate history-coverage gap. |
| Usage threshold overrides | **FIXED** | Invalid, non-finite, or looser warning/stop values are rejected at `first-officer/usage/track.py:84-101`, and CLI values flow through that check at `first-officer/usage/track.py:180-203`. |
| Exact-source redirect (`efaadb1`) | **PARTIAL** | Globally unlisted redirect targets are refused, but `restricted_fetch.py:219-238` cannot prove that the target belongs to the active route. |
| Installed verifier argument contract (`23a9932`) | **PARTIAL** | `first-officer/install.py:133-148` constructs the service pin's three arguments and `switch()` invokes them before stopping the gateway. The executable is external, and moving its candidate call after publish leaves the ordering issue above. |

## Focused release conclusions

### Prompt-injection and delivery boundaries

Live lookup routes are connected to the restricted boundary and do not launch
the unrestricted agent. Fetched text cannot become an outbound tool call. The
remaining lookup problem is cross-route page selection.

Outbound confirmation is bound to the exact draft, and an absent or inexact
carrier response fails closed. The release is still blocked because an
ambiguous post-delivery local failure can cause a second carrier call. The
carrier implementation is external and was not exercised.

### Activation and rollback

`release.py activate` orders gateway and identity preflights, extraction,
approved digest comparison, inventory verification, and rollback preparation
before publication. It publishes before the production candidate verifier, as
described above.

`install.switch()` accepts each saved original/candidate config-and-pin pair,
verifies the target before stopping the service, writes the target pair, starts
it, and waits for readiness. On a catchable failure it restores the command's
prior complete pair. Re-running rollback from either mixed interruption state
converges to the saved original config and pin. Subject to the absent external
verifier, rollback does restore the prior gateway configuration, verifier pin,
and active service. It does not remove the published candidate directory.

### Secret/PII scan and gate strength

The enforced scan still checks regular files, logs, ordinary reachable-history
patches, pathnames, renames, directory symlinks, and named data/artifact trees.
The standard scan and a separate merge-aware scan were clean. The merge gap
above means the gate is not complete.

No workflow step was removed. The branch adds full-history checkout and
separate LiteLLM and tracing jobs. Across modified pre-existing tests, no Python
test function disappeared, no existing Python test had fewer AST assertions,
and the modified Node file gained one test and ten assertion calls. No gate or
assertion weakening was found. The Playwright quarantine checker remains
manual rather than a PR job.

## Verification performed

- Exact head, branch, clean starting worktree, draft PR base/head metadata, and
  four successful exact-head PR jobs verified.
- `git diff --check origin/codex/first-officer-segment-1...HEAD`: passed.
- `RUN_COMM_SMOKE=0 bash scripts/test_integration.sh`: passed; standard secret
  scan clean, 988 Python tests passed, 22 handoff tests passed, and live
  communication was skipped.
- `node --test tests/first_officer/segment1.test.mjs tests/first_officer/captain.test.mjs`:
  23 passed.
- Safe offline adversarial repros: complete owner-writable audit reset accepted;
  cross-route redirect accepted; confirmed carrier call repeated after local
  receipt failure; merge-resolution-only credential missed.
- Separate merge-aware history scan: clean across 313 reachable commits.
- Post-report enforced secret/PII scan: clean.

## Review status and remaining risks

- Changed file: `REVIEW-bc5be0f.md` only.
- Tests added or updated: none; this was a read-only review with temporary
  offline repros.
- Final release status: **FAIL**. One HIGH outbound side-effect issue and four
  MEDIUM integrity, route-scope, release-ordering, and scan-coverage issues
  remain.
- Remaining external risks: the installed production verifier and loopback
  carrier implementation are absent from the checkout. No live gateway,
  carrier, private-message service, or production rollback was validated.

## FIXES

Each finding was reproduced with a failing test, then fixed. The original FAIL above is unchanged. Local gates after these commits: secret scan clean (`Files 364`, `History commits 320`, `Logs 0`, before the commit that records this section; the file count includes the local gitignored `data/` tree), `bash scripts/test_integration.sh` passed 994 Python tests, and the Segment 1 and captain Node gate passed 23 tests. No live gateway, bobserver, Telegram, or production action was used. Phoenix, Restate and Hatchet were not installed.

The server file fence moved in `ba35b9bfc680aa9bb232a572f29772859fa599b1` so the gate still pins `services/andrea_sync/server.py` after the pending-send change. The confirm phrases in that test are unchanged.

| Finding | Commit | Test |
| --- | --- | --- |
| HIGH — a confirmed carrier send can be repeated after local receipt failure | `d845dff936b2a693f5bd1856b7c08aab09b8f569` Do not call the carrier again when a pending send has no receipt. | `tests/test_outbound_carrier.py::test_confirmed_send_is_not_retried_after_local_receipt_failure` and `tests/test_outbound_carrier.py::test_the_server_surfaces_an_unrecorded_confirmation_for_manual_check` |
| MEDIUM — deleting all owner-writable audit anchors still resets the chain | `80b2b54beffa7b3d9b6fababe13f3e8c75e7d41b` Report an empty audit chain when no undeletable trust anchor exists. | `tests/first_officer/test_cycle.py::test_deleting_every_owner_writable_audit_copy_is_not_a_silent_pass` |
| MEDIUM — redirect authorization is not scoped to the active lookup route | `004c7517132aa6ae417e1ff8a459ab8974dfc282` Scope a redirect to the active route's source list. | `tests/first_officer/test_guard.py::test_a_redirect_stays_on_the_active_route_source_list` |
| MEDIUM — the production candidate verifier still runs only after publication | `6af6c7ba7fc24a6dd954430bb1100342adbc3383` Verify the candidate before the release is made read-only. | `tests/first_officer/test_install.py::test_prepare_verifies_the_unpacked_candidate_and_rollback_restores_the_prior_bundle` and `tests/first_officer/test_release.py::test_activate_checks_the_digest_and_writes_rollback_before_publish` |
| MEDIUM — the secret/PII history gate skips merge-resolution-only content | `747e727fff796000f3fa00697dd233c85280596f` Scan merge results for secrets that never appear in a normal patch. | `tests/first_officer/test_privacy.py::test_history_scans_a_credential_that_exists_only_in_a_merge` |

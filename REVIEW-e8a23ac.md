FAIL

# Independent release review — e8a23ac

Reviewed exact head `e8a23acbaf0795bfd25a5907343916ff989242b9` on
`codex-candace/fo-finish-20261120`, draft PR #36, against
`origin/codex/first-officer-segment-1`.

No live gateway, bobserver, Telegram, carrier, or production service was
contacted or changed. No secret, network address, or personal contact value is
reproduced here.

## Findings

### HIGH — A failed idempotent rollback reactivates the candidate

Location: `first-officer/install.py:462-503`

`switch()` chooses its recovery pair solely from the requested direction:
rollback always sets `before` to the candidate pair, even when the live config
and pin are already the complete original pair. If a repeated rollback reaches
`start(original)` and readiness fails, the exception handler writes `before`
and starts it. That reactivates the candidate the operator had already rolled
back.

Safe offline repro: prepare and activate temporary config/pin files, complete
one rollback, then call `switch(..., restore=True)` again with the first
readiness check failing and the recovery readiness check succeeding. The live
bytes after the raised error equal the saved candidate pair
(`rollback_reactivated_candidate=true`).

This is reachable after an operator retry, or after a process loses its final
success response. Recovery must preserve the actual complete pair observed at
entry, and an already-restored rollback should not fall back to the candidate.
Add readiness-failure coverage for all accepted original/candidate pair
combinations.

### MEDIUM — Total audit deletion can be blessed as a new valid chain

Locations: `first-officer/acceptance/cycle.py:264-289`,
`first-officer/acceptance/cycle.py:350-363`, `INSTALL.md:115-119`

When the audit log, local key/head, trust head, and witness head are all absent,
`_check_head()` returns false rather than retaining evidence that this root
previously existed. `initialize()` then creates a new key and three new heads.
The same owner that can delete the files can therefore erase the history,
invoke `init`, and obtain a valid empty chain.

Safe offline repro: initialize a temporary root, append a routine, delete the
root plus `trust_dir(root)` and `witness_dir(root)`, call `initialize(root)`,
then call `verify(root)`. The result is zero verified lines
(`reset_chain_verifies=true`).

The hash chain detects alteration only while at least one owner-writable copy
survives. A separately protected, durable root identity/checkpoint needs to be
enforced by code; an operator convention is not an audit trust anchor.

### MEDIUM — A hand-edited Playwright event artifact can turn the gate green

Location: `first-officer/qa/flaky.py:300-329`

`produce_events()` validates the shape of the intermediate JSON and recomputes
the outcome from the attempts that remain, but it does not bind that artifact
to the original Playwright report. Removing a failed attempt, leaving one
retry-zero pass, and changing `outcome` to `expected` passes validation and the
unchanged expected inventory.

Safe offline repro: produce events from one failed attempt followed by a passed
retry; replace `attempts` with one retry-zero pass and set `outcome` to
`expected`; run `produce_events()` and `evaluate()` with no quarantine and the
original inventory. The result is `edited_events_gate_green=true`.

Quarantine entries themselves remained non-passing in review. The bypass is
the mutable intermediate artifact. Evaluate the source report in the same
operation, or bind the events to an immutable source-report digest.

### MEDIUM — The secret gate misses common credential forms

Locations: `first-officer/privacy/scan.py:38-47`,
`first-officer/privacy/scan.py:153-156`

The cloud access-key rule recognizes the long-lived prefix but not the common
temporary-session prefix. The bearer rule also deliberately ignores a token
unless it contains a digit. A temporary cloud credential set or an
alphabetic bearer credential can therefore pass both checkout and candidate
scans.

Safe offline repro: `findings_in_text("ASIA" + "A" * 16)` and
`findings_in_text("Bearer " + "a" * 16)` both return an empty list. These are
constructed fixtures, not credentials.

Cover temporary credentials and token assignments without printing matched
values; add regression tests for source, history, and files-only candidate
scans.

### MEDIUM — A local write failure can orphan active LiteLLM lane keys

Location: `first-officer/litellm/configure.py:290-310`

When no credential file exists, `provision_file()` creates the remote virtual
key before durably writing the `.pending` file. A crash or write failure in
that window leaves an active key with no local credential or recovery record.
A retry can create another key and another independent budget.

Safe offline repro: replace `create_lane()` with a call counter and
`write_private()` with a synthetic disk failure, then invoke
`provision_file()` twice. Two remote creates are observed while neither the
pending nor final credential exists (`remote_keys_created=2`,
`durable_credential_exists=false`).

Use a server-side idempotency/recovery identifier, or revoke the generated key
when local persistence fails. Document and test crash recovery.

### LOW — Fetched text is also exported outside its fence

Locations: `first-officer/guard/boundary.py:120-140`,
`first-officer/guard/boundary.py:252-268`,
`scripts/andrea_sync_openclaw_hybrid.py:567-577`

The restricted route correctly short-circuits before launching the unrestricted
agent, and no page, message, memory note, or model status produced a send in
review. However, `_finish()` copies the visible untrusted body into `summary`,
`user_summary`, and `raw_text` as ordinary strings while also returning the
fenced copy. Those ordinary fields satisfy `visible_to_tools()`.

This is not a current send bypass because the lookup route has no outbound tool
and the server renders the result directly. It does mean the fence is not the
actual security boundary, contrary to the documentation; route isolation is.
Keep untrusted provenance on every exported field or avoid exporting an
unfenced duplicate that a future consumer could feed to a model or tool parser.

### LOW — INSTALL.md gives the wrong verifier/permission order

Locations: `INSTALL.md:223-233`, `first-officer/release.py:94-103`,
`JEFF-ACTIONS.md:23-31`

The release command now makes the published candidate read-only and traversable
before `verify-candidate`, which fixes the prior production permission failure.
`INSTALL.md` still shows and describes verification before the read-only
permission step. `JEFF-ACTIONS.md` describes the implemented order correctly.

Following the INSTALL block opens a mutable first-verification window, although
`install.py activate` verifies again after the permission change. Make the
operator block exactly match `release.py`.

## Controls that held

- Build is bound to a full reviewed commit and a clean checkout, scans the
  staged candidate, and refuses archive replacement.
- Activation preflights service state, operator identity, and the read-only
  Andrea bridge before publication; digest, inventory, and private rollback are
  checked before the switch.
- Successful rollback restores the exact saved config and pin and waits for
  readiness. The blocking defect is its failure recovery when rollback is
  repeated from an already-original pair.
- `--dry-run` executes no plan command and does not contact the gateway. It does
  create and remove a local temporary directory.
- Lookup routes do not launch the unrestricted OpenClaw agent. Fenced memory is
  omitted when malformed. Outbound delivery requires the exact confirmation
  phrase, principal/chat binding, an unexpired exact draft digest, and a
  matching carrier receipt. Ambiguous delivery remains pending and is not
  retried.
- Active, expired, and orphaned Playwright quarantine entries all keep the
  evaluation red.
- `JEFF-ACTIONS.md` and `LIVE-TEST.md` are otherwise consistent with the
  implemented approval and live-test boundaries.

## Verification

- Sanitized `bash scripts/test_integration.sh`: passed; secret scan clean,
  1022 Python tests passed, 22 handoff tests passed, and all live probes/smoke
  were skipped.
- `node --test tests/first_officer/segment1.test.mjs tests/first_officer/captain.test.mjs`:
  23 passed.
- Focused First Officer, outbound-carrier, and Andrea LiteLLM unit tests:
  175 passed.
- `git diff --check origin/codex/first-officer-segment-1...HEAD`: passed.
- Build, activate, and rollback dry-run commands: passed.
- All four PR jobs report passing, including the LiteLLM and tracing acceptance
  jobs.

The full LiteLLM/PostgreSQL acceptance fixture was not rerun locally because
its pinned dependencies and database binaries are not installed in this
environment; its unit coverage passed and the exact-head PR acceptance job is
green. No live acceptance or production rollback was attempted.

## Review status

- Changed file: `REVIEW-e8a23ac.md` only.
- Tests added or updated: none; this was a read-only review with temporary
  offline repros.
- Final release status: **FAIL**.
- Remaining external risks: the installed production verifier, carrier
  implementation, live gateway behavior, and real proxy state are outside this
  checkout and were not exercised.

## FIXES

Each finding was reproduced with a failing test, then fixed, except the audit residual stated below. The original FAIL above is unchanged. Local gates after these commits: secret scan clean (`Files 388`, `History commits 345`, `Logs 0`, before the commit that records this section; the file count includes the local gitignored `data/` tree), `bash scripts/test_integration.sh` passed 1028 Python tests, and the Segment 1 and captain Node gate passed 23 tests. No live gateway, bobserver, Telegram, or production action was used. Phoenix, Restate and Hatchet were not installed. Draft PR #36 was not merged.

The server file fence moved in `83b82d13c20c21fae86817f91210a1fa54309c71` so the gate still pins `services/andrea_sync/server.py` after the lookup display unwrap. The confirm phrases in that test are unchanged.

Deleting the acceptance root, the trust directory, and the witness is refused while `root.id` remains. Deleting `root.id` as well still lets `initialize` mint a new empty chain that `verify` accepts with zero lines. That full reset is not fixed.

| Finding | Commit | Test |
| --- | --- | --- |
| HIGH — a failed idempotent rollback reactivates the candidate | `de964dea33acf7521ce705313a5725612e1fd789` Keep rollback recovery on the complete pair already live. | `tests/first_officer/test_install.py::test_readiness_failure_keeps_the_complete_pair_observed_at_entry` |
| MEDIUM — total audit deletion can be blessed as a new valid chain | `c6c8f9e7f77af45fbdfdb9a2c71a4210f90513c2` Refuse a replacement audit chain while the root identity remains. The review repro is refused while `root.id` remains. Deleting `root.id` too is not fixed: a new empty chain still verifies. | `tests/first_officer/test_cycle.py::test_reinitializing_after_deleting_the_log_and_heads_does_not_verify` |
| MEDIUM — a hand-edited Playwright event artifact can turn the gate green | `a1c5065b24e281d7e3acf7ce2cc008259f13cda1` Evaluate the Playwright report instead of a hand-edited events file. | `tests/first_officer/test_flaky.py::test_a_hand_edited_attempt_cannot_turn_the_gate_green` |
| MEDIUM — the secret gate misses common credential forms | `2ce6166f9226af7f605b5213dd4193811bf81c94` Flag temporary cloud access keys and alphabetic bearer tokens. | `tests/first_officer/test_privacy.py::test_temporary_session_keys_and_alphabetic_bearers_fail_closed` |
| MEDIUM — a local write failure can orphan active LiteLLM lane keys | `768eea61d9e71310004d4154896399bd0a5110e0` Write the LiteLLM lane key before asking the proxy to create it. | `tests/first_officer/test_litellm_policy.py::test_a_failed_local_write_does_not_leave_a_second_active_key` |
| LOW — untrusted fetched text exported unfenced | `4615dda4b29eec5a563ecce9da3427f8c0c4d049` Fence fetched lookup text in the exported summary fields. | `tests/first_officer/test_guard.py::test_normal_lookup_queries_work_through_the_fenced_boundary` |
| LOW — INSTALL.md verifies before the read-only permission step | `e41d1b79fba3b6afd2390b7d7217c40eb1095eaa` Verify the published candidate after its directory is read-only. `8f50bd29aa504cc8bb78ad8bd7f6fd2a63ef87ce` pins that order. | `tests/first_officer/test_release.py::test_install_doc_verifies_the_candidate_after_it_is_read_only` and `tests/first_officer/test_release.py::test_one_activate_command_preflights_publishes_and_switches_in_order` |

FAIL

# Independent release re-review — 4046f4a

Reviewed exact head `4046f4a5f6240228b2d90999941928cd31a53562` on
`codex-candace/fo-finish-20261120`, draft PR #36, against
`origin/codex/first-officer-segment-1` at
`3d350baac7636fe068d5be6299991f5e776259b5`.

No live gateway, bobserver, Telegram, carrier, or production action was invoked.
No secret or personal contact value is reproduced here.

## Findings

### MEDIUM — Deleting the actual owner-writable audit anchor still resets the chain

Locations: `first-officer/acceptance/cycle.py:178-225`,
`first-officer/acceptance/cycle.py:237-245`,
`tests/first_officer/test_cycle.py:104-116`

The fix moves the trusted head outside the acceptance directory's parent. That
protects against deleting the old root and its old sibling, but it does not
protect against the prior finding: deletion of all owner-writable trust state.
The new anchor is created and kept owner-writable. If the acceptance root and
`trust_dir(root)` are both removed, `_check_head()` sees no rows, no local head,
no key and no trusted head, and accepts the directory as fresh.

The regression test deletes the acceptance root and the obsolete sibling path;
it does not delete the actual path returned by `trust_dir()`.

Safe offline repro: create a temporary acceptance root, run one routine, remove
both the root and `trust_dir(root)`, then call `verify(root)`. It returns `0`.
The prior complete-reset finding remains **PARTIAL**.

### MEDIUM — An exact source may redirect to an unlisted page

Locations: `first-officer/guard/restricted_fetch.py:104-120`,
`first-officer/guard/restricted_fetch.py:139-158`,
`first-officer/guard/restricted_fetch.py:219-230`

The initial lookup target now comes only from the route's exact `sources`
entries, so the prior prompt-selected-path bug is fixed. A redirect is checked
only against the host allowlist, however; it is not required to equal an exact
source. An exact source can therefore redirect the fetch to any path on any
allowlisted host.

Safe offline repro: inject `_open()` so the reviewed source returns one redirect
to a different path on the same allowed host. `fetch()` opens both targets and
returns the second page in a fence even though that page is absent from every
route source.

The redirected response remains address-checked and fenced, and it cannot
invoke an outbound tool. This is not a send bypass, but it violates the
documented exact-page boundary and has no regression test.

## Prior finding disposition

| Prior finding / claimed fix | Disposition | Production evidence |
| --- | --- | --- |
| Restricted lookup routes (`9cede96`) | **FIXED** | The server supplies the route allowlist at `services/andrea_sync/server.py:4499-4552`; the hybrid runner returns through the restricted boundary before launching OpenClaw at `scripts/andrea_sync_openclaw_hybrid.py:566-578`. Empty fetched text is refused at `first-officer/guard/boundary.py:250-264`, and public dual-stack answers are accepted and connection-pinned at `first-officer/guard/restricted_fetch.py:123-158` and `first-officer/guard/restricted_fetch.py:178-230`. The separate redirect finding above remains. |
| Exact outbound delivery (`62c46b3`, `4df9543`) | **FIXED** | Approval binds action, target, exact body, principal, origin, expiry and nonce at `services/andrea_sync/outbound.py:34-108`. The production server calls the delivery function and clears the draft only after an exact receipt at `services/andrea_sync/server.py:4051-4084`. Delivery now calls a loopback carrier first, requires its exact confirmation, and only then records it at `services/andrea_sync/outbound.py:119-180` and `services/andrea_sync/outbound.py:194-225`. With no carrier or an inexact response, it fails closed. |
| Playwright report-level errors (`d5e4ac0`) | **FIXED** | Producer errors survive at `first-officer/qa/flaky.py:122-134`; report errors and exact inventory contribute to failure at `first-officer/qa/flaky.py:198-251`; the CLI evaluates the validated producer output at `first-officer/qa/flaky.py:270-295`. |
| Verify candidate before publish (`d822c29`) | **PARTIAL** | Digest, inventory and rollback preparation precede publication at `first-officer/release.py:73-97`. `prepare()` verifies the unpacked inventory and asks the installed verifier to validate the candidate at `first-officer/install.py:192-229`. The production verifier under the release store is still not in this repository, so its candidate-argument contract cannot be reviewed; `tests/first_officer/test_install.py:218-303` supplies a synthetic verifier. A contract mismatch fails before publication, but production compatibility remains unproven here. |
| Complete audit reset (`e7fcb72`) | **PARTIAL** | The anchor moved outside the acceptance parent at `first-officer/acceptance/cycle.py:178-225`, but deleting that owner-writable anchor together with the root still restores fresh-state success, as reproduced above. |
| Unsafe LiteLLM pricing (`3d31b06`) | **FIXED** | Routed prices must be present, numeric, finite, nonnegative and consistent, and each routed deployment needs a positive charge dimension at `first-officer/litellm/configure.py:53-103`. |
| Private LiteLLM HTTPS (`6dc9972`) | **FIXED** | Management and runtime clients pass the checked addresses to the pinned transport at `first-officer/litellm/configure.py:158-178`, `first-officer/litellm/configure.py:208-235`, and `services/andrea_sync/litellm_api.py:104-170`. The transport dials those addresses directly while preserving hostname verification at `first-officer/pinhttp.py:17-78`. |
| Directory symlinks and named trees (`eb96181`) | **FIXED** | Directory symlinks are findings and named data/artifact trees are walked at `first-officer/privacy/scan.py:156-181`; the enforced scan runs before tests at `scripts/test_integration.sh:73-78`. |
| Ambiguous Hatchet failover (`1de23b6`) | **FIXED** | Only definite refusal statuses fail over; timeouts, server errors and ambiguous transport failures become uncertain outcomes and stop dispatch at `first-officer/lanes/queue.py:43-49`, `first-officer/lanes/queue.py:196-233`, and `first-officer/lanes/queue.py:352-374`. |
| Queue credential destination (`43259d6`, `dc0fd1b`) | **FIXED** | Named and literal private HTTPS endpoints require the exact queue allowlist, and requests use checked addresses, at `first-officer/lanes/queue.py:92-184` and `first-officer/lanes/queue.py:242-263`. |
| Historical pathname and rename scan (`7f00504`, `f476497`) | **FIXED** | Git C-quoted tokens and metadata paths are decoded and both patch sides are scanned at `first-officer/privacy/scan.py:236-373`. |
| Usage threshold overrides (`aad0759`) | **FIXED** | Non-finite, invalid, or looser warning/stop values are rejected at `first-officer/usage/track.py:84-101`, and CLI overrides flow through that check at `first-officer/usage/track.py:176-203`. |

## Focused release review

### Prompt-injection and delivery boundaries

Lookup routes do not launch the unrestricted agent. The production server adds
the exact route tool list, the hybrid runner branches into the boundary, and the
boundary admits no outbound tool. Fetched and local-message text is fenced and
returned as data. The new redirect finding broadens which public page may be
read, but it does not turn page text into a tool call.

Outbound approval is bound to the exact draft, and the production call site
requires an exact carrier receipt before clearing that draft. The carrier
implementation itself is external to this checkout and was not exercised; the
checked-in path stays disabled without it.

### Activation and rollback

`release.py` orders gateway preflight, archive extraction, manifest comparison,
inventory verification and rollback preparation before publication, then
publishes before activating (`first-officer/release.py:65-99`). Rollback invokes
the installer restore before checking service activity
(`first-officer/release.py:103-108`).

`install.switch()` now accepts every saved original/candidate config-and-pin
combination, writes the requested target pair, starts that target, and restores
the prior complete pair on a catchable failure
(`first-officer/install.py:341-404`). Repeating activation or rollback recovers
the mixed pair left by interruption between the two replacements. The focused
regression exercised all four pairs. Subject to the external installed-verifier
limitation above, rollback genuinely restores the saved prior config and pin
and waits for that gateway to become ready.

### Secret and PII gate

The enforced scanner walks regular files, logs and reachable full history,
redacts sensitive pathnames, rejects symlinks and binary files outside its
documented local-runtime database exception, and now decodes quoted Git paths.
The final scan was clean. No runtime database is tracked in this head, and the
review archive includes only its explicit text trees.

No workflow step or integration-gate line was removed. Since `acc0167`, no test
function was removed; two changed assertions were replaced by stronger or
equivalent checks, while twelve regression tests and ninety-seven assertions
were added. No gate or assertion weakening was found. The Playwright quarantine
checker remains a manual CLI and is not wired to a PR Playwright report.

## Verification performed

- Exact head, branch, merge-base and draft PR metadata verified. All four PR
  jobs report success.
- `git diff --check origin/codex/first-officer-segment-1...HEAD`: passed.
- `RUN_COMM_SMOKE=0 bash scripts/test_integration.sh`: passed; secret scan
  clean, 986 Python tests passed, 22 handoff tests passed, and live
  communication was skipped.
- `node --test tests/first_officer/segment1.test.mjs tests/first_officer/captain.test.mjs`:
  23 passed.
- Focused outbound, installer, guard, privacy, audit, queue and LiteLLM tests:
  68 passed.
- Safe offline adversarial repros: deletion of all actual owner-writable audit
  state still verifies as a fresh chain; a redirect reaches a page absent from
  the exact route sources.

## Review status and remaining risks

- Changed file: `REVIEW-4046f4a.md` only.
- Tests added or updated: none; this was a read-only review with temporary
  offline repros.
- Final release status: **FAIL**. No new HIGH issue was found, but one prior
  integrity finding remains reproducible and the exact-source boundary has a
  new MEDIUM gap.
- Remaining external risks: the installed production verifier and loopback
  carrier implementation are absent from the checkout; no live gateway or
  carrier behavior was validated.

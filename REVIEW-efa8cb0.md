# Independent review of efa8cb0

The initial verdict is **FAIL**. Two independent reviewers examined `3733bf1..efa8cb0` and exercised the packaged tracker and handoff entry points with disposable local data. The review preserves the original findings below. The runtime checks used fixtures and public CLI documentation.

## Original findings

| Check | Verdict | Evidence |
| --- | --- | --- |
| Cursor schema, freshness and billing flags | PASS | Missing, malformed, stale, expired, future or token-required receipts fail the Cursor window with exit 1. Duplicate keys fail. Each of the five flags must be the JSON boolean false. |
| Central Time | PASS | The focused gate includes both CST and CDT resets. |
| Threshold precision | FAIL | Floating-point utilization and six significant display digits can show 90% for a value below 90% while allowing a launch. A value sufficiently close to 97% rounds up to the stop threshold. |
| Repeated usage warnings | FAIL | Two identical 90% tracker invocations produced two commits in a disposable branch. The second commit changed only the resume SHA to the previous handoff commit. |
| Handoff file scope and privacy | FAIL | A handoff committed only `HANDOFF.md` and preserved the unrelated staged edit. The fixed pending filename is not safe to reuse. The handoff guard omitted scanner rules for phone, email and IPv6, and a malformed reset could print a derived invalid year. |
| Same-login Cursor evidence | FAIL | A timestamp and disabled flags establish the shape of a receipt. The CLI accepted an unbound file without checking that it was private or attested to the operator-verified login. |
| CLI credential and execution boundary | FAIL | The fixture saw alternate auth, provider and endpoint variables inside the child environment. The read-only invocation lacked explicit Ask mode, and both prompts in edit mode asked to cancel other jobs. The sole CLI launch inherited standard input. |
| CLI dry-run and diagnostic | FAIL | Dry-run declared an unread plan ready and reported the API model for a CLI handoff. Diagnostic dispatch used the presence of a Cloud key instead of the resolved backend and could run Cloud reads in CLI mode or dry-run. |
| CLI output privacy | FAIL | Captured CLI output and validation errors could be printed verbatim. |
| Existing offline gates | PASS | The original focused command passed 41 tests in a clean archive. The existing offline workflow exercises fixture-only Cursor submission and prevents live smoke and exhaustive API calls in the normal offline gate. |

Cursor documents Ask mode and explicit print mode for a read-only CLI invocation. [Cursor CLI modes](https://cursor.com/docs/cli/using) · [Cursor headless operation](https://cursor.com/docs/cli/headless)

## Re-review findings carried to the fixes

| Check | Verdict | Evidence |
| --- | --- | --- |
| Direct wrapper output | FAIL | A synthetic child printed private markers on both streams when launched directly through the shell wrapper. |
| Preview privacy | FAIL | A project dotenv value appeared in CLI SSL diagnostics. A synthetic marker crossing the preview limit exposed a prefix because the preview was clipped before scanning. |
| Repository code trust | FAIL | The handoff looked for the receipt validator in the target checkout and working directory before the installed module. The dry-run path must treat target checkout files as data. |
| Concurrent handoff and Git hooks | FAIL | A disposable checkout switched to a protected branch just before the commit and received the handoff there. A hook in a second checkout added an unrelated file to the partial commit. |
| Receipt and stream failures | FAIL | An explicit FIFO receipt blocked diagnosis, and receipt metadata could expose a private marker in a CLI response. Invalid UTF-8 at the stream cap expanded past the intended output limit. Omitting the required direct CLI mode started the editable path. |
| Operator handoff | FAIL | The earlier instructions depended on variables spanning the Nuc and Bobserver. The final service status step did not request rollback after a successful switch when that status check failed. |

## FIXES

| Finding | Fix commit | Regression evidence | Re-review |
| --- | --- | --- | --- |
| Threshold precision and malformed reset | `b833f6b` | `test_cursor_use_exact_thresholds_and_display_below_the_cutoff`, `test_cursor_keeps_a_precise_stricter_threshold_in_decision_and_display`, and `test_cursor_invalid_date_preserves_only_the_fixed_error_label`. | PASS |
| Duplicate handoff, unrelated staged paths and Git races | `b833f6b` | `test_repeated_cli_warning_reuses_the_same_resume_commit_until_lane_changes`, `test_handoff_commit_tree_and_ref_publish_never_run_repo_hooks`, protected-branch, concurrent-tip, and post-publication checkout tests. | PASS |
| Private handoff and receipt binding | `b833f6b` | `test_handoff_refuses_scanner_personal_data_without_emitting_values`, account-binding mismatch, owner, mode, symlink, hardlink and FIFO cases in `test_usage.py`. | PASS |
| CLI-only launch and configuration provenance | `b833f6b` | `test_cli_backend_uses_headless_ask_and_only_eligible_operator_environment`, `test_project_dotenv_cannot_supply_subscription_receipt` and the existing CLI and auto fallback matrix. | PASS |
| CLI preview and diagnostic accuracy | `b833f6b` | `test_cli_dry_run_reports_real_model_and_validates_the_local_plan` and `test_cli_and_dry_run_diagnostics_do_not_call_cursor`. | PASS |
| Direct output bounds, redaction and mode | `b833f6b` | `test_cli_output_with_credentials_and_personal_fields_is_withheld`, oversized stream, invalid UTF-8, and explicit direct-mode cases in `test_cursor_handoff_model_flag.py`. | PASS |
| Preview metadata and doctor receipt safety | `b833f6b` | SSL metadata omission, scan-before-truncation, receipt projection redaction, and FIFO and oversized doctor receipt tests. | PASS |
| Shipped receipt validator | `b833f6b` | `test_cli_reads_project_receipt_data_without_loading_project_code`. | PASS |
| Single activation and recovery | `d944af6`, `b833f6b` | `test_verifier_preflight_refuses_missing_or_symlink_paths`, `test_activation_recovers_only_after_successful_switch`, and an offline preview of the exact guide block with live commands replaced by failing sentinels. | PASS |

## Final review verdict

**PASS** on the reviewed source at `b833f6be4bedff6d4cf074e3b6b04c2c13925946`. The independent re-review passed 54 tracker tests and the four final wrapper regressions, with two receipt subtests. The combined focused gate passed 71 tests. Release and installer tests passed 58 tests. The WSL baseline first exposed Node missing from the shell path. The unchanged baseline gates passed after the existing Node directory was added to the path.

[PR #37 checks](https://github.com/rupret007/Cursor-OpenClaw-Integration/pull/37/checks) show the CI results on the published head. The review covers local and fixture evidence. Jeff checks the live Cursor profile and receipt, runs the timed Telegram checks and authority pilot, and verifies the live rollback. [Jeff's activation and validation guide](JEFF-ACTIONS.md)

# First Officer handoff

Resume on branch `codex-candace/fo-finish-20261120`. The commit that contains this file is the resume point. Use `git rev-parse HEAD`. Draft PR #36 targets `codex/first-officer-segment-1`.

## Done in this checkout

Steps 1 through 8 have local evidence in `STATUS.md`. Step 8 rehearses the authority pilot on a staged lease, including a missed-check-in failover, and prints the live checklist without running it. The offline gate passed 938 Python tests. The Node gate passed 23 tests. The four pull-request jobs were green on `3cd946c`. Later commits, including this one, need those jobs green again. The live lease was not touched.

## Next

The eight ordered steps are evidenced locally. Next is the rollback drill on a staged copy, with the timing recorded. Then the secrets and PII scan, the release archive, the doc refresh, and the Cursor plan-usage check. Do not activate the live pilot.

## Open failures

None in the local gates at this commit. Confirm the pull-request jobs are green on this head. Gateway activation, timed Telegram checks and the live authority pilot still wait for Jeff.

## Do not

No merges, no pushes to main, no force pushes, no deploys, no live gateway or Telegram changes, and no texts. Do not write to bobserver. Do not touch colima-vw, Vaultwarden, Podman, asgard, or Jeff's sessions.

# First Officer handoff

Resume on branch `codex-candace/fo-finish-20261120`. The commit that contains this file is the resume point. Use `git rev-parse HEAD`. Draft PR #36 targets `codex/first-officer-segment-1`.

## Done in this checkout

Steps 1 through 7 are proven in `STATUS.md`. Step 7 saves a fact only when the approval digest matches, records that on a hash-chained audit log, and dry-runs the 07:00 America/Chicago lane review without sending a message. The offline gate passed 935 Python tests. The Node gate passed 23 tests. The four pull-request jobs were green on the Step 6 head `3cd946c`. This commit adds the acceptance cycle and needs those jobs green again.

## Next

Step 8. Prepare the supervised authority pilot and a tested failover checklist. Anything that touches the live gateway waits for Jeff. Do not send a message.

## Open failures

None in the local gates at this commit. Confirm the pull-request jobs are green on this head. Gateway activation, timed Telegram checks and the supervised authority pilot still wait for Jeff.

## Do not

No merges, no pushes to main, no force pushes, no deploys, no live gateway or Telegram changes, and no texts. Do not write to bobserver. Do not touch colima-vw, Vaultwarden, Podman, asgard, or Jeff's sessions.

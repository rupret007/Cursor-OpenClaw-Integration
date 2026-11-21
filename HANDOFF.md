# First Officer handoff

Resume on branch `codex-candace/fo-finish-20261120`. The commit that contains this file is the resume point. Use `git rev-parse HEAD`. Draft PR #36 targets `codex/first-officer-segment-1`.

## Done in this checkout

Steps 1 through 5 are proven in `STATUS.md`. Step 5 reads a Playwright JSON report, keeps every attempt, and treats quarantine as non-passing. The offline gate passed 928 Python tests. The four pull-request jobs were green on the Step 4 head `3256157`. This commit adds the flaky-report gate and needs those jobs green again.

## Next

Step 6. Finish the shared status view and the read-only admin console, bound to loopback or the tailnet.

## Open failures

None in the local gates at this commit. Confirm the pull-request jobs are green on this head. Gateway activation, timed Telegram checks and the supervised authority pilot still wait for Jeff.

## Do not

No merges, no pushes to main, no force pushes, no deploys, no live gateway or Telegram changes, and no texts. Do not write to bobserver. Do not touch colima-vw, Vaultwarden, Podman, asgard, or Jeff's sessions.

# First Officer handoff

Resume on branch `codex-candace/fo-finish-20261120`. The commit that contains this file is the resume point. Use `git rev-parse HEAD`. Draft PR #36 targets `codex/first-officer-segment-1`.

## Done in this checkout

Steps 1 through 8 have local evidence in `STATUS.md`. The staged rollback drill is also done: the prior pin was restored on a private copy in 114 us, and the gateway was not touched. The offline gate passed 940 Python tests. The Node gate passed 23 tests. The four pull-request jobs were green on `3cd946c`. Later commits, including this one, need those jobs green again. The live lease was not touched.

## Next

Scan the repository, reachable Git history and relevant logs for secrets and personal data, and add a test that keeps the scan enforced. Then build the release archive, refresh `CAPTAIN.md`, `INSTALL.md` and `LIVE-TEST.md`, and check that Cursor fallback stays inside plan usage. Do not activate the live pilot or roll back the live gateway.

## Open failures

None in the local gates at this commit. Confirm the pull-request jobs are green on this head. Gateway activation, timed Telegram checks, the live authority pilot and a live rollback still wait for Jeff.

## Do not

No merges, no pushes to main, no force pushes, no deploys, no live gateway or Telegram changes, and no texts. Do not write to bobserver. Do not touch colima-vw, Vaultwarden, Podman, asgard, or Jeff's sessions.

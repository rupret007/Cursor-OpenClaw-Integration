# First Officer handoff

Resume on branch `codex-candace/fo-finish-20261120`. The commit that contains this file is the resume point. Use `git rev-parse HEAD`. Draft PR #36 targets `codex/first-officer-segment-1`.

## Done in this checkout

Steps 1 and 2 are proven in `STATUS.md`. Step 3 is proven there too. `first-officer/lanes/queue.py` queues one review lane on Hatchet and calls Restate only after a definite refusal. `tests/first_officer/test_lane_queue.py` passed 16 tests on Python 3.12.15 and Python 3.14.4. The offline gate passed 918 Python tests and the Node gate passed 22. Hatchet and Restate are not installed. The proof uses loopback servers that speak the documented HTTP contracts. Nothing was deployed.

## Next

Step 4. Integrate Phoenix and OpenLLMetry and prove a trace arrives at a local collector. Do not use a hosted collector and do not spend money.

## Open failures

None in the local gates at this commit. Confirm the pull-request jobs are green on this head before treating CI as finished. Gateway activation, timed Telegram checks and the supervised authority pilot still wait for Jeff.

## Do not

No merges, no pushes to main, no force pushes, no deploys, no live gateway or Telegram changes, and no texts. Do not write to bobserver. Do not touch colima-vw, Vaultwarden, Podman, asgard, or Jeff's sessions.

# OpenClaw x Cursor Implementation Roadmap

## Prioritized Backlog (Impact x Effort)

- **P1: Cursor lifecycle expansion** (`status`, `followup`, `conversation`, `artifacts`)
  - **Impact:** Very high
  - **Effort:** Medium
  - **Why now:** Moves from one-shot handoff to full iterative agent collaboration.

- **P1: `cursor_handoff` diagnostics + reliability hardening**
  - **Impact:** Very high
  - **Effort:** Low/Medium
  - **Why now:** Solves recurring auth/env/SSL issues and reduces failed runs.

- **P1: Intent templates (code review, refactor, release notes, creative briefs)**
  - **Impact:** High
  - **Effort:** Medium
  - **Why now:** Improves output quality and consistency across Jeff-style workflows.

- **P2: Pre-handoff repo triage**
  - **Impact:** High
  - **Effort:** Medium
  - **Why:** Provides better context to Cursor and lowers ambiguity.

- **P2: Multi-agent orchestration**
  - **Impact:** High
  - **Effort:** High
  - **Why:** Parallelizes backend/frontend/docs tracks for faster delivery.

- **P3: Project-manager bridge (GitHub issue/PR sync)**
  - **Impact:** Medium/High
  - **Effort:** High
  - **Why:** Strong operational value once core workflows are stable.

- **P3: Creative ops bundle generation (campaign kits from code changes)**
  - **Impact:** Medium/High
  - **Effort:** Medium/High
  - **Why:** High value for musician/creator workflows after engineering core is solid.

## Sprint Plan

## Sprint 1 (Now): Reliability + Operator UX

- Add `--diagnose` mode to `cursor_handoff`.
- Add API retry/backoff and timeout controls.
- Improve read-only prompt behavior (avoid misleading branch instructions).
- Add Python unit test suite and stronger smoke tests.
- Update docs with troubleshooting playbook.

**Definition of done**
- Tests pass locally.
- Diagnose output clearly identifies common failure classes.
- Retry behavior validated in unit tests.

## Sprint 2: Lifecycle Commands + Templates

- Add companion `cursor_ops` command set:
  - list/status/followup/stop/delete/conversation/artifacts/download-url.
- Add intent template system (`--intent` + context bundle support).
- Add output schema normalization for all command paths.

**Definition of done**
- One command can launch, inspect, iterate, and collect artifacts.
- Intent templates produce consistent prompt scaffolds.

## Sprint 3: Orchestration + PM + Creative Bridge

- Add multi-agent orchestrator with aggregate status.
- Add GitHub issue/PR sync helpers.
- Add creative deliverable templates (release notes/social variants/campaign briefs).

**Definition of done**
- Parallel workstreams supported.
- Agent runs map cleanly to project artifacts and communication outputs.

## Suggested Next Action

Implement Sprint 1 in `skills/cursor_handoff` first, then validate with real API credentials and publish a short operator runbook.

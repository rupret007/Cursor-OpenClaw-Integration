# OpenClaw x Cursor Integration Proposal

## Context

This proposal analyzes the current `cursor_handoff` skill and recommends new integration patterns for OpenClaw to better leverage Cursor Cloud Agents and local Cursor CLI for coding, creative production, and project management use cases.

Primary audience: a hybrid user profile (developer + creative + musician) who needs fast switching between implementation work, planning, and content execution.

## Current State Analysis (`cursor_handoff`)

### What is already strong

- Clear backend strategy: API-first with CLI fallback (`auto`, `api`, `cli`).
- Safe-by-default behavior for ambiguous tasks (read-only policy in skill guidance).
- Input normalization for local path, GitHub URL, and `owner/repo`.
- Basic robustness:
  - auth-mode fallback (`bearer` then `basic`)
  - dry-run mode
  - initial status polling
  - structured JSON output
- Good baseline docs and smoke test script.

### Current gaps and friction

- Single operation focus: mostly "submit and optionally poll", not a full agent lifecycle toolkit.
- No first-class support for follow-up prompts, stop/delete, conversation retrieval, artifacts listing/download.
- Limited diagnostics for common field issues:
  - missing env propagation across shell/gateway contexts
  - SSL trust store mismatch (`urllib` cert errors)
  - entitlement mismatch (`/v0/me` works but `/v0/agents` fails)
- Prompting is generic and branch-centric, which can confuse read-only tasks when branch refs do not exist remotely.
- No intent templates for non-coding workflows (creative ideation, campaign copy generation, release planning).
- No queueing/orchestration model for multiple concurrent Cursor jobs triggered by OpenClaw.

## Proposed New Integration Ideas

## 1) Cursor Operations Skill (Full Agent Lifecycle)

- **Description**
  - Expand from one "handoff" action to a full skill surface:
    - `whoami`, `models`, `list-agents`, `agent-status`
    - `create-agent`, `followup`, `stop-agent`, `delete-agent`
    - `conversation`, `artifacts`, `artifact-download-url`
- **Benefits**
  - OpenClaw can manage long-running agent workflows directly in chat.
  - Better debugging and visibility for incomplete/failed jobs.
  - Enables iterative collaboration with the same agent instead of relaunching.
- **Implementation (high level)**
  - Add a new Python CLI module under workspace skill scripts.
  - Keep shared auth/retry/diagnostics layer.
  - Return standardized JSON for every command so OpenClaw can parse and summarize consistently.

## 2) Intent-Based Prompt Templates (Coding + Creative + PM)

- **Description**
  - Add prompt templates for common intents:
    - code refactor / failing tests / security review
    - release notes / changelog / migration guides
    - artist campaign planning / social content drafts / livestream run-of-show support
- **Benefits**
  - Higher quality first-pass results.
  - Less manual prompt engineering by user.
  - Better fit for Jeff-style cross-domain tasks (dev + creative ops).
- **Implementation (high level)**
  - Introduce `--intent` and optional `--context-file` flags.
  - Map intents to structured prompt skeletons with objective, constraints, acceptance criteria, output format.
  - Preserve `--prompt` override for expert users.

## 3) Multi-Agent Orchestration for Parallel Work

- **Description**
  - Allow OpenClaw to spawn and track multiple Cursor agents at once for independent tracks:
    - one for backend fixes
    - one for frontend polish
    - one for docs/test plan
- **Benefits**
  - Faster turnaround on broad tasks.
  - Separation of concerns with cleaner outputs.
  - Useful for fast pre-release cycles.
- **Implementation (high level)**
  - Add orchestration command:
    - accepts a JSON spec of subtasks
    - launches one agent per subtask
    - polls statuses
    - returns aggregate summary and URLs
  - Include configurable concurrency cap.

## 4) Artifact-Aware Output Pipelines

- **Description**
  - Turn Cursor artifacts into first-class OpenClaw assets:
    - screenshots, clips, generated docs, patch notes, verification reports.
- **Benefits**
  - Better handoff from coding work to communication and publishing workflows.
  - Supports creative project workflows (promo assets, show materials, content packs).
- **Implementation (high level)**
  - Extend skill with artifact list/download helpers.
  - Add optional post-processing hooks to move artifacts into known workspace folders.
  - Generate concise artifact index markdown for downstream use.

## 5) Automated Repo Triage Mode (Pre-Handoff Analyzer)

- **Description**
  - Before sending work to Cursor, run a lightweight OpenClaw analyzer:
    - detect repo type
    - test/build commands
    - failing checks
    - branch/remote status
- **Benefits**
  - Better prompts with concrete context.
  - Fewer failed agent runs due to missing assumptions.
  - Faster debugging cycle.
- **Implementation (high level)**
  - Add `triage` subcommand using safe shell probes.
  - Inject findings into generated prompt as "Known environment facts."
  - Cache triage summary per repo/session.

## 6) Project-Manager Bridge (Issue/PR-Centric Workflow)

- **Description**
  - Integrate Cursor jobs with issue/PR lifecycle:
    - launch from issue links
    - update issue comments with status snapshots
    - generate PR summaries and test plans
- **Benefits**
  - Better traceability and team collaboration.
  - Reduces manual reporting overhead.
  - Aligns with structured project workflows.
- **Implementation (high level)**
  - Build wrappers that connect `gh` metadata + Cursor agent IDs.
  - Add status sync command (`sync-agent-to-issue`).
  - Use markdown templates for consistent updates.

## 7) Creative Ops Companion Mode

- **Description**
  - Use Cursor for non-code outputs tied to creative workflows:
    - campaign outlines from code/product changes
    - audience-specific copy variants
    - release messaging kits from changelog and commit history
- **Benefits**
  - Leverages Cursor reasoning for content and planning, not just code edits.
  - Bridges development work and public communication for musician/creator workflows.
- **Implementation (high level)**
  - Add creative intents with strict output schemas.
  - Pull repo diffs and notes as input context.
  - Return publish-ready markdown bundles.

## Improvements Specifically for Existing `cursor_handoff`

## A) Reliability hardening

- Add retry/backoff for `429` and `5xx` on create/status calls.
- Add configurable timeout and poll strategy.
- Add explicit SSL diagnostics:
  - detect Python cert path
  - recommend `certifi` fix when `CERTIFICATE_VERIFY_FAILED` appears.

## B) Better diagnostics and guardrails

- Add `diagnose` command:
  - checks env presence (`CURSOR_API_KEY`, base URL)
  - tests `/v0/me` and optional `/v0/agents?limit=1`
  - returns actionable troubleshooting.
- Validate read-only branch behavior:
  - for analysis tasks, omit branch line unless user requested.

## C) Better UX and output contracts

- Standardize output envelope:
  - `ok`, `backend`, `auth_mode`, `agent_id`, `status`, `agent_url`, `errors`, `next_actions`.
- Add concise human-readable mode plus JSON mode parity.

## D) Coverage and testing

- Expand tests to include:
  - auth fallback matrix
  - retry behavior
  - diagnostics command behavior
  - conversation/artifact endpoints
  - integration contract tests with mocked HTTP server.

## Suggested Roadmap

## Phase 1 (Quick Wins)

- Add lifecycle commands (`status`, `followup`, `conversation`, `artifacts`).
- Add `diagnose` command and SSL troubleshooting output.
- Improve read-only prompt handling and output schema.

## Phase 2 (Workflow Depth)

- Add intent templates and repo triage preflight.
- Add multi-agent orchestration command.
- Add issue/PR status sync hooks.

## Phase 3 (Creative + PM Expansion)

- Add creative-ops templates and asset bundle generation.
- Add artifact pipelines and auto-indexing.
- Add dashboard-ready summary outputs for recurring workflows.

## Feasibility Summary

- All ideas are feasible within OpenClaw’s existing model using `exec` and skill extension patterns.
- No platform changes are strictly required for Phase 1.
- Biggest value per effort:
  1. lifecycle commands
  2. diagnostics hardening
  3. intent templates
  4. triage mode
  5. orchestration

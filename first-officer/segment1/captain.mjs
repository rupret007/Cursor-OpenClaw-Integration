import { execFile } from 'node:child_process';
import { loadavg } from 'node:os';
import { dirname, isAbsolute, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { bounded } from './dispatch.mjs';
import { fleetSnapshotIsLocal, readFleetSnapshot, validLocalCapacity } from './tools.mjs';

const stateScript = fileURLToPath(new URL('./captain-state.py', import.meta.url));
const repo = 'repos/rupret007/Cursor-OpenClaw-Integration';
const sha = /^[a-f0-9]{40}$/;
const required = ['first-officer-segment1', 'offline-integration'];
const active = new Set(['launching', 'running']);
const blockers = {
  fleet: 'Wait for fleet evidence and runner capacity, then request the review again.',
  gate: 'Wait for a current idle gate reading, then request the review again.',
  quota: 'Check the approved runner’s quota or wait for its window to reset.',
  'ci-changed': 'The PR head changed. Request a review of its current head.',
  'ci-unavailable': 'Verify that the current PR head has receipts for both required GitHub checks.',
  'ci-pending': 'OpenClaw is waiting for required GitHub checks on the exact head.',
  'ci-timeout': 'CI exceeded the wait window. Check GitHub before requesting another review.',
  'ci-paused': 'Authority or the grant changed while this review was waiting for CI. Request it again if needed.',
};
const terminal = {
  verified: 'Required CI passed for that exact head. Ready for Jeff’s review.',
  'ci-failed': 'Required CI failed for that exact head. Jeff can request a fix or leave the PR open.',
  'head-changed': 'The PR head changed before verification. Request a fresh review of the new head.',
  'worker-failed': 'The lane failed to return a verified result. Inspect its local receipt before any retry.',
  'worker-timeout': 'The lane exceeded its verification window. Check the live worker before starting another run.',
  'ci-unavailable': 'GitHub could not verify the worker result against the current head. Recheck CI before using the result.',
};

export function resolveCaptainStateScript(config) {
  const paths = config?.plugins?.load?.paths;
  const pinnedConductor = /\/first-officer-[a-f0-9]{64}\/round6\/runtime\/first-officer-conductor\.mjs$/;
  const matches = Array.isArray(paths) ? paths.filter((path) =>
    typeof path === 'string' && isAbsolute(path) && pinnedConductor.test(path)) : [];
  if (matches.length > 1) throw new Error('Ambiguous pinned Captain release');
  // OpenClaw can load JavaScript from a capture that omits the packaged Python queue runtime.
  return matches.length ? join(dirname(matches[0]), 'segment1', 'captain-state.py') : stateScript;
}

export function localState(operation, payload = {}, script = stateScript) {
  const python = process.env.FO_CAPTAIN_PYTHON || '/usr/bin/python3';
  return new Promise((resolve, reject) => {
    const child = execFile('/usr/bin/nice', ['-n', '10', python, '-I', '-B', script, operation],
      {
        maxBuffer: 48 * 1024,
        ...(operation === 'submit' ? {} : {
          timeout: operation === 'inspect-project' ? 30_000 : operation === 'dispatch-project' ? 15_000 :
            ['runner-quota', 'local-quota'].includes(operation) ? 7000 : 2500,
        }),
      },
      (error, stdout) => {
        if (error) return reject(new Error('Captain state unavailable'));
        try { resolve(JSON.parse(stdout)); } catch { reject(new Error('Invalid captain state')); }
      });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(payload));
  });
}

export function integrationCI(pr, read) {
  return projectCI(repo, pr, required, read);
}

export async function projectCI(repository, pr, expectedChecks, read) {
  const path = repository + '/pulls/' + pr;
  const pull = await read(path);
  const identity = (row) => row?.number === pr && row.state === 'open' &&
    row.base?.repo?.full_name?.toLowerCase() === repository.slice(6).toLowerCase() &&
    sha.test(row.head?.sha ?? '');
  if (!identity(pull)) throw new Error('Open project PR identity unavailable');
  const head = pull.head.sha;
  const checks = await read(repository + '/commits/' + head + '/check-runs?per_page=100&filter=latest');
  const final = await read(path);
  if (!identity(final) || final.head.sha !== head) return { head, ci: 'changed' };
  if (!Array.isArray(checks?.check_runs) || checks.total_count !== checks.check_runs.length) {
    return { head, ci: 'unavailable' };
  }
  const valid = expectedChecks.map((name) =>
    checks.check_runs.filter((row) => row.app?.slug === 'github-actions' && row.name === name && row.head_sha === head));
  if (valid.some((rows) => rows.length !== 1)) return { head, ci: 'unavailable' };
  const statuses = valid.flat().map((row) => row.status === 'completed' ? row.conclusion : 'pending');
  if (statuses.includes('pending')) return { head, ci: 'pending' };
  return { head, ci: statuses.every((result) => result === 'success') ? 'passing' : 'failing' };
}

async function fleetAdmission(_signal, standalone, readState) {
  if (standalone) {
    const load = loadavg()[0];
    const capacity = await readState('local-capacity');
    if (!validLocalCapacity(capacity, standalone, Date.now()) || !Number.isFinite(load)) {
      return 'Local capacity evidence is unavailable.';
    }
    return load > 16 || capacity.used >= capacity.limit ? 'The Captain host is at its lane capacity.' : undefined;
  }
  const snapshot = await readFleetSnapshot();
  const clock = Date.now() - snapshot.ts * 1000;
  const gate = snapshot.host.locks?.find((row) => row.slot === 'fullgate');
  if (!fleetSnapshotIsLocal(snapshot) || clock < -30_000 || clock > 300_000 ||
      !Number.isSafeInteger(snapshot.host.heavy_jobs) || !Number.isSafeInteger(snapshot.host.heavy_max)) {
    return 'Fleet capacity evidence is stale or unavailable.';
  }
  if (typeof gate?.held !== 'boolean' || !Array.isArray(snapshot.gates)) return 'Gate evidence is unavailable.';
  if (gate.held || snapshot.gates.some((row) => row.gate === 'running')) return 'The gate is busy.';
  if (loadavg()[0] > 16 || snapshot.host.heavy_jobs >= snapshot.host.heavy_max) return 'The Captain host is at its lane capacity.';
}

function role(view) {
  const { lease, armed, grant } = view;
  if (view.standalone && lease.state === 'parked') {
    return 'Captain · this host is parked · epoch ' + lease.epoch + '.';
  }
  const holder = lease.holder === 'first-officer' ? 'OpenClaw' : lease.holder === 'captain' ? 'Grok Bot' : 'unknown';
  const scoped = grant?.epoch === lease.epoch && holder === 'OpenClaw';
  return [
    'Captain · ' + holder + ' · epoch ' + lease.epoch + (lease.state === 'handback-pending' ? ' · handback pending.' : '.'),
    scoped ? 'Scope · Integration PR CI review · ' + (grant.paused || !armed ? 'dispatch paused.' : 'dispatch enabled.') :
      'OpenClaw has no active scope grant.',
  ].join('\n');
}

function jobText(job) {
  const identity = 'Integration PR #' + job.pr + ' · ' + job.head.slice(0, 8) + ' · ' + job.lane + '.';
  if (active.has(job.state) && job.overtime) return identity +
    'The runner exceeded its deadline and remains active. Jeff can inspect the local lane.';
  if (active.has(job.state) && job.queuePhase && job.queuePhase !== 'queued') {
    return identity + ' Queue acceptance is unresolved' + (job.backend ? ' on ' + job.backend : '') + '.';
  }
  if (active.has(job.state) && job.backend) return identity + ' Accepted by ' + job.backend +
    ' ' + Math.max(0, Math.floor(Date.now() / 1000) - job.acceptedAt) + 's ago. Awaiting worker completion.';
  if (active.has(job.state)) return identity + ' Runner active. OpenClaw is checking for a terminal receipt.';
  return identity + ' ' + terminal[job.state];
}

const noDecisions = 'No captain decisions are waiting for Jeff.';

function dailyProjectText(receipt) {
  if (!receipt) return '';
  const next = receipt.recommended;
  return 'Last saved project briefing · ' + receipt.date + ' CT' +
    (next ? ' · ' + next.repository + ' #' + next.issue + ' · ' + next.action : '') + '.';
}

function projectWork(job) {
  const { repository, issue, head } = job.request;
  const identity = repository + ' #' + issue + ' · ' + head.slice(0, 8) + '.';
  const status = {
    running: 'Hatchet accepted the coding job. Awaiting its outcome.',
    'patch-ready': 'The worker returned a candidate. Jeff can review the patch and authorize publication for exact-commit CI.',
    'ci-pending': 'Independent CI evidence is pending for the candidate.',
    'ci-failed': 'The last verification found a failing required check. Review CI before another attempt.',
    verified: 'The last verification matched all required CI checks.',
    refused: 'Hatchet refused the job. Check the coding worker.',
    'worker-failed': 'Hatchet reports that the worker failed or was cancelled. Check the worker before authorizing another attempt.',
    'invalid-artifact': 'The worker returned an invalid or inconsistent artifact. Inspect the isolated worktree.',
    'stale-base': 'The issue or approved base changed. Review the current scope.',
    'stale-candidate': 'The PR head or its base changed. Review the candidate against current GitHub evidence.',
  };
  return [identity, job.commit ? 'Candidate ' + job.commit.slice(0, 8) + '.' : '',
    status[job.state] ?? 'Coding submission is unresolved. Check the original Hatchet receipt before another attempt.'].filter(Boolean).join(' ');
}

function decisions(view, currentDelivery) {
  const needs = [];
  if (view.lease.holder === 'first-officer' && view.lease.state === 'held' &&
      (!view.armed || view.grant?.epoch !== view.lease.epoch || view.grant.paused)) {
    needs.push('Jeff needs to grant or resume the named scope for this epoch before dispatch.');
  }
  const unresolved = (view.unconfirmedProjectDelivery ?? []).filter((key) => key !== currentDelivery).length;
  if (unresolved) {
    const status = unresolved === 1 ? '1 Captain Telegram delivery is unconfirmed.' :
      unresolved + ' Captain Telegram deliveries are unconfirmed.';
    needs.push(status + ' Check the original conversation and reconcile the private receipts.');
  }
  if (view.projectTelegramLedgerFull) needs.push('The Captain Telegram receipt journal is full. Review it before continuing.');
  if (view.lease.state === 'handback-pending') needs.push('Wait for the shared fence to finish transferring authority.');
  if (view.active?.overtime) needs.push(jobText(view.active));
  for (const job of view.activeProjectJobs ?? []) {
    if (job.state !== 'running' && job.state !== 'ci-pending') needs.push(projectWork(job));
  }
  for (const job of view.recentProjectResults ?? []) {
    if (job.state !== 'verified') needs.push(projectWork(job));
  }
  if (view.blocked && view.blocked.reason !== 'ci-pending') needs.push('Last blocked review · Integration PR #' + view.blocked.pr + '. ' +
    blockers[view.blocked.reason]);
  for (const job of view.jobs) {
    if (job.delivery === 'delivery-unverified') {
      needs.push(jobText(job) + ' Delivery could not be confirmed. Check Telegram for the final review reply.');
    } else if (job.state in terminal && job.state !== 'verified') {
      needs.push(jobText(job));
    }
  }
  return needs.length ? needs.join('\n') : noDecisions;
}

export function createCaptain({ registry, get, state = localState, admit = fleetAdmission, now = Date.now,
  projectBrief = async () => null, dailyProjects = false }) {
  const runnerQuota = () => state('runner-quota').catch(() => null);
  async function status(kind, turn) {
    const source = registry[kind];
    return bounded(6500, (signal) => source.run({}, { ...turn, signal })).then((reply) => reply.text)
      .catch(() => source.failure);
  }

  async function task(pr, view, turn, pending) {
    const { lease, armed, grant } = view;
    if (!armed || lease.holder !== 'first-officer' || lease.state !== 'held' ||
        grant?.epoch !== lease.epoch || grant.paused) return 'Dispatch is paused until Jeff grants OpenClaw this scope at the current epoch.';
    if (view.active) return jobText(view.active);
    const [fleet, gate, quota, result] = await Promise.all([
      bounded(4500, (signal) => admit(signal, view.standalone, state)).catch(() => 'Fleet or gate evidence is unavailable.'),
      status('gate', turn),
      runnerQuota(),
      bounded(9500, () => integrationCI(pr, get)).catch(() => ({ ci: 'unavailable' })),
    ]);
    turn.signal.throwIfAborted();
    async function blocked(reason, text, head) {
      await state('block', { reason, pr, epoch: lease.epoch, head,
        owner: turn.owner, message: Number(turn.ctx.messageId) });
      return text;
    }
    if (pending && now() - pending.started * 1000 > 20 * 60_000) {
      return blocked('ci-timeout', blockers['ci-timeout']);
    }
    if (pending && result.head && result.head !== pending.head) {
      return blocked('ci-changed', blockers['ci-changed']);
    }
    if (result.ci === 'pending') {
      return blocked('ci-pending', blockers['ci-pending'], result.head);
    }
    if (fleet) return blocked('fleet', fleet + ' No lane was launched.');
    if (gate === registry.gate.failure || /HOLD is active|full gate is running|couldn’t verify/i.test(gate ?? '')) {
      return blocked('gate', gate + '\nNo lane was launched.');
    }
    if (quota?.allowed !== true) {
      return blocked('quota', 'The runner’s subscription quota scan did not verify sufficient included usage. No lane was launched.');
    }
    if (!['passing', 'failing'].includes(result.ci)) {
      return blocked('ci-' + result.ci, 'Required CI for the exact Integration PR head is ' + result.ci + '.');
    }
    const placed = await state('submit', {
      owner: turn.owner, message: Number(turn.ctx.messageId), epoch: lease.epoch, pr, head: result.head, ci: result.ci,
      resumePending: Boolean(pending),
    });
    if (placed.result === 'duplicate') return 'This review request already has a captain lane. Ask for “handoff” to see the receipt.';
    if (placed.result === 'active') return 'Another captain lane is active. Ask for “handoff” to see it.';
    if (placed.result === 'stale') return 'The epoch or approval changed before dispatch. No lane was launched.';
    if (placed.result === 'gate-unavailable') return 'The live gate could not be verified when submitting. No review was sent.';
    if (placed.result === 'capacity-unavailable') return 'Live local capacity could not be verified. No review was sent.';
    if (placed.result === 'capacity-blocked') return 'The Captain host is at its lane capacity. No review was sent.';
    if (placed.result === 'gate-blocked') return 'The gate is running or on hold. No review was sent.';
    if (placed.result === 'runner-unconfigured') return 'The standalone queue runner is unavailable. No review was submitted.';
    if (placed.result !== 'running') return placed.job?.queuePhase
      ? jobText(placed.job)
      : 'The runner did not confirm launch. OpenClaw will reconcile the local receipt.';
    return (placed.job.backend ? 'The queue accepted one exact-head CI review. ' :
      'I delegated one exact-head CI review to the configured runner. ') + jobText(placed.job);
  }

  async function run(request, turn) {
    const action = request.action;
    if (action === 'help') return 'Use “captain status”, “brief me”, “what needs me?”, “handoff”, or “captain this: verify CI for PR #33”.';
    if (action === 'advance-project') {
      turn.signal.throwIfAborted();
      const placed = await state('dispatch-project', { repository: request.repository, issue: request.issue });
      if (placed.result === 'duplicate') return 'This approved coding run already has a receipt. ' + projectWork(placed.job);
      if (placed.job) return projectWork(placed.job);
      const blocker = {
        'scope-denied': 'Jeff needs to approve the exact issue, base commit, and authority epoch.',
        stale: 'The Captain authority epoch changed. Check “captain status”.',
        'stale-github': 'The approved issue or base commit changed. Check GitHub before requesting a fresh grant.',
        'github-unavailable': 'Live GitHub evidence is unavailable. The run did not start.',
        active: 'This repository already has an active coding job. Ask for “handoff”.',
        'runner-unconfigured': 'The scoped coding worker or Hatchet policy is unavailable.',
        'admission-unavailable': 'The local gate or capacity could not be verified.',
        'admission-blocked': 'The local gate or capacity is blocking the run.',
        'existing-queue-receipt': 'Hatchet already has a receipt. Verify it before requesting another run.',
        'ledger-full': 'Jeff needs to review the full project ledger before dispatch.',
      };
      return blocker[placed.result] ?? 'The project admission result is unavailable. Check “handoff” before retrying.';
    }
    if (['grant', 'pause', 'handback'].includes(action)) {
      const outcome = await state(action, {
        owner: turn.owner, epoch: request.epoch, message: Number(turn.ctx.messageId),
      });
      if (outcome.result === 'stale') return 'The epoch, takeover state, or owner approval changed. Check “captain status”.';
      if (outcome.result === 'operator-transfer') return 'Use the operator handoff to transfer standalone Captain authority.';
      if (outcome.result === 'no-grant') return 'OpenClaw has no active scope grant to pause.';
      return {
        grant: 'Jeff approved Integration PR CI reviews for this epoch.',
        pause: 'Captain dispatch is paused. Existing work remains in the handoff.',
        handback: 'Handback requested. The shared fence will restore Grok Bot once it has drained.',
      }[outcome.result];
    }
    const viewPromise = state(action === 'handoff' ? 'handoff' : 'view');
    const projectEvidence = () => bounded(11_000, (signal) =>
      viewPromise.catch(() => null).then((view) =>
        projectBrief(AbortSignal.any([signal, turn.signal]), view?.activeProjectJobs ?? [])))
      .catch(() => ({ summary: 'Project evidence is unavailable.', decisions: ['Project decisions could not be verified.'] }));
    if (action === 'brief') {
      const [view, quota, projects, ...evidence] = await Promise.all([
        viewPromise.catch(() => null), runnerQuota(), projectEvidence(),
        ...['fleet', 'gate', 'github-open', 'quota'].map((kind) => status(kind, turn)),
      ]);
      return [view ? role(view) + '\n' + decisions(view).split('\n')[0] +
        (view.active ? '\n' + jobText(view.active) : '') :
        'Captain authority and decisions are unavailable.',
        ...(view?.activeProjectJobs ?? []).filter((job) => ['running', 'ci-pending'].includes(job.state)).map(projectWork),
        ...(projects ? [projects.summary] : []), dailyProjectText(view?.lastProjectDaily), ...evidence,
        quota?.modelFree ? 'Captain CI review uses a model-free worker.' :
          quota ? 'Captain runner quota · ' + quota.session.used + '% of 5-hour window · ' + quota.weekly.used + '% of weekly window.'
          : 'The captain runner’s subscription quota is unavailable.'].filter(Boolean).join('\n\n');
    }
    if (action === 'decisions') {
      const [view, projects] = await Promise.all([viewPromise, projectEvidence()]);
      const captainDecisions = decisions(view);
      if (!projects) return role(view) + '\n' + captainDecisions;
      return [role(view), ...projects.decisions,
        captainDecisions === noDecisions && projects.decisions.length ? null : captainDecisions].filter(Boolean).join('\n');
    }
    const view = await viewPromise;
    if (action === 'role') return role(view) + '\n' + decisions(view);
    const recommendation = view.lastProjectRecommendation;
    if (action === 'handoff') return [role(view), 'Work owner · OpenClaw. Decisions · Jeff.',
      'Handoff recorded at ' + new Date(view.recorded * 1000).toISOString() + '.', decisions(view, turn.delivery),
      recommendation ? 'Last recorded project recommendation · ' + recommendation.repo + ' #' +
        recommendation.issue + ' · ' + recommendation.action + ' · epoch ' + recommendation.epoch + '.' : '',
      view.blocked?.reason === 'ci-pending' ? 'Integration PR #' + view.blocked.pr + ' · ' +
        view.blocked.head.slice(0, 8) + ' · awaiting required CI.' : '',
      ...(view.activeProjectJobs ?? []).filter((job) => ['running', 'ci-pending'].includes(job.state)).map(projectWork),
      ...(view.recentProjectResults ?? []).filter((job) => job.state === 'verified').map(projectWork),
      dailyProjectText(view.lastProjectDaily),
      ...view.jobs.map(jobText)].filter(Boolean).join('\n');
    return task(request.pr, view, turn);
  }

  async function tick(deliver) {
    const view = await state('view');
    if (view.lease?.holder === 'first-officer' && view.lease.state === 'handback-pending') {
      await state('settle-handback');
    }
    const job = view.active;
    if (!job) {
      const pending = view.blocked;
      if (pending?.reason === 'ci-pending' && view.lease?.holder === 'first-officer' &&
          view.lease.state === 'held' && view.lease.epoch === pending.epoch && view.armed &&
          view.grant?.owner === pending.owner && !view.grant.paused &&
          view.grant.message < pending.message && now() - pending.observed * 1000 >= 30_000) {
        const update = await task(pending.pr, view, {
          owner: pending.owner, ctx: { messageId: pending.message }, signal: AbortSignal.timeout(15_000),
        }, pending);
        if (update !== blockers['ci-pending']) await deliver(update, pending.owner);
      }
      return;
    }
    const receipt = await state('poll', { lane: job.lane });
    const expired = now() - job.started * 1000 > 20 * 60_000;
    if (receipt.worker === 'running' || receipt.worker === 'unresolved') {
      if (expired && !job.overtime) {
        const notice = await state('overtime', { lane: job.lane });
        if (notice.job) await deliver(jobText(notice.job), notice.job.owner);
      }
      return;
    }
    if (receipt.worker === 'no-launch-receipt' && !job.launchRejected && now() - job.started * 1000 < 30_000) return;
    let result = 'worker-failed';
    if (expired) result = 'worker-timeout';
    else if (receipt.worker === 'settled' && receipt.head === job.head && receipt.ci === job.ci) {
      const current = await bounded(9500, () => integrationCI(job.pr, get)).catch(() => ({ ci: 'unavailable' }));
      if (current.ci === 'changed' || current.head && current.head !== job.head) result = 'head-changed';
      else if (current.ci === 'passing' && job.ci === 'passing') result = 'verified';
      else if (current.ci === 'failing') result = 'ci-failed';
      else result = 'ci-unavailable';
    }
    const done = await state('finish', { lane: job.lane, state: result });
    if (done.job) {
      await deliver(jobText(done.job), done.job.owner);
      await state('reported', { lane: job.lane });
    }
  }
  let lastDailyCheck = -Infinity;
  async function saveDailyProject(view) {
    const started = now();
    if (!dailyProjects || started - lastDailyCheck < 60_000) return;
    lastDailyCheck = started;
    const due = await state('project-daily-due');
    if (!due) return;
    const projects = await bounded(11_000, (signal) => projectBrief(signal, view.activeProjectJobs ?? []));
    if (projects?.snapshot) await state('save-project-daily', { ...due, snapshot: projects.snapshot });
  }

  let checkingProject = false;
  async function tickProject() {
    if (checkingProject) return;
    checkingProject = true;
    try {
      const view = await state('view');
      if (view.lease?.holder !== 'first-officer' || view.lease.state !== 'held') return;
      const jobs = (view.activeProjectJobs ?? []).filter((job) =>
        ['running', 'patch-ready', 'ci-pending'].includes(job.state));
      const ready = jobs.filter((job) => now() / 1000 - (job.observed ?? 0) >= 30)
        .sort((a, b) => (a.observed ?? 0) - (b.observed ?? 0));
      if (ready.length) {
        const { repository, issue, head, epoch } = ready[0].request;
        await state('inspect-project', { repository, issue, head, epoch });
      }
      await saveDailyProject(view);
    } finally {
      checkingProject = false;
    }
  }
  return { run, tick, tickProject };
}

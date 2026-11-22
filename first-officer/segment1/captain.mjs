import { execFile } from 'node:child_process';
import { loadavg } from 'node:os';
import { fileURLToPath } from 'node:url';
import { bounded } from './dispatch.mjs';
import { fleetSnapshotIsLocal, readFleetSnapshot } from './tools.mjs';

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

function localState(operation, payload = {}) {
  return new Promise((resolve, reject) => {
    const child = execFile('/usr/bin/nice', ['-n', '10', '/usr/bin/python3', '-I', '-B', stateScript, operation],
      { maxBuffer: 32 * 1024, ...(operation === 'submit' ? {} : { timeout: operation === 'runner-quota' ? 7000 : 2500 }) },
      (error, stdout) => {
        if (error) return reject(new Error('Captain state unavailable'));
        try { resolve(JSON.parse(stdout)); } catch { reject(new Error('Invalid captain state')); }
      });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(payload));
  });
}

export async function integrationCI(pr, read) {
  const path = repo + '/pulls/' + pr;
  const pull = await read(path);
  const identity = (row) => row?.number === pr && row.state === 'open' &&
    row.base?.repo?.full_name?.toLowerCase() === repo.slice(6).toLowerCase() &&
    sha.test(row.head?.sha ?? '');
  if (!identity(pull)) throw new Error('Open integration PR identity unavailable');
  const head = pull.head.sha;
  const checks = await read(repo + '/commits/' + head + '/check-runs?per_page=100&filter=latest');
  const final = await read(path);
  if (!identity(final) || final.head.sha !== head) return { head, ci: 'changed' };
  if (!Array.isArray(checks?.check_runs) || checks.total_count !== checks.check_runs.length) {
    return { head, ci: 'unavailable' };
  }
  const valid = required.map((name) =>
    checks.check_runs.filter((row) => row.app?.slug === 'github-actions' && row.name === name && row.head_sha === head));
  if (valid.some((rows) => rows.length !== 1)) return { head, ci: 'unavailable' };
  const statuses = valid.flat().map((row) => row.status === 'completed' ? row.conclusion : 'pending');
  if (statuses.includes('pending')) return { head, ci: 'pending' };
  return { head, ci: statuses.every((result) => result === 'success') ? 'passing' : 'failing' };
}

async function fleetAdmission() {
  const snapshot = await readFleetSnapshot();
  const clock = Date.now() - snapshot.ts * 1000;
  const gate = snapshot.host.locks?.find((row) => row.slot === 'fullgate');
  if (!fleetSnapshotIsLocal(snapshot) || clock < -30_000 || clock > 300_000 || typeof gate?.held !== 'boolean' ||
      !Array.isArray(snapshot.gates) || !Number.isSafeInteger(snapshot.host.heavy_jobs) ||
      !Number.isSafeInteger(snapshot.host.heavy_max)) return 'Fleet or gate evidence is stale or unavailable.';
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
  if (active.has(job.state)) return identity + ' Runner active. OpenClaw is checking for a terminal receipt.';
  return identity + ' ' + terminal[job.state];
}

function decisions(view) {
  const needs = [];
  if (view.lease.holder === 'first-officer' && view.lease.state === 'held' &&
      (!view.armed || view.grant?.epoch !== view.lease.epoch || view.grant.paused)) {
    needs.push('Jeff needs to grant or resume the named scope for this epoch before dispatch.');
  }
  if (view.lease.state === 'handback-pending') needs.push('Wait for the shared fence to finish transferring authority.');
  if (view.active?.overtime) needs.push(jobText(view.active));
  if (view.blocked && view.blocked.reason !== 'ci-pending') needs.push('Last blocked review · Integration PR #' + view.blocked.pr + '. ' +
    blockers[view.blocked.reason]);
  for (const job of view.jobs.filter((row) => row.delivery === 'delivery-unverified' ||
      row.state in terminal && row.state !== 'verified')) needs.push(jobText(job));
  return needs.length ? needs.join('\n') : 'No captain decisions are waiting for Jeff.';
}

export function createCaptain({ registry, get, state = localState, admit = fleetAdmission, now = Date.now }) {
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
      bounded(4500, admit).catch(() => 'Fleet or gate evidence is unavailable.'),
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
    if (gate === registry.gate.failure || /HOLD is active|couldn’t verify/i.test(gate ?? '')) {
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
    if (placed.result === 'runner-unconfigured') return 'The standalone queue runner is unavailable. No review was submitted.';
    if (placed.result !== 'running') return 'The runner did not confirm launch. OpenClaw will reconcile the local receipt.';
    return 'I delegated one exact-head CI review to the configured runner. ' + jobText(placed.job);
  }

  async function run(request, turn) {
    const action = request.action;
    if (action === 'help') return 'Use “captain status”, “brief me”, “what needs me?”, “handoff”, or “captain this: verify CI for PR #33”.';
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
    if (action === 'brief') {
      const [view, quota, ...evidence] = await Promise.all([
        viewPromise.catch(() => null), runnerQuota(),
        ...['fleet', 'gate', 'github-open', 'quota'].map((kind) => status(kind, turn)),
      ]);
      return [view ? role(view) + '\n' + decisions(view).split('\n')[0] +
        (view.active ? '\n' + jobText(view.active) : '') :
        'Captain authority and decisions are unavailable.', ...evidence,
        quota ? 'Captain runner quota · ' + quota.session.used + '% of 5-hour window · ' + quota.weekly.used + '% of weekly window.'
          : 'The captain runner’s subscription quota is unavailable.'].join('\n\n');
    }
    const view = await viewPromise;
    if (action === 'role' || action === 'decisions') return role(view) + '\n' + decisions(view);
    if (action === 'handoff') return [role(view), 'Work owner · OpenClaw. Decisions · Jeff.',
      'Handoff recorded at ' + new Date(view.recorded * 1000).toISOString() + '.', decisions(view),
      view.blocked?.reason === 'ci-pending' ? 'Integration PR #' + view.blocked.pr + ' · ' +
        view.blocked.head.slice(0, 8) + ' · awaiting required CI.' : '',
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
    if (receipt.worker === 'running') {
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
  return { run, tick };
}

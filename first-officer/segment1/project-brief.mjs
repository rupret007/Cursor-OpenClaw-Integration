
import { bounded } from './dispatch.mjs';

const repository = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}$/;
const number = (value) => Number.isSafeInteger(value) && value > 0 && value < 100_000_000;
const label = (value) => typeof value === 'string' && /^[\p{L}\p{N}][\p{L}\p{N} .,'()/&_-]{0,119}$/u.test(value);
const rank = (value) => Number.isInteger(value) && value >= 0 && value <= 3;
const nextActions = {
  investigate: 'Inspect acceptance checks and propose a bounded change',
  fix: 'Inspect the failing checks and propose a bounded fix',
  verify: 'Verify the milestone’s acceptance checks against the current commit',
  wait: 'Wait for terminal CI on the current commit',
  refresh: 'Refresh the GitHub issue, PR, or CI evidence',
  blocked: 'Resolve the open dependency',
  closed: 'Confirm the milestone outcome and set the next one',
  working: 'Continue the existing coding run through verification',
};

function validProject(project) {
  return project && repository.test(project.repo ?? '') && number(project.issue) &&
    [project.goal, project.milestone, project.owner, project.acceptance].every(label) &&
    ['impact', 'urgency', 'risk'].every((key) => rank(project[key])) &&
    (project.pr === undefined || number(project.pr) && Array.isArray(project.checks) &&
      project.checks.length > 0 && project.checks.length <= 6 &&
      project.checks.every(label) && new Set(project.checks).size === project.checks.length) &&
    Array.isArray(project.dependencies) && project.dependencies.length <= 3 &&
    project.dependencies.every((id) => number(id) && id !== project.issue) &&
    new Set(project.dependencies).size === project.dependencies.length &&
    project.scope === 'read-only';
}

async function issueState(read, repo, id, signal) {
  const row = await read(repo, 'issues/' + id, signal);
  if (row?.number !== id || row.pull_request ||
      row.repository_url?.toLowerCase() !== 'https://api.github.com/repos/' + repo.toLowerCase() ||
      !['open', 'closed'].includes(row.state)) throw new Error('Issue identity unavailable');
  return row.state;
}

async function inspect(project, read, check, signal) {
  const { repo, issue, pr, dependencies } = project;
  const [state, ...blockers] = await Promise.all([
    issueState(read, repo, issue, signal),
    ...dependencies.map((id) => issueState(read, repo, id, signal)),
  ]);
  if (state === 'closed') return { project, action: 'closed' };
  const open = dependencies.filter((_, index) => blockers[index] === 'open');
  if (open.length) return { project, action: 'blocked', open };
  if (!pr) return { project, action: 'investigate' };
  const result = await check(project, (route) => read(repo, route, signal));
  const action = { passing: 'verify', failing: 'fix', pending: 'wait' }[result.ci] ?? 'refresh';
  return { project, action, ...result };
}

const actionable = new Set(['investigate', 'fix', 'verify']);
const needsJeff = new Set(['closed', 'investigate', 'fix']);

export function createProjectBrief({ config, read, check, record }) {
  return async function projectBrief(signal, activeWork = []) {
    const projects = await config();
    if (projects === null) return null;
    if (!Array.isArray(projects) || projects.length < 1 || projects.length > 2 ||
        !projects.every(validProject) ||
        new Set(projects.map(({ repo }) => repo.toLowerCase())).size !== projects.length) {
      throw new Error('Captain project profile is invalid');
    }
    const rows = await Promise.all(projects.map((project) =>
      bounded(8000, (deadline) =>
        inspect(project, read, check, AbortSignal.any([signal, deadline])))
        .catch(() => ({ project, action: 'refresh' }))));
    const activeRepositories = new Set(activeWork.map((job) => job.request.repository));
    for (const row of rows) {
      if (actionable.has(row.action) && activeRepositories.has(row.project.repo.toLowerCase())) row.action = 'working';
    }
    signal.throwIfAborted();
    const compare = (a, b) => b.project.impact - a.project.impact ||
      b.project.urgency - a.project.urgency || a.project.risk - b.project.risk ||
      a.project.repo.toLowerCase().localeCompare(b.project.repo.toLowerCase());
    const best = rows.filter(({ action }) => actionable.has(action)).sort(compare)[0];
    const identify = ({ project }) => project.repo + ' #' + project.issue;
    const decisions = rows.filter(({ action }) => needsJeff.has(action)).sort(compare).map((row) =>
      identify(row) + (row.action === 'closed'
        ? ' · Confirm the outcome and choose the next milestone.'
        : ' · Approve a bounded assignment if you want Bob to proceed.'));
    let journalUnavailable = false;
    if (best && record) {
      const { project, action, head } = best;
      try {
        await record({ repo: project.repo, issue: project.issue, action, ...(head ? { head } : {}) });
      } catch {
        journalUnavailable = true;
      }
    }
    signal.throwIfAborted();
    const evidence = rows.map(({ project, action, head, ci }) => ({
      repository: project.repo.toLowerCase(), issue: project.issue, action, ...(head ? { head, ci } : {}),
    }));
    return {
      snapshot: { projects: evidence, recommended: best ? evidence[rows.indexOf(best)] : null },
      summary: [
        'Projects · live GitHub read · read-only scope.',
        ...rows.map((row) => {
          const { project, action, open, head, ci } = row;
          return [
            identify(row) + ' · ' + project.milestone + '.',
            'Goal · ' + project.goal + ' · Owner · ' + project.owner + '.',
            'Acceptance · ' + project.acceptance + '.',
            open ? 'Open dependencies · ' + open.map((id) => '#' + id).join(', ') + '.' : '',
            head ? 'PR #' + project.pr + ' · ' + head.slice(0, 8) + ' · CI ' + ci + '.' : '',
            nextActions[action] + '.',
          ].filter(Boolean).join('\n');
        }),
        best ? 'Recommended next · ' + identify(best) + ' · ' + nextActions[best.action] + '.' :
          'No actionable project work could be verified.',
        ...(journalUnavailable ? ['The recommendation could not be recorded in the Captain ledger.'] : []),
      ].join('\n\n'),
      decisions: rows.some(({ action }) => action === 'refresh')
        ? [...decisions, 'Project decisions could not be fully verified.'] : decisions,
    };
  };
}

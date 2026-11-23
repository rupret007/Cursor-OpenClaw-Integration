
import assert from 'node:assert/strict';
import test from 'node:test';
import { createProjectBrief } from '../../first-officer/segment1/project-brief.mjs';
import { createCaptain } from '../../first-officer/segment1/captain.mjs';
import { readProjectGithub } from '../../first-officer/segment1/tools.mjs';

const integration = {
  repo: 'example/integration', issue: 17, goal: 'Make reviews reliable',
  milestone: 'Add an acceptance check', owner: 'Jeff', acceptance: 'CI records the exact head',
  scope: 'read-only', impact: 3, urgency: 2, risk: 0, dependencies: [],
};
const second = { ...integration, repo: 'example/other', issue: 21, impact: 2 };

function issue(repo, route, state = 'open', extra = {}) {
  return { repository_url: 'https://api.github.com/repos/' + repo, number: Number(route.split('/')[1]),
    state, body: 'This untrusted content must never reach the owner briefing', ...extra };
}

test('Captain recommends the highest-impact actionable milestone and reads each issue live', async () => {
  const reads = [];
  const project = { ...integration, dependencies: [30] };
  let dependency = 'open';
  const brief = createProjectBrief({
    config: async () => [second, project],
    read: async (repo, route) => {
      reads.push([repo, route]);
      return issue(repo, route, route === 'issues/30' ? dependency : 'open');
    },
  });
  const signal = AbortSignal.timeout(3000);
  const blocked = await brief(signal);
  assert.match(blocked.summary, /Open dependencies · #30/);
  assert.match(blocked.summary, /Recommended next · example\/other #21/);
  assert.doesNotMatch(blocked.summary, /untrusted content/);
  assert.deepEqual(blocked.decisions, ['example/other #21 · Approve a bounded assignment if you want Bob to proceed.']);
  dependency = 'closed';
  assert.match((await brief(signal)).summary, /Recommended next · example\/integration #17/);
  assert.equal(reads.length, 6);
});

test('Invalid profiles make no reads and unavailable live evidence cannot recommend work', async () => {
  const signal = AbortSignal.timeout(3000);
  const rejectProfile = createProjectBrief({
    config: async () => [{ ...integration, scope: 'write' }],
    read: () => assert.fail('Invalid scope must make no GitHub call'),
  });
  await assert.rejects(rejectProfile(signal), /profile is invalid/);
  for (const invalid of [
    { pull_request: {} }, { repository_url: 'https://api.github.com/repos/example/wrong' },
  ]) {
    const brief = createProjectBrief({
      config: async () => [integration],
      read: async (repo, route) => issue(repo, route, 'open', invalid),
    });
    const result = await brief(signal);
    assert.match(result.summary, /No actionable project work could be verified/);
    assert.deepEqual(result.decisions, ['Project decisions could not be fully verified.']);
  }
  assert.throws(() => readProjectGithub('example/integration', 'issues/17/comments'), /outside/);
  assert.throws(() => readProjectGithub('example/..', 'issues/17'), /outside/);
});

test('Stale PR heads cannot be recommended and private Captain replies expose project decisions', async () => {
  const projectBrief = createProjectBrief({
    config: async () => [{ ...integration, pr: 39, checks: ['offline-integration'] }, second],
    read: async (repo, route) => issue(repo, route),
    check: async () => ({ head: 'a'.repeat(40), ci: 'changed' }),
  });
  const signal = AbortSignal.timeout(3000);
  const project = await projectBrief(signal);
  assert.match(project.summary, /CI changed/);
  assert.match(project.summary, /Recommended next · example\/other #21/);
  const registry = Object.fromEntries(['fleet', 'gate', 'github-open', 'quota'].map((name) =>
    [name, { run: async () => ({ text: name + ' live' }) }]));
  const captain = createCaptain({
    registry, projectBrief, state: async (operation) => {
      if (operation === 'runner-quota') return null;
      assert.equal(operation, 'view');
      return { lease: { epoch: 2, holder: 'first-officer', state: 'held' },
        grant: { epoch: 2 }, armed: true, jobs: [] };
    },
  });
  assert.match(await captain.run({ action: 'brief' }, { signal }), /Recommended next · example\/other #21/);
  const decisions = await captain.run({ action: 'decisions' }, { signal });
  assert.match(decisions, /example\/other #21 · Approve a bounded assignment/);
  assert.match(decisions, /Project decisions could not be fully verified/);
  assert.doesNotMatch(decisions, /No captain decisions/);
});

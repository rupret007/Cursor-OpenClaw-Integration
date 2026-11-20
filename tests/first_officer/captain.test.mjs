import assert from 'node:assert/strict';
import test from 'node:test';
import { createCaptain, integrationCI } from '../../first-officer/segment1/captain.mjs';
import { classifyRequest } from '../../first-officer/segment1/dispatch.mjs';
import { diagnoseMacAndrea, diagnoseMacAndreaSync } from '../../first-officer/segment1/andrea-mac.mjs';
import { summarizeAndrea } from '../../first-officer/segment1/tools.mjs';

const head = 'a'.repeat(40);
const other = 'b'.repeat(40);
const readFixture = ({ latest = head, tested = head, conclusion = 'success' } = {}) => {
  let reads = 0;
  return async (path) => {
    if (path.includes('/pulls/')) {
      return { number: 33, state: 'open', head: { sha: ++reads === 1 ? head : latest },
        base: { repo: { full_name: 'rupret007/Cursor-OpenClaw-Integration' } } };
    }
    assert.match(path, new RegExp('/commits/' + head + '/check-runs'));
    const rows = ['first-officer-segment1', 'offline-integration'].map((name) => ({
      name, head_sha: tested, status: 'completed', conclusion, app: { slug: 'github-actions' },
    }));
    return { total_count: rows.length, check_runs: rows };
  };
};

test('captain routing claims exact phrases before legacy control and never sends an arbitrary task to the runner', () => {
  const hooks = { isControl: () => true };
  assert.deepEqual(classifyRequest('What needs me?', hooks), { kind: 'captain', action: 'decisions' });
  assert.deepEqual(classifyRequest('Handoff', hooks), { kind: 'captain', action: 'handoff' });
  assert.deepEqual(classifyRequest('captain this: verify CI for PR #33', hooks), { kind: 'captain', action: 'task', pr: 33 });
  assert.deepEqual(classifyRequest('captain this: deploy', hooks), { kind: 'captain', action: 'help' });
});

test('captain CI proof rejects an old tested head and a PR that changes mid-read', async () => {
  assert.deepEqual(await integrationCI(33, readFixture()), { head, ci: 'passing' });
  assert.deepEqual(await integrationCI(33, readFixture({ latest: other })), { head, ci: 'changed' });
  assert.deepEqual(await integrationCI(33, readFixture({ tested: other })), { head, ci: 'unavailable' });
  assert.deepEqual(await integrationCI(33, readFixture({ conclusion: 'failure' })), { head, ci: 'failing' });
});

test('captain brief uses live tools and a paused or stale grant cannot reach submission', async () => {
  const view = { lease: { holder: 'first-officer', epoch: 3, state: 'held' }, armed: true,
    grant: { epoch: 2, profile: 1 }, jobs: [] };
  const invoked = new Set();
  const registry = Object.fromEntries(['fleet', 'gate', 'github-open', 'quota'].map((name) =>
    [name, { failure: name + ' unavailable', async run() {
      invoked.add(name);
      if (name === 'gate') throw new Error('offline fixture');
      return { text: name + ' live' };
    } }]));
  const captain = createCaptain({ registry, get: () => assert.fail('GitHub planning is unavailable without a grant'),
    state: async (op) => {
      if (op === 'runner-quota') return null;
      assert.equal(op, 'view');
      return view;
    } });
  const turn = { owner: '123456', ctx: { messageId: '553' }, signal: AbortSignal.timeout(3000) };
  const brief = await captain.run({ action: 'brief' }, turn);
  assert.match(brief, /Captain · OpenClaw · epoch 3/);
  assert.match(brief, /gate unavailable/);
  assert.equal(invoked.size, 4);
  assert.match(await captain.run({ action: 'task', pr: 33 }, turn), /until Jeff grants/);
});

test('captain monitor reports lane failure and verifies current CI before reporting success', async () => {
  const job = { lane: 'fo-ci-fixture', pr: 33, head, ci: 'passing', state: 'running', started: 100, owner: '123456' };
  let worker = { worker: 'missing-result' };
  const calls = [];
  const messages = [];
  const state = async (op, req) => {
    calls.push(op);
    if (op === 'view') return { active: job };
    if (op === 'poll') return worker;
    if (op === 'finish') return { job: { ...job, state: req.state } };
    assert.equal(op, 'reported');
  };
  const deliver = async (reply, owner) => {
    assert.equal(owner, job.owner);
    messages.push(reply);
  };
  await createCaptain({ state, get: () => assert.fail('Failed lane has no result to verify'), now: () => 110_000 }).tick(deliver);
  assert.match(messages.pop(), /The lane failed/);
  worker = { worker: 'settled', head, ci: 'passing' };
  await createCaptain({ state, get: readFixture(), now: () => 110_000 }).tick(deliver);
  assert.match(messages.pop(), /Required CI passed for that exact head/);
  await createCaptain({ state, get: readFixture({ latest: other }), now: () => 110_000 }).tick(deliver);
  assert.match(messages.pop(), /PR head changed/);
  assert.deepEqual(calls.slice(-4), ['view', 'poll', 'finish', 'reported']);
});

test('Andrea checks bounded previews when the chat metadata is stale and emits metadata-only diagnostics', async () => {
  const now = Date.now();
  const fallback = {
    chats: async () => ({ complete: true, chats: [{ chatJid: 'bb:fixture', lastMessageAt: '1999-01-01T01:00:00Z' }] }),
    messages: async () => [{ timestamp: new Date(now).toISOString(), preview: 'Appointment at the demo bakery', isFromMe: false }],
  };
  assert.match(await summarizeAndrea(fallback, now), /1 incoming, 0 outgoing/);
  const settings = { BLUEBUBBLES_CONTROL_TOKEN: 'x'.repeat(24), BLUEBUBBLES_CANONICAL_SELF_THREAD_JID: 'bb:owner' };
  const counts = await diagnoseMacAndrea(() => settings, async (url) => {
    if (url.pathname.endsWith('/chats')) return Response.json(await fallback.chats().then(({ chats }) => ({
      chats: [{ chatJid: 'bb:owner', isGroup: false }, { ...chats[0], isGroup: false }],
    })));
    const messages = await fallback.messages();
    return Response.json({ messages: [{ ...messages[0], chatJid: 'bb:fixture', isBotMessage: false }] });
  }, now);
  assert.equal(counts.listedChats, 2);
  assert.equal(counts.excludedChats, 1);
  assert.equal(counts.freshChatMetadata, 0);
  assert.equal(counts.recentTextPreviews, 1);
  assert.ok(Object.values(counts).every(Number.isInteger));
});

test('Andrea diagnostics classify settings and API failures without returning response data', async () => {
  const settings = { BLUEBUBBLES_CONTROL_TOKEN: 'x'.repeat(24), BLUEBUBBLES_CANONICAL_SELF_THREAD_JID: 'bb:owner' };
  await assert.rejects(diagnoseMacAndrea(() => { throw new Error('private fixture path'); }),
    { code: 'settings-unavailable', message: 'settings-unavailable' });
  await assert.rejects(diagnoseMacAndrea(() => ({})),
    { code: 'settings-invalid', message: 'settings-invalid' });
  await assert.rejects(diagnoseMacAndrea(() => settings, async () => new Response('private fixture data', { status: 403 })),
    { code: 'api-authentication', message: 'api-authentication' });
  await assert.rejects(diagnoseMacAndrea(() => settings, async () => { throw new Error('private fixture address'); }),
    { code: 'api-unreachable', message: 'api-unreachable' });
});

test('Andrea upstream diagnostic returns only allowed status labels and age categories', async () => {
  const now = Date.now();
  const settings = { BLUEBUBBLES_CONTROL_TOKEN: 'x'.repeat(24), BLUEBUBBLES_CANONICAL_SELF_THREAD_JID: 'bb:owner' };
  const result = await diagnoseMacAndreaSync(() => settings, async (url, request) => {
    assert.equal(url.pathname, '/v1/bluebubbles/status');
    assert.equal(request.method, 'GET');
    return Response.json({ status: {
      connected: true, chatScope: 'allowlist', transportState: 'reachable',
      webhookRegistrationState: 'private fixture detail',
      shadowPollLastOkAt: new Date(now - 1000).toISOString(),
      lastInboundAt: new Date(now - 3 * 24 * 60 * 60_000).toISOString(),
      lastInboundChatJid: 'private fixture detail',
    } });
  }, now);
  assert.deepEqual(result, { channel: 'connected', scope: 'allowlist', transport: 'reachable',
    webhook: 'unknown', shadowPoll: 'recent', lastInbound: 'older' });
});

import assert from 'node:assert/strict';
import { setTimeout as delay } from 'node:timers/promises';
import { test } from 'node:test';
import { budgets, classifyRequest, createDispatch } from '../../first-officer/segment1/dispatch.mjs';
import { answerSimple, fastModelRef, routeOwnerModel, standaloneSimple } from '../../first-officer/segment1/model-router.mjs';
import { registerSegmentOne } from '../../first-officer/segment1/register.mjs';
import { fleetStatus, openPullRequests, summarizeAndrea } from '../../first-officer/segment1/tools.mjs';

const fixtures = [
  ["what's running / load on each machine", 'fleet'],
  ['gate status', 'gate'],
  ['quota status', 'quota'],
  ['my open PRs and CI', 'github-open'],
  ['Remember that my test marker is an orange triangle', 'remember'],
  ['What did I tell you about test marker?', 'recall'],
  ['summarize my texts', 'texts'],
  ['Summarize my recent text messages.', 'texts'],
];

const short = (prompt) => ({ model: fastModelRef.split('/')[1], reason: 'short-chat', bytes: prompt.length, fallbacks: [] });
const intentHooks = {
  legacyStatus: () => undefined,
  legacyRequest: () => undefined,
  isControl: () => false,
  isSimple: (text) => standaloneSimple(text, short, {}),
};

test('natural requests identify the live route and map memory to a stable topic', () => {
  for (const [text, kind] of fixtures) assert.equal(classifyRequest(text, intentHooks)?.kind, kind);
  assert.equal(classifyRequest(fixtures[4][0], intentHooks).args, 'test-marker: an orange triangle');
  assert.equal(classifyRequest(fixtures[5][0], intentHooks).args, 'test-marker');
  assert.equal(classifyRequest('remember', intentHooks).args, undefined);
  assert.equal(classifyRequest('remember sample-topic: blue kite', intentHooks).args, 'sample-topic: blue kite');
  assert.equal(classifyRequest('local CI status', intentHooks), undefined);
  assert.equal(classifyRequest('show latest GitHub checks', intentHooks).kind, 'github-help');
  assert.equal(classifyRequest('what is a pull request?', intentHooks).kind, 'simple');
  assert.equal(classifyRequest('what is the gate status', intentHooks).kind, 'gate');
});

test('dispatch starts progress and the tool concurrently and stays inside its work budgets', async () => {
  assert.ok(budgets.acknowledgment < 2000);
  assert.ok(budgets.simple <= 11_000, 'Reserve time for Telegram to deliver a reply before 15 seconds');
  assert.ok(budgets.status <= 18_000, 'Reserve time for Telegram to deliver a reply before 30 seconds');
  const calls = [];
  let finishAck;
  const ack = new Promise((resolve) => { finishAck = resolve; });
  const result = await createDispatch({
    authorize: () => 'fixture', classify: () => ({ kind: 'fleet' }),
    acknowledge: () => { calls.push('ack-start'); return ack; },
    registry: { fleet: { run() { calls.push('live-read'); finishAck(); return { text: 'Observed.' }; } } },
    warn: () => assert.fail('Dispatch should report no failures'),
    timing: { acknowledgment: 500, simple: 500, status: 500 },
  })({ content: 'fleet status' }, {});
  assert.deepEqual(calls, ['ack-start', 'live-read']);
  assert.deepEqual(result, { handled: true, text: 'Observed.' });
});

test('timeouts cancel hung calls and keep the turn claimed with an explicit failure', async () => {
  const warnings = [];
  const canceled = [];
  const stalled = (name, signal) => new Promise((_, reject) => {
    signal.addEventListener('abort', () => { canceled.push(name); reject(signal.reason); }, { once: true });
  });
  const dispatch = createDispatch({
    authorize: () => 'fixture', classify: () => ({ kind: 'github-open' }),
    acknowledge: (_, __, signal) => stalled('ack', signal),
    registry: {
      'github-open': { run: (_, { signal }) => stalled('tool', signal), failure: 'GitHub check failed.' },
    },
    warn: (line) => warnings.push(line),
    timing: { acknowledgment: 15, simple: 25, status: 40 },
  });
  const result = await dispatch({ content: 'my open PRs and CI' }, {});
  assert.deepEqual(canceled, ['ack', 'tool']);
  assert.equal(result.handled, true);
  assert.equal(result.text, 'GitHub check failed.');
  assert.equal(warnings.length, 2);
});

test('all Segment 1 handlers are registered on the private inbound hook', async () => {
  const owner = '7';
  const config = { channels: { telegram: {} } };
  const events = [];
  let dispatch;
  const api = {
    config,
    on(hook, fn, options) {
      assert.equal(hook, 'before_dispatch');
      assert.equal(options.priority, 240);
      assert.ok(options.timeoutMs > budgets.status);
      dispatch = fn;
    },
    logger: { warn: (line) => events.push(line) },
    runtime: { channel: {
      telegram: { sendTypingTelegram(to) { assert.equal(to, owner); events.push('typing'); } },
      outbound: {
        async loadAdapter(channel) {
          assert.equal(channel, 'telegram');
          return {
            async sendText({ to, text, signal }) {
              assert.equal(to, owner);
              assert.equal(text, 'On it.');
              assert.equal(signal.aborted, false);
              events.push('ack');
              return { target: { id: to }, messageId: '1' };
            },
          };
        },
      },
    } },
  };
  const services = {
    statusTopic: intentHooks.legacyStatus, parseRequest: (text) => text === 'pr 1 ci'
      ? { kind: 'github', repo: 'fixture', number: 1, checks: true } : undefined,
    controlIntent: intentHooks.isControl, routeFor: short, redacted: (text) => text,
    incomingOwner: (_, event, ctx) => event.isGroup === false && ctx.senderId === owner,
    telegramOwner: () => owner,
    gateStatus: async () => { throw new Error('synthetic transport detail'); },
    quotaStatus: async () => 'Quota checked',
    githubReply: async () => 'PR checked',
    readPrivateConfig: async () => undefined,
    createAndreaReader: () => ({
      chats: async () => ({ complete: true, chats: [] }),
    }),
  };
  const commands = new Map([
    ['remember', { handler: async (ctx) => ({ text: 'Memory check received', args: ctx.args }) }],
    ['recall', { handler: async () => ({ text: 'Saved fact verified' }) }],
  ]);
  registerSegmentOne(api, commands, services);
  const dispatchOwner = (content) => dispatch({ content, isGroup: false }, { senderId: owner });
  assert.equal(await dispatch({ content: 'gate status', isGroup: true }, { senderId: owner }), undefined);
  assert.equal(await dispatch({ content: 'summarize my texts', isGroup: false }, { senderId: '8' }), undefined);
  assert.equal(await dispatch({ content: 'summarize my texts', isGroup: false }, { senderId: owner, accountId: 'other' }), undefined);
  assert.equal(events.length, 0);

  assert.match((await dispatchOwner('gate status')).text, /live gate status check failed/);
  assert.match((await dispatchOwner('quota status')).text, /Quota checked/);
  assert.match((await dispatchOwner('pr 1 ci')).text, /PR checked/);
  assert.match((await dispatchOwner('remember marker: synthetic')).text, /Memory check received/);
  assert.match((await dispatchOwner('what did I tell you about marker?')).text, /Saved fact verified/);
  assert.match((await dispatchOwner('summarize my texts')).text, /Andrea.*synced/);
  assert.match((await dispatchOwner('define an algorithm')).text, /fast model.*failed/i);
  assert.equal(events.filter((event) => event === 'ack').length, 7);
  assert.equal(events.filter((event) => event === 'typing').length, 7);
  assert.equal(events.some((event) => event.includes('synthetic transport detail')), false);
});

test('short answers use the configured flash model with bounded context and skip the slow fallback probe', async () => {
  const classify = (prompt) => short(prompt);
  const forbidden = () => assert.fail('No quota probe on the simple route');
  const selection = await routeOwnerModel({ prompt: 'hello' }, {}, classify, forbidden);
  assert.equal(selection.model, fastModelRef.split('/')[1]);
  assert.deepEqual(selection.fallbacks, []);
  const signal = new AbortController().signal;
  const runtime = { llm: {
    async complete(params) {
      assert.equal(params.model, fastModelRef);
      assert.equal(params.signal, signal);
      assert.equal(params.messages.length, 1);
      assert.equal(params.maxTokens, 384);
      assert.match(params.systemPrompt, /no tools or current operational evidence/i);
      return { stopReason: 'stop', text: 'A fixture answer.' };
    },
  } };
  assert.equal(await answerSimple(runtime, 'define a branch', signal), 'A fixture answer.');
  runtime.llm.complete = async () => ({ stopReason: 'error', text: 'A partial reply' });
  await assert.rejects(answerSimple(runtime, 'define a branch', signal), /did not complete/);
  assert.equal(standaloneSimple('explain a branch', classify, { replyToId: '1' }), false);
  const strong = (prompt) => ({ ...short(prompt), reason: 'deliberate-task', model: 'minimax-m3', fallbacks: [] });
  assert.equal(standaloneSimple('explain complex evidence', strong, {}), false);
});

test('fleet refuses old remote readings and keeps independent local evidence', async () => {
  const now = Date.parse('2026-11-20T04:00:00Z');
  const fresh = { timestamp: new Date(now).toISOString(), load1: 2, load5: 3, load15: 4, codex_jobs: 1, test_jobs: 0 };
  const stale = { ...fresh, timestamp: new Date(now - 600_000).toISOString(), load1: 99 };
  const read = async () => ({
    ts: now / 1000, host: { heavy_jobs: 2 }, lane_counts: { running: 1 },
    nodes: { 'mac-mini': fresh, nucbox: stale },
  });
  const lines = await fleetStatus({}, { read, load: () => [1, 1, 1], now: () => now });
  assert.match(lines, /Bobserver · 1\.00 \/ 1\.00 \/ 1\.00.*2 heavy jobs/);
  assert.match(lines, /Mac Mini · 2\.00 \/ 3\.00 \/ 4\.00.*1 Codex jobs/);
  assert.match(lines, /NucBox · current load and tracked jobs unavailable/);
  assert.match(lines, /MacBook Pro · current load and tracked jobs unavailable/);
  assert.doesNotMatch(lines, /99\.00/);
  assert.match(await fleetStatus({ node: 'mac-mini' }, {
    read: async () => { throw new Error('unavailable'); }, now: () => now,
  }), /Mac Mini · current load and tracked jobs unavailable/);
});

test('GitHub uses one live query and binds CI to the exact visible head', async () => {
  const signal = new AbortController().signal;
  const head = 'a'.repeat(40);
  const oid = 'b'.repeat(40);
  let calls = 0;
  const response = { data: { viewer: { login: 'fixture', pullRequests: {
    totalCount: 2, pageInfo: { hasNextPage: false },
    nodes: [head, oid].map((headRefOid, i) => ({
      number: i + 1, headRefOid, isDraft: i === 1, repository: { nameWithOwner: 'fixture/sample' },
      commits: { nodes: [{ commit: { oid: head, statusCheckRollup: { state: 'SUCCESS' } } }] },
    })),
  } } } };
  const query = async (document, cancellation) => {
    calls += 1;
    assert.equal(cancellation, signal);
    assert.match(document, /query.*\n[\s\S]*?pullRequests.*states: OPEN/);
    assert.doesNotMatch(document, /mutation/);
    return response;
  };
  const text = await openPullRequests(signal, { query, expectedLogin: 'fixture' });
  assert.equal(calls, 1);
  assert.match(text, /#1 · aaaaaaaa · passing/);
  assert.match(text, /#2 \(draft\) · bbbbbbbb · CI unavailable for this head/);
  await assert.rejects(openPullRequests(signal, { query, expectedLogin: 'someone-else' }), /could not be verified/);
});

test('Andrea reads a bounded batch in parallel and returns themes without preview text or contacts', async () => {
  const now = Date.parse('2026-11-20T04:00:00Z');
  const timestamp = new Date(now - 60000).toISOString();
  let active = 0;
  let peak = 0;
  const syntheticPreview = 'Meet tomorrow with the demonstration character';
  const reader = {
    chats: async () => ({ complete: false, chats: ['a', 'b', 'c', 'd', 'e'].map((chatJid) => ({
      lastMessageAt: timestamp, chatJid, name: 'sample-contact',
    })) }),
    async messages(chat) {
      active += 1;
      peak = Math.max(peak, active);
      await delay(10);
      active -= 1;
      if (chat === 'a') throw new Error('synthetic transport failure');
      return [{ timestamp, preview: syntheticPreview, isFromMe: false }];
    },
  };
  const report = await summarizeAndrea(reader, now);
  assert.equal(peak, 4);
  assert.match(report, /3 incoming, 0 outgoing across 3 recent conversations/);
  assert.match(report, /Topics detected in previews · scheduling/);
  assert.match(report, /could not read 1/);
  assert.match(report, /partial chat list/);
  assert.equal(report.includes(syntheticPreview), false);
  assert.equal(report.includes('sample-contact'), false);
  await assert.rejects(summarizeAndrea({ ...reader, messages: async () => { throw Error('timeout'); } }, now),
    /previews unavailable/);
});

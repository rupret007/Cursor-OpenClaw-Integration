import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { test } from 'node:test';
import { budgets, classifyRequest, createDispatch } from '../../first-officer/segment1/dispatch.mjs';
import { answerSimple, createQuickChat, fastModelRef, routeOwnerModel, standaloneSimple } from '../../first-officer/segment1/model-router.mjs';
import { registerSegmentOne } from '../../first-officer/segment1/register.mjs';
import { fleetStatus, openPullRequests, readFleetSnapshot, standaloneGateStatus, standaloneQuotaStatus, summarizeAndrea } from '../../first-officer/segment1/tools.mjs';
import { andreaCommand, parseAndreaSummary } from '../../first-officer/segment1/andrea-ssh.mjs';
import { readMacAndrea, runMacAndrea } from '../../first-officer/segment1/andrea-mac.mjs';

const fixtures = [
  ["what's running / load on each machine", 'fleet'],
  ['gate status', 'gate'],
  ['quota status', 'quota'],
  ['my open PRs and CI', 'github-open'],
  ['Remember that my test marker is an orange triangle', 'remember'],
  ['What did I tell you about test marker?', 'recall'],
  ['summarize my texts', 'texts'],
  ['Summarize my recent text messages.', 'texts'],
  ['Could you summerize my recent texts?', 'texts'],
  ['Sumarise my iMessages', 'texts'],
  ['Summmarize my SMS', 'texts'],
];

const short = (prompt) => ({ model: fastModelRef.split('/')[1], reason: 'short-chat', bytes: prompt.length, fallbacks: [] });
const intentHooks = {
  legacyStatus: () => undefined,
  legacyRequest: () => undefined,
  isControl: () => false,
  isSimple: (text) => standaloneSimple(text, short, {}),
};

test('the Captain overlay loads through OpenClaw’s synchronous module loader', () => {
  const script = 'const overlay = require("./first-officer/segment1/register.mjs");' +
    'if (typeof overlay.registerSegmentOne !== "function") process.exitCode = 1;';
  const result = spawnSync(process.execPath, ['-e', script], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
});

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
  assert.notEqual(classifyRequest('summerize my texts and send a message', intentHooks)?.kind, 'texts');
});

test('the Andrea address claims only private messaging intents and keeps other prompts on their usual route', () => {
  for (const text of [
    '@andrea summarize my texts',
    '@Andrea, what are my recent text messages?',
    '@andrea: catch me up on my texts',
    '@ANDREA Could you summerize my recent texts?',
  ]) {
    assert.deepEqual(classifyRequest(text, intentHooks), { kind: 'texts' });
  }
  for (const text of [
    '@andrea', '@andrea help', '@andrea gate status', '@andrea send a text',
    '@andrea draft a reply', '@andrea what is a branch?', '@andrea summarize my texts and send a message',
    '@andrea summarize my texts\n' + ' '.repeat(1800) + 'x',
  ]) {
    assert.deepEqual(classifyRequest(text, intentHooks), { kind: 'andrea-help' });
  }
  for (const text of ['@andreas summarize my texts', '@andrea_reader summarize my texts', 'explain @andrea']) {
    assert.ok(!['texts', 'andrea-help'].includes(classifyRequest(text, intentHooks)?.kind));
  }
  assert.deepEqual(classifyRequest('define an algorithm', intentHooks), { kind: 'simple', prompt: 'define an algorithm' });
  assert.deepEqual(classifyRequest('gate status', intentHooks), { kind: 'gate' });
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

test('all Segment 1 handlers are registered on the private inbound hook', async (t) => {
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
  for (const [isGroup, ctx] of [
    [true, { senderId: owner }],
    [false, { senderId: '8' }],
    [false, { senderId: owner, accountId: 'other' }],
    [false, { senderId: owner, threadId: 1 }],
  ]) {
    assert.equal(await dispatch({ content: '@andrea summarize my texts', isGroup }, ctx), undefined);
  }
  assert.equal(events.length, 0);

  assert.match((await dispatchOwner('gate status')).text, /live gate status check failed/);
  assert.match((await dispatchOwner('quota status')).text, /Quota checked/);
  assert.match((await dispatchOwner('pr 1 ci')).text, /PR checked/);
  assert.match((await dispatchOwner('remember marker: synthetic')).text, /Memory check received/);
  assert.match((await dispatchOwner('what did I tell you about marker?')).text, /Saved fact verified/);
  assert.match((await dispatchOwner('summarize my texts')).text, /Andrea.*synced/);
  assert.match((await dispatchOwner('Could you summerize my recent texts?')).text, /Andrea.*synced/);
  assert.match((await dispatchOwner('@andrea summarize my texts')).text, /Andrea.*synced/);
  assert.match((await dispatchOwner('@andrea send a text')).text, /@andrea summarize my texts/);
  assert.match((await dispatchOwner('define an algorithm')).text, /fast model.*failed/i);
  assert.equal(events.filter((event) => event === 'ack').length, 10);
  assert.equal(events.filter((event) => event === 'typing').length, 10);
  assert.equal(events.some((event) => event.includes('synthetic transport detail')), false);

  t.mock.method(services, 'createAndreaReader', () => assert.fail('The Mac should use its local reader'));
  const priorNode = process.env.FO_LOCAL_NODE;
  process.env.FO_LOCAL_NODE = 'mac-mini';
  try {
    services.readMacAndrea = (signal) => readMacAndrea(signal, async (command) => {
      assert.equal(command, andreaCommand);
      return {
        version: 1,
        groupSummary: 'Andrea · activity in approved group chats from the last 48 hours.\n2 incoming, 1 outgoing across 1 recent conversations.',
        activity: {
          topics: ['scheduling'], developments: ['question'],
          latest: { intent: 'ask-time', topic: 'scheduling', reply: 'earlier' },
        },
      };
    });
    const reply = (await dispatchOwner('@andrea summarize my texts')).text;
    assert.match(reply, /Latest incoming · asks about timing/);
    assert.match(reply, /after your last outgoing message/);
    services.readMacAndrea = async () => { throw new Error('synthetic Mac transport detail'); };
    assert.match((await dispatchOwner('@andrea summarize my texts')).text, /live read through Andrea failed/);
  } finally {
    if (priorNode === undefined) delete process.env.FO_LOCAL_NODE;
    else process.env.FO_LOCAL_NODE = priorNode;
  }
});

test('short answers use the configured flash model with bounded context and skip the slow fallback probe', async () => {
  const classify = (prompt) => short(prompt);
  const selection = routeOwnerModel({ prompt: 'hello' }, {}, classify);
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

test('contextual OpenClaw fallback stays on registered LiteLLM models', () => {
  const flash = fastModelRef;
  const strong = 'litellm/minimax-m3';
  const research = 'litellm/gemini-3.1-pro';
  const alternate = 'litellm/fixture-fallback';
  const config = {
    agents: { defaults: { model: { fallbacks: [
      'minimax/MiniMax-M3', research, flash, 'litellm/unregistered', alternate, strong,
    ] } } },
    models: { providers: { litellm: { models: [flash, strong, research, alternate].map((ref) => ({ id: ref.split('/')[1] })) } } },
  };
  const route = () => ({
    model: 'minimax-m3', reason: 'deliberate-task', fallbacks: [flash, 'direct/fixture', strong],
  });
  const selected = routeOwnerModel({ prompt: 'review the fixture' }, config, route,
    () => assert.fail('The direct subscription probe must stay off the routed model path'));
  assert.equal(selected.model, 'minimax-m3');
  assert.deepEqual(selected.fallbacks, [research, flash, alternate]);
  assert.deepEqual(routeOwnerModel({ prompt: 'review the fixture' }, {}, route).fallbacks, []);
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

test('a Nuc Captain never presents its local load or a legacy snapshot as Bobserver evidence', async () => {
  const now = Date.parse('2026-11-20T04:00:00Z');
  const read = async () => ({ ts: now / 1000, host: { heavy_jobs: 2 }, lane_counts: { running: 1 } });
  const options = { read, load: () => [1, 2, 3], now: () => now, local: 'nucbox' };
  const result = await fleetStatus({}, options);
  assert.match(result, /NucBox · 1\.00 \/ 2\.00 \/ 3\.00.*job count unavailable/);
  assert.match(result, /Bobserver · current load and tracked jobs unavailable/);
  assert.doesNotMatch(result, /2 heavy jobs|1 running/);
  const localRequest = classifyRequest('load', { ...intentHooks, legacyStatus: () => 'load' });
  const localResult = await fleetStatus(localRequest, options);
  assert.match(localResult, /NucBox/);
  assert.doesNotMatch(localResult, /Bobserver/);
  const localSnapshot = async () => ({
    ts: now / 1000, host: { node: 'nucbox', heavy_jobs: 1 }, lane_counts: { running: 1 },
  });
  assert.match(await fleetStatus({}, { ...options, read: localSnapshot }), /NucBox.*1 heavy jobs/);
  const capacity = async () => ({ node: 'nucbox', observed: now / 1000, used: 2, limit: 3 });
  assert.match(await fleetStatus({}, { ...options, capacity }), /NucBox.*2\/3 tracked heavy processes/);
  const unavailable = await fleetStatus({}, { ...options, read: localSnapshot,
    capacity: async () => { throw new Error('unavailable'); } });
  assert.match(unavailable, /NucBox.*job count unavailable/);
  assert.doesNotMatch(unavailable, /1 heavy jobs/);
});

test('standalone gate status requires current evidence from the active host', async () => {
  const ts = 1_800_000_000;
  const base = { observed: ts, node: 'mac-mini', hold: true, running: false };
  const options = { now: () => ts * 1000, local: 'mac-mini' };
  assert.match(await standaloneGateStatus({ ...options, read: async () => base }), /HOLD is active/);
  await assert.rejects(standaloneGateStatus({ ...options, local: 'nucbox', read: async () => base }), /unavailable/);
  await assert.rejects(standaloneGateStatus({ ...options, read: async () => ({ ...base, observed: ts - 301 }) }),
    /unavailable/);
  await assert.rejects(standaloneGateStatus({ ...options, read: async () => ({ ...base, hold: undefined }) }), /unavailable/);
});

test('standalone quota rejects an expired window before reporting usage', async () => {
  const observed = 1_800_000_000;
  const receipt = { observed, session: { used: 15, reset: observed + 1800 },
    weekly: { used: 31, reset: observed + 5000 } };
  const now = () => observed * 1000;
  assert.match(await standaloneQuotaStatus({ read: async () => receipt, now }), /5-hour · 15% used/);
  const weeklyOnly = await standaloneQuotaStatus({ read: async () => ({ observed, weekly: receipt.weekly }), now });
  assert.match(weeklyOnly, /5-hour · unavailable/);
  assert.match(weeklyOnly, /weekly · 31% used/);
  await assert.rejects(standaloneQuotaStatus({ read: async () => ({ observed }), now }), /windows are unavailable/);
  await assert.rejects(standaloneQuotaStatus({ read: async () => ({ ...receipt, observed: observed - 61 }), now }),
    /evidence is unavailable/);
  await assert.rejects(standaloneQuotaStatus({ read: async () => ({ ...receipt, session: { used: 15, reset: observed } }), now }),
    /windows are unavailable/);
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
  const timestamp = new Date(now + 20).toISOString();
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

test('the Mac key runs only the fixed read-only summary and returns aggregate activity', async () => {
  await assert.rejects(runMacAndrea('anything else', () => assert.fail('No settings access')), /Only the Andrea summary/);
  const now = new Date().toISOString();
  const privatePreview = 'Confirm the meeting on the fictional outpost';
  const syntheticToken = 'a'.repeat(32);
  const requests = [];
  const result = await runMacAndrea(andreaCommand, () => ({
    BLUEBUBBLES_CONTROL_BASE_URL: 'http://localhost:4315',
    BLUEBUBBLES_CONTROL_TOKEN: syntheticToken,
    BLUEBUBBLES_CANONICAL_SELF_THREAD_JID: 'bb:owner',
    BLUEBUBBLES_SELF_THREAD_ALIAS_JIDS: 'bb:owner-alias',
  }), async (url, options) => {
    requests.push(url.pathname);
    assert.equal(url.hostname, 'localhost');
    assert.equal(options.method, 'GET');
    assert.equal(options.redirect, 'error');
    assert.equal(options.headers.Authorization, 'Bearer ' + syntheticToken);
    if (url.pathname === '/v1/bluebubbles/chats') {
      return Response.json({ chats: ['owner', 'owner-alias', 'other'].map((id) => ({
        chatJid: 'bb:' + id, isGroup: false, lastMessageAt: now,
      })) });
    }
    assert.equal(url.pathname, '/v1/bluebubbles/chats/bb%3Aother/messages');
    return Response.json({ messages: [{ chatJid: 'bb:other', timestamp: now, preview: privatePreview,
      isBotMessage: false, isFromMe: false }] });
  });
  assert.equal(requests.length, 2);
  assert.match(result.summary, /Topics detected in previews · scheduling, follow-up/);
  assert.match(parseAndreaSummary(JSON.stringify(result)), /Topics · scheduling/);
  assert.doesNotMatch(result.summary, /fictional outpost|bb:/);
  assert.equal(JSON.stringify(result).includes(syntheticToken), false);
  assert.throws(() => parseAndreaSummary(JSON.stringify({ ...result, summary: result.summary + '\n' + privatePreview })),
    /response was invalid/);
});

test('Andrea SSH pins one command and renders safe findings from approved Mac group history', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'fo-reader-fixture-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const now = Date.parse('2026-11-20T06:00:00Z');
  const group = 'iMessage;+;fixture-approved';
  const privateMarker = 'fixture-private-marker';
  const payload = await runMacAndrea(andreaCommand, () => ({
    BLUEBUBBLES_CONTROL_TOKEN: 'x'.repeat(24),
    BLUEBUBBLES_CANONICAL_SELF_THREAD_JID: 'bb:iMessage;-;fixture-owner',
    BLUEBUBBLES_BASE_URL: 'https://fixture.invalid',
    BLUEBUBBLES_PASSWORD: 'fixture-provider-credential',
    BLUEBUBBLES_ALLOWED_CHAT_GUIDS: group,
  }), async (url, options) => {
    assert.equal(options.method, 'GET');
    if (url.pathname.endsWith('/status')) return Response.json({ status: { chatScope: 'allowlist' } });
    if (url.pathname.endsWith('/chats')) return Response.json({ chats: [] });
    assert.equal(decodeURIComponent(url.pathname), '/api/v1/chat/' + group + '/message');
    return Response.json({ data: [
      { guid: 'fixture-row-1', text: 'What time is the meeting? ' + privateMarker,
        dateCreated: now, isFromMe: false, chats: [{ guid: group, isGroup: true }] },
      { guid: 'fixture-row-2', text: 'The meeting was confirmed.',
        dateCreated: now - 1000, isFromMe: true, chats: [{ guid: group, isGroup: true }] },
      { guid: 'fixture-row-3', text: 'I attached the document.',
        dateCreated: now - 2000, isFromMe: false, chats: [{ guid: group, isGroup: true }] },
      { guid: 'fixture-row-4', text: 'I paid the invoice.', dateCreated: now,
        isFromMe: false, chats: [{ guid: 'iMessage;+;fixture-unapproved', isGroup: true }] },
    ] });
  }, now);
  assert.equal(payload.version, 1);
  assert.equal(payload.summary, 'Andrea · synced one-to-one text activity from the last 48 hours.\n' +
    'Andrea returned no recent previews in the chats checked.');
  assert.equal(payload.groupSummary, [
    'Andrea · activity in approved group chats from the last 48 hours.',
    '2 incoming, 1 outgoing across 1 recent conversations.',
    'The latest message was incoming in 1 of those conversations.',
    'Topics detected in previews · scheduling.',
  ].join('\n'));
  const wire = JSON.stringify(payload);
  const privateData = /fixture-(?:private-marker|approved|unapproved|owner|row|provider-credential)|fixture\.invalid|What time is the meeting|The meeting was confirmed|I attached the document|I paid the invoice/;
  assert.doesNotMatch(wire, privateData);
  await Promise.all([
    writeFile(join(directory, 'route.json'), JSON.stringify({ host: 'fixture-mac', user: 'fixture' }), { mode: 0o600 }),
    writeFile(join(directory, 'id_ed25519'), 'fixture identity', { mode: 0o600 }),
    writeFile(join(directory, 'known_hosts'), 'fixture host pin', { mode: 0o600 }),
  ]);
  const signal = AbortSignal.timeout(30_000);
  // Provisioning loads the gateway parser as a standalone data URI.
  const source = await readFile(new URL('../../first-officer/segment1/andrea-ssh.mjs', import.meta.url));
  const standalone = await import('data:text/javascript;base64,' + source.toString('base64'));
  const summary = await standalone.sshAndreaSummary(signal, { directory, run: async (file, args, options) => {
    assert.equal(file, '/usr/bin/nice');
    assert.deepEqual(args.slice(-2), ['fixture-mac', andreaCommand]);
    assert.ok(args.includes('-oStrictHostKeyChecking=yes'));
    assert.ok(args.includes('-oBatchMode=yes'));
    assert.ok(args.includes('-oClearAllForwardings=yes'));
    assert.equal(options.signal, signal);
    assert.ok(options.timeout < budgets.status);
    return { stdout: wire };
  } });
  assert.match(summary, /approved group chats from the last 48 hours/);
  assert.match(summary, /\b2 incoming, 1 outgoing across 1 recent conversation\./);
  assert.match(summary, /Topics · scheduling/);
  assert.match(summary, /Recent activity · questions, plan confirmations, shared items\./);
  assert.match(summary, /Latest incoming · asks about timing\./);
  assert.match(summary, /It came after your last outgoing message in that conversation\./);
  assert.doesNotMatch(summary, /The latest message was incoming|payments/);
  assert.doesNotMatch(summary, privateData);
});

test('Andrea rejects arbitrary activity details and preserves validated legacy summaries', () => {
  const summary = 'Andrea · synced one-to-one text activity from the last 48 hours.\n' +
    '1 incoming, 0 outgoing across 1 recent conversations.\n' +
    'The latest message was incoming in 1 of those conversations.';
  const legacy = { version: 1, summary };
  const unclassified = { topics: [], developments: [], latest: { intent: 'other', topic: null, reply: 'none' } };
  const parse = (payload) => parseAndreaSummary(JSON.stringify(payload));
  assert.equal(parse(legacy), summary);
  assert.match(parse({ ...legacy, activity: unclassified }),
    /The recent previews did not give Andrea a clear topic or request\./);

  const marker = 'fixture-private-preview';
  for (const activity of [
    { ...unclassified, topics: ['scheduling', marker] },
    { ...unclassified, topics: ['scheduling', 'travel', 'logistics', 'work', 'events'] },
    { ...unclassified, developments: [marker] },
    { ...unclassified, latest: { ...unclassified.latest, intent: marker } },
    { ...unclassified, latest: { ...unclassified.latest, topic: marker } },
    { ...unclassified, latest: { ...unclassified.latest, reply: marker } },
    { ...unclassified, latest: { ...unclassified.latest, preview: marker } },
    { ...unclassified, preview: marker },
    marker,
  ]) {
    assert.equal(parse({ ...legacy, activity }), summary);
  }
  assert.throws(() => parse({ ...legacy, summary: summary + '\n' + marker, activity: unclassified }),
    /response was invalid/);
});

test('the shared status publisher is accepted by the fleet reader', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'fo-status-'));
  const evidence = join(dir, 'evidence.json');
  const snapshot = join(dir, 'STATUS.json');
  await writeFile(evidence, JSON.stringify({
    heavy_jobs: 1,
    lane_counts: { running: 2, queued: 1 },
    nodes: {
      'mac-mini': { load1: 1.25, load5: 1, load15: 0.5, codex_jobs: 1, test_jobs: 0, age_s: 1 },
      nucbox: { load1: 99, load5: 99, load15: 99, codex_jobs: 4, test_jobs: 4, age_s: 900 },
    },
  }));
  const child = spawnSync('python3', ['first-officer/status/publish.py', 'publish', evidence, snapshot],
    { encoding: 'utf8' });
  try {
    assert.equal(child.status, 0, child.stderr);
    const read = await readFleetSnapshot(snapshot);
    assert.equal(read.v, 1);
    assert.equal(read.host.heavy_jobs, 1);
    assert.equal(read.lane_counts.running, 2);
    assert.equal(read.nodes['mac-mini'].codex_jobs, 1);
    assert.deepEqual(read.nodes.nucbox, { missing: true, stale: true });
    assert.ok(Math.abs(Date.parse(read.generated_at) - read.ts * 1000) < 1000);
    const text = await fleetStatus({}, {
      read: async () => read, load: () => [0.1, 0.1, 0.1], now: () => read.ts * 1000,
    });
    assert.match(text, /Mac Mini · 1\.25 \/ 1\.00 \/ 0\.50/);
    assert.match(text, /NucBox · current load and tracked jobs unavailable/);
    assert.doesNotMatch(text, /99\.00/);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});


test('standalone quick chat fails closed when the local model route cannot be verified', async () => {
  const alias = 'qwen-local-fast';
  const reference = 'litellm/' + alias;
  const config = {
    agents: { defaults: { model: { primary: reference, fallbacks: [] } } },
    plugins: { entries: { 'first-officer-conductor': { llm: { allowedCompletionModels: [reference] } } } },
    models: { providers: { litellm: { baseUrl: 'http://127.0.0.1:41502/v1', models: [{ id: alias }] } } },
  };
  const calls = [];
  const runtime = { llm: { complete: async ({ model }) => {
    calls.push(model);
    return { text: 'A fixture answer.', stopReason: 'stop' };
  } } };
  const turn = { owner: 'fixture-owner', signal: new AbortController().signal };
  const chat = createQuickChat(runtime, (value) => value, { requireLocal: true });
  for (const invalid of [
    { ...config, models: {} },
    { ...config, agents: { defaults: { model: { primary: reference, fallbacks: [fastModelRef] } } } },
    { ...config, models: { providers: { litellm: { ...config.models.providers.litellm, baseUrl: 'https://example.invalid/v1' } } } },
  ]) await assert.rejects(chat.answer({ prompt: 'define a branch' }, turn, invalid), /local Qwen/i);
  assert.deepEqual(calls, []);
  assert.equal(await chat.answer({ prompt: 'define a branch' }, turn, config), 'A fixture answer.');
  assert.deepEqual(calls, [reference]);

  const classify = () => ({ reason: 'deliberate-task', model: 'minimax-m3',
    fallbacks: [fastModelRef, 'litellm/gemini-3.1-pro'] });
  const context = { prompt: 'Explain the longer context', attachments: [] };
  const selection = routeOwnerModel(context, config, classify);
  assert.equal(selection.model, alias);
  assert.deepEqual(selection.fallbacks, []);
});

test('the approved LiteLLM alias can serve simple turns from an independent local proxy', async () => {
  const alias = 'qwen-local-fast';
  const reference = 'litellm/' + alias;
  const routeFor = () => ({ reason: 'short-chat' });
  const config = {
    plugins: { entries: { 'first-officer-conductor': { llm: { allowedCompletionModels: [reference] } } } },
    models: { providers: { litellm: { models: [{ id: alias }] } } },
  };
  const requests = [];
  const runtime = { llm: { complete: async ({ model }) => {
    requests.push(model);
    return { text: 'A fixture answer.', stopReason: 'stop' };
  } } };
  assert.equal(routeOwnerModel({ prompt: 'define a branch' }, config, routeFor).model, alias);
  await answerSimple(runtime, 'define a branch', new AbortController().signal, config);
  await answerSimple(runtime, 'define a branch', new AbortController().signal, { ...config, models: {} });
  assert.deepEqual(requests, [reference, fastModelRef]);
});

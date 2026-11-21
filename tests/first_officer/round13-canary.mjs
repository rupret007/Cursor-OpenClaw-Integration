// Run against a private, inventory-verified copy of the gateway release.
import assert from 'node:assert/strict';
import { mkdir, readdir, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const release = process.env.FO_RELEASE_ROOT;
assert.ok(release, 'Set FO_RELEASE_ROOT to the release under test');
const home = homedir();
assert.equal(process.env.FO_CANARY_HOME, home, 'Use a new private test home');
assert.deepEqual(await readdir(home), [], 'The test home must start empty');
process.umask(0o077);
const { default: conductor } = await import(pathToFileURL(
  join(release, 'round6/runtime/first-officer-conductor.mjs'),
));
const hooks = new Map();
const interactive = new Map();
const sent = [];
let typing = 0;
let models = 0;
const owner = '7';
const context = {
  accountId: 'default', agentId: 'main', channel: 'telegram', channelId: 'telegram',
  conversationId: owner, senderId: owner, messageId: '1', sessionKey: 'agent:main:main',
};
const flashModel = 'litellm/minimax-m3.1-flash';
const deliberateModel = 'litellm/minimax-m3';
const researchModel = 'litellm/gemini-3.1-pro';
const alternateModel = 'litellm/fixture-fallback';
const directModel = 'direct/fixture-model';
const noop = () => {};
const api = {
  config: {
    channels: {
      telegram: {
        dmPolicy: 'allowlist', allowFrom: [owner], execApprovals: { approvers: [owner] },
      },
    },
    agents: { defaults: { model: {
      fallbacks: [directModel, researchModel, flashModel, 'litellm/unregistered', alternateModel, deliberateModel],
    } } },
    models: { providers: {
      litellm: { models: [flashModel, deliberateModel, researchModel, alternateModel].map((ref) => ({
        id: ref.split('/')[1],
      })) },
      direct: { models: [{ id: 'fixture-model' }] },
    } },
  },
  logger: { info: noop, warn: noop, debug: noop, error: noop },
  on(name, handler, { priority = 0 } = {}) {
    hooks.set(name, [...(hooks.get(name) ?? []), { handler, priority }]);
  },
  registerCommand: noop,
  registerService: noop,
  registerInteractiveHandler: ({ namespace, handler }) => interactive.set(namespace, handler),
  registerGatewayMethod: noop,
  runtime: {
    channel: {
      telegram: { sendTypingTelegram: async () => { typing += 1; } },
      outbound: { async loadAdapter(channel) {
        assert.equal(channel, 'telegram');
        const send = async (row) => {
          assert.equal(row.to, owner);
          sent.push(row);
          return { target: { id: owner }, messageId: String(sent.length) };
        };
        return { sendText: send, sendPayload: send };
      } },
    },
    llm: { async complete(params) {
      models += 1;
      assert.equal(params.model, flashModel);
      assert.equal(params.messages.length, 1);
      return { text: 'A branch is a movable reference to a commit.', stopReason: 'stop' };
    } },
  },
};
conductor.register(api);

async function dispatch(content, ctx = context) {
  const event = { content, isGroup: false };
  for (const { handler } of hooks.get('before_dispatch').sort((a, b) => b.priority - a.priority)) {
    const result = await handler(event, ctx);
    if (result?.handled) return result;
  }
}

const modelHook = hooks.get('before_model_resolve').at(-1).handler;
const savedProviderFetch = globalThis.fetch;
let providerFetches = 0;
globalThis.fetch = async () => {
  providerFetches += 1;
  assert.fail('Model selection must not call a provider');
};
try {
  assert.deepEqual(await modelHook({ prompt: 'Review this synthetic design' }, context), {
    providerOverride: 'litellm',
    modelOverride: 'minimax-m3',
    fallbacksOverride: [researchModel, flashModel, alternateModel],
  });
  assert.deepEqual(await modelHook({ prompt: 'Research the sample problem' }, context), {
    providerOverride: 'litellm',
    modelOverride: 'gemini-3.1-pro',
    fallbacksOverride: [deliberateModel, flashModel, alternateModel],
  });
  assert.deepEqual(await modelHook({ prompt: 'Define a branch' }, context), {
    providerOverride: 'litellm',
    modelOverride: 'minimax-m3.1-flash',
    fallbacksOverride: [],
  });
  assert.equal(providerFetches, 0);
} finally {
  globalThis.fetch = savedProviderFetch;
}

const request = 'my open PRs and CI';
const reply = await dispatch(request);
const promptHook = hooks.get('before_prompt_build').at(-1).handler;
const callGuard = hooks.get('before_tool_call').find(({ priority }) => priority === 100).handler;
const modelTools = await promptHook({ prompt: request }, context);
assert.deepEqual(modelTools.toolsAllow, [], 'The generic model must remain tool-free');
assert.equal(callGuard({ toolName: 'exec' }, context)?.block, true, 'Unapproved exec must stay blocked');
assert.equal(reply?.handled, true, 'Owner PR summary must be claimed before a tool-free model turn');
// The isolated canary supplies no gh credentials or backend, so the failure must be explicit.
assert.match(reply.text, /GitHub.*(?:failed|unavailable)/i);
assert.equal(await dispatch(request, { ...context, senderId: '8', conversationId: '8' }), undefined);
assert.equal(sent.length, 1, 'Only the owner receives progress');

const sampleAt = Math.floor(Date.now() / 1000);
const stamp = new Date(sampleAt * 1000).toISOString();
const sample = { timestamp: stamp, load1: 3, load5: 2, load15: 1, codex_jobs: 2, test_jobs: 1 };
await mkdir(join(home, 'work/status'), { recursive: true });
const snapshot = {
  v: 1, ts: sampleAt, generated_at: stamp, alerts: [],
  gates: [{ gate: 'running', scope: 'full', sha: 'a'.repeat(40) }],
  lanes: [], lane_counts: { running: 1 },
  gate_counts: { running: 1 },
  host: { heavy_jobs: 1, locks: [{ slot: 'fullgate', held: true, live: true }] },
  nodes: { 'mac-mini': sample, nucbox: sample, 'macbook-pro': sample },
};
await writeFile(join(home, 'work/status/STATUS.json'), JSON.stringify(snapshot));
const fleet = await dispatch("what's running / load on each machine");
for (const node of ['Mac Mini', 'NucBox', 'MacBook Pro']) {
  assert.ok(fleet.text.includes(node + ' · 3.00 / 2.00 / 1.00'), 'Fleet must include each remote reading');
}
assert.match(fleet.text, /Bobserver · [\d.]+ \/ [\d.]+ \/ [\d.]+ at /);
assert.match(fleet.text, /Lane counts at .* · 1 running/);
assert.match((await dispatch('gate status')).text, /full gate was running on aaaaaaaa/);
assert.match((await dispatch('quota status')).text, /Quota checked at /);
assert.match((await dispatch('captain status')).text, /Captain state or evidence is unavailable/);
assert.match((await dispatch('captain this: deploy')).text, /verify CI for PR #33/);

const remember = await dispatch('remember that my segment marker is orange triangle');
assert.equal(remember.handled, true);
assert.equal(remember.text, undefined, 'The approval card is the final reply');
const card = sent.find((row) => row.payload);
assert.match(card.text, /Remember this fact for segment-marker/);
const readFact = () => dispatch('what did I tell you about segment marker?');
assert.match((await readFact()).text, /No verified saved fact/);
let edited;
const callback = card.payload.channelData.telegram.buttons[0][0].callback_data;
assert.match(callback, /^fo-memory-live:/);
await interactive.get('fo-memory-live')({
  auth: { isAuthorizedSender: true },
  accountId: 'default', isGroup: false, senderId: owner,
  callback: { chatId: owner, payload: callback.slice('fo-memory-live:'.length) },
  respond: { editMessage: async (change) => { edited = change.text; } },
});
assert.match(edited, /Fact saved for segment-marker/);
assert.match((await readFact()).text, /Saved fact for segment-marker\norange triangle/);

const andrea = join(home, '.config/openclaw-firstofficer');
await mkdir(andrea, { recursive: true });
const loopback = [127, 0, 0, 1].join('.');
await writeFile(join(andrea, 'andrea-reader.json'), JSON.stringify({
  baseUrl: 'http://' + loopback + ':1717', token: 'a'.repeat(32), selfChat: 'bb:fixture-self',
}));
const savedFetch = globalThis.fetch;
const syntheticPreview = 'Meet tomorrow with the demonstration character';
const reads = [];
globalThis.fetch = async (url, options) => {
  assert.equal(url.hostname, loopback);
  assert.equal(options.method, 'GET');
  assert.equal(options.redirect, 'error');
  reads.push(url.pathname);
  if (url.pathname === '/v1/bluebubbles/chats') return new Response(JSON.stringify({
    chats: [{ chatJid: 'bb:fixture', name: 'fixture contact', lastMessageAt: stamp, isGroup: false }],
  }));
  assert.equal(url.pathname, '/v1/bluebubbles/chats/bb%3Afixture/messages');
  return new Response(JSON.stringify({
    messages: [{ chatJid: 'bb:fixture', preview: syntheticPreview, timestamp: stamp, isBotMessage: false, isFromMe: false }],
  }));
};
try {
  const summaries = [
    'summarize my texts',
    '@andrea summarize my texts',
    '@andrea what are my recent text messages',
  ];
  for (const request of summaries) {
    const summary = await dispatch(request);
    assert.equal(summary?.handled, true, 'Andrea summaries must be claimed before a model turn');
    assert.match(summary.text, /Andrea.*synced[\s\S]*1 incoming[\s\S]*scheduling/);
    assert.equal(summary.text.includes(syntheticPreview), false);
    assert.equal(summary.text.includes('fixture contact'), false);
  }
  const help = await dispatch('@andrea send a text to fixture contact: a fake reminder');
  assert.match(help?.text, /@andrea summarize my texts/);
  const progress = sent.length;
  assert.equal(await dispatch(summaries[1], { ...context, senderId: '8', conversationId: '8' }), undefined);
  assert.equal(sent.length, progress, 'Andrea progress is private to the owner');
  assert.equal(reads.length, summaries.length * 2, 'Only summary requests can read Andrea');
} finally {
  globalThis.fetch = savedFetch;
}
assert.equal(models, 0, 'Operational answers must never reach a model');
assert.match((await dispatch('define a branch')).text, /movable reference/);
assert.equal(models, 1);
assert.equal(sent.filter(({ text }) => text === 'On it.').length, typing);

import { homedir } from 'node:os';
import { join } from 'node:path';
import { classifyRequest, createDispatch } from './dispatch.mjs';
import { answerSimple, standaloneSimple } from './model-router.mjs';
import { fleetStatus, openPullRequests, summarizeAndrea } from './tools.mjs';

const account = 'default';
const expectedGithubAccount = 'rupret007';
const readerConfig = join(homedir(), '.config/openclaw-firstofficer/andrea-reader.json');
const plain = (text) => ({ text });

async function telegramSend(api, owner, liveConfig, text, signal, buttons) {
  const cfg = liveConfig(owner);
  const adapter = await api.runtime.channel.outbound.loadAdapter('telegram');
  signal.throwIfAborted();
  const context = { cfg, accountId: account, to: owner, text, signal, abortSignal: signal };
  const receipt = buttons
    ? await adapter.sendPayload({ ...context, payload: { text, channelData: { telegram: { buttons: [buttons] } } } })
    : await adapter.sendText(context);
  if (String(receipt?.target?.id) !== owner || !/^[1-9]\d{0,15}$/.test(String(receipt?.messageId))) {
    throw new Error('Private delivery could not be verified');
  }
}

async function telegramTyping(api, owner, liveConfig, signal) {
  const cfg = liveConfig(owner);
  const channel = cfg.channels.telegram;
  const legacyTyping = api.runtime.channel.telegram?.sendTypingTelegram;
  if (typeof legacyTyping === 'function') {
    await legacyTyping(owner, { cfg, accountId: account, signal });
    return;
  }
  // Use the host-resolved SecretRef. This direct activity call is limited to
  // the configured default account on the standard Telegram API.
  if (channel.apiRoot || channel.proxy || typeof channel.botToken !== 'string' ||
      !/^\d{6,12}:[A-Za-z0-9_-]{30,}$/.test(channel.botToken)) {
    throw new Error('Telegram typing transport is unavailable');
  }
  const response = await fetch('https://api.telegram.org/bot' + channel.botToken + '/sendChatAction', {
    method: 'POST', redirect: 'error', signal,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_id: owner, action: 'typing' }),
  });
  if (!response.ok || (await response.json()).result !== true) throw new Error('Telegram typing failed');
}

async function acknowledge(api, owner, liveConfig, signal) {
  const results = await Promise.allSettled([
    telegramTyping(api, owner, liveConfig, signal),
    telegramSend(api, owner, liveConfig, 'On it.', signal),
  ]);
  if (results.some((result) => result.status === 'rejected')) throw new Error('Telegram acknowledgment incomplete');
}

async function memoryReply(api, commands, liveConfig, request, { owner, signal }) {
  if (typeof request.args !== 'string' || request.args.length > 420) {
    return plain(request.kind === 'remember'
      ? 'Use “remember topic: fact” and tap Remember to save it.'
      : 'Use “what did I tell you about topic?” with one saved topic.');
  }
  const command = commands.get(request.kind);
  signal.throwIfAborted();
  const reply = await command.handler({
    config: liveConfig(owner),
    channel: 'telegram', accountId: account, from: owner, to: owner, senderId: owner,
    isAuthorizedSender: true, args: request.args,
  });
  const buttons = reply.presentation?.blocks?.[0]?.buttons;
  if (!buttons?.length) return plain(reply.text);
  await telegramSend(api, owner, liveConfig, reply.text, signal, buttons.map(({ label, action }) => ({
    text: label, callback_data: action.value,
  })));
  return {};
}

function andreaReader(signal, { readPrivateConfig, createAndreaReader }) {
  const config = readPrivateConfig(readerConfig);
  return createAndreaReader({
    config: () => config,
    transport: (url, options) => fetch(url, { ...options, signal: AbortSignal.any([signal, options.signal]) }),
  });
}

function toolRegistry(api, commands, liveConfig, services) {
  const { gateStatus, quotaStatus, githubReply, redacted } = services;
  const memory = (request, turn) => memoryReply(api, commands, liveConfig, request, turn);
  return {
    fleet: {
      run: async (request) => plain(await fleetStatus(request)),
      failure: 'The live fleet status check failed. Current load and jobs are unavailable.',
    },
    gate: {
      run: async () => plain(await gateStatus()),
      failure: 'The live gate status check failed. Current gate state is unavailable.',
    },
    overview: {
      run: async () => {
        const [fleet, gate] = await Promise.allSettled([fleetStatus({}), gateStatus()]);
        return plain([
          fleet.status === 'fulfilled' ? fleet.value : 'The live fleet status check failed.',
          gate.status === 'fulfilled' ? gate.value : 'The live gate status check failed.',
        ].join('\n\n'));
      },
      failure: 'The live fleet and gate status check timed out.',
    },
    quota: {
      run: async () => {
        let profile = 0;
        return plain((await quotaStatus()).replace(/^Codex [^·\r\n]+·/gm, () => 'Codex profile ' + ++profile + ' ·'));
      },
      failure: 'The live quota check failed. Current quota is unavailable.',
    },
    'github-open': {
      run: async (_, { signal }) => plain(await openPullRequests(signal, { expectedLogin: expectedGithubAccount })),
      failure: 'The live GitHub check failed. Open PR and CI status are unavailable.',
    },
    'github-pr': {
      run: async (request) => plain(await githubReply(request)),
      failure: 'The live GitHub check failed. Current PR or CI status is unavailable.',
    },
    'github-help': {
      run: () => plain('I couldn’t identify the live GitHub lookup. Ask for “my open PRs and CI” or give a repository and PR number.'),
    },
    'status-help': {
      run: () => plain('I couldn’t identify the live status lookup. Ask for fleet load, gate status, quota status, or GitHub PR and CI status.'),
    },
    remember: { run: memory, failure: 'The private memory request failed. The fact wasn’t confirmed as saved.' },
    recall: { run: memory, failure: 'The private memory lookup failed. I couldn’t verify a saved fact.' },
    texts: {
      run: async (_, { signal }) => plain(await summarizeAndrea(andreaReader(signal, services))),
      failure: 'The live read through Andrea failed. Your recent text activity is unavailable.',
    },
    simple: {
      run: async ({ prompt }, { signal }) => {
        const answer = redacted(await answerSimple(api.runtime, prompt, signal), 1250);
        if (!answer) throw new Error('Fast answer unavailable');
        return plain(answer);
      },
      failure: 'The fast model didn’t return an answer in time or the request failed.',
    },
  };
}

export function registerSegmentOne(api, commands, services) {
  const { incomingOwner, telegramOwner, statusTopic, parseRequest, controlIntent, routeFor } = services;
  const currentConfig = () => api.runtime.config?.current?.() ?? api.config;
  const liveConfig = (owner) => {
    const cfg = currentConfig();
    if (telegramOwner(cfg, account) !== owner) throw new Error('Private owner is unavailable');
    return cfg;
  };
  const registry = toolRegistry(api, commands, liveConfig, services);
  const dispatch = createDispatch({
    authorize(event, ctx) {
      const cfg = currentConfig();
      if ((ctx.accountId ?? account) === account && ctx.threadId == null && incomingOwner(cfg, event, ctx)) {
        return telegramOwner(cfg, account);
      }
    },
    classify: (text, event) => classifyRequest(text, {
      legacyStatus: statusTopic, legacyRequest: parseRequest, isControl: controlIntent,
      isSimple: (raw) => standaloneSimple(raw, routeFor, event),
    }),
    acknowledge: (owner, _, signal) => acknowledge(api, owner, liveConfig, signal),
    registry,
    warn: (line) => api.logger.warn(line),
  });
  // The existing inbound safety hook runs at 250. Guarded operations retain
  // their handlers because control requests pass through to them.
  api.on('before_dispatch', dispatch, { priority: 240, timeoutMs: 22_000 });
}

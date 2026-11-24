import { createHash, randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import { isIP } from 'node:net';
import { join } from 'node:path';
import { readMacAndrea } from './andrea-mac.mjs';
import { sshAndreaSummary } from './andrea-ssh.mjs';
import { createCaptain, localState, projectCI, resolveCaptainStateScript } from './captain.mjs';
import { createProjectBrief } from './project-brief.mjs';
import { classifyRequest, createDispatch } from './dispatch.mjs';
import { createQuickChat, isLocalQuickChat, quickFollowUp, standaloneSimple } from './model-router.mjs';
import { fleetStatus, localFleetNode, openPullRequests, readIntegrationGithub, readProjectGithub, standaloneGateStatus, standaloneQuotaStatus, summarizeAndrea } from './tools.mjs';

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

async function telegramDirect(owner, liveConfig, method, fields, signal) {
  const channel = liveConfig(owner).channels.telegram;
  const root = new URL(channel.apiRoot ?? 'https://api.telegram.org');
  const official = root.protocol === 'https:' && root.hostname === 'api.telegram.org' && !root.port;
  const loopback = root.protocol === 'http:' && (root.hostname === 'localhost' ||
    isIP(root.hostname) === 4 && root.hostname.startsWith('127.'));
  if ((!official && !loopback) || (process.env.FO_STANDBY_TELEGRAM === '1' && !loopback) ||
      root.username || root.password || root.search || root.hash ||
      root.pathname !== '/' || channel.proxy || typeof channel.botToken !== 'string' ||
      !/^\d{6,12}:[A-Za-z0-9_-]{30,}$/.test(channel.botToken)) {
    throw new Error('Direct Telegram transport is unavailable');
  }
  const response = await fetch(new URL('/bot' + channel.botToken + '/' + method, root), {
    method: 'POST', redirect: 'error', signal,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...fields, chat_id: owner }),
  });
  if (!response.ok) throw new Error('Direct Telegram delivery failed');
  const receipt = await response.json();
  if (receipt.ok !== true) throw new Error('Direct Telegram delivery could not be verified');
  return receipt.result;
}

async function telegramTyping(api, owner, liveConfig, signal) {
  const cfg = liveConfig(owner);
  const legacyTyping = api.runtime.channel.telegram?.sendTypingTelegram;
  if (typeof legacyTyping === 'function') {
    await legacyTyping(owner, { cfg, accountId: account, signal });
    return;
  }
  if (await telegramDirect(owner, liveConfig, 'sendChatAction', { action: 'typing' }, signal) !== true) {
    throw new Error('Telegram typing failed');
  }
}

function brokerMessage(ctx) {
  const value = String(ctx?.messageId);
  if (!/^[1-9]\d{0,15}$/.test(value) || !Number.isSafeInteger(Number(value))) {
    throw new Error('Brokered Telegram message needs its original ID');
  }
  return Number(value);
}

function projectTelegramDelivery(state, liveConfig, brokered) {
  return {
    async reserve(owner, ctx) {
      const message = String(ctx.messageId);
      if (!/^[1-9]\d{0,15}$/.test(message)) return;
      const key = createHash('sha256').update('fo-project-telegram-v1:' + owner + ':' + message).digest('hex');
      const reservation = await state('reserve-project-telegram', { key });
      if (reservation.result === 'reserved') return key;
      if (reservation.result !== 'duplicate') throw new Error('Project delivery cannot be reserved');
    },
    async deliver(key, owner, text, ctx) {
      let outcome = 'unverified';
      try {
        const body = text.length <= 4096 ? text : text.slice(0, 3800) + '\n\nSee the private handoff for the full record.';
        const fields = { text: body };
        if (brokered) fields.reply_parameters = { message_id: brokerMessage(ctx) };
        const receipt = await telegramDirect(owner, liveConfig, 'sendMessage', fields, AbortSignal.timeout(3000));
        if (String(receipt?.chat?.id) !== owner || !/^[1-9]\d{0,15}$/.test(String(receipt?.message_id))) {
          throw new Error('The project reply has no verified receipt');
        }
        outcome = 'delivered';
      } finally {
        const saved = await state('finish-project-telegram', { key, outcome });
        if (saved.result !== outcome) throw new Error('The project reply receipt could not be saved');
      }
    },
  };
}

async function acknowledge(api, owner, liveConfig, signal, ctx, brokered) {
  if (brokered) {
    const progress = await telegramDirect(owner, liveConfig, 'foProgress', { fo_message_id: brokerMessage(ctx) }, signal);
    if (progress?.typing !== 'confirmed' || progress?.ack !== 'confirmed') {
      throw new Error('Telegram acknowledgment incomplete');
    }
    return;
  }
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

async function brokerMemory(request, { owner, ctx, signal }, liveConfig) {
  if (request.kind === 'recall' && !request.args) return plain('Use “recall topic” with one saved topic.');
  const operation = request.kind === 'memory-confirm' ? 'confirm' : request.kind;
  const result = await telegramDirect(owner, liveConfig, 'foMemory', {
    fo_message_id: brokerMessage(ctx), operation, ...(operation === 'recall' ? { topic: request.args } : {}),
  }, signal);
  switch (result?.state) {
    case 'saved': return plain(`Saved fact for ${result.topic}\n${result.statement}`);
    case 'missing': return plain(`No verified saved fact for ${request.args}.`);
    case 'pending': return plain(`To save this fact for ${result.topic}, reply “confirm memory ${result.code}” within five minutes.`);
    case 'confirmed': return plain(`Fact saved for ${result.topic}.`);
    case 'expired': return plain('That memory confirmation expired or belongs to an earlier Captain. Ask me to remember the fact again.');
    case 'unsafe': return plain('That fact contains material I can’t save.');
    case 'busy': return plain('Too many memory confirmations are waiting. Try again in five minutes.');
    case 'syntax': return plain('Use “remember topic: fact” to open a confirmation.');
    default: throw new Error('Brokered private memory is unavailable');
  }
}

function andreaReader(signal, { readPrivateConfig, createAndreaReader }) {
  const config = readPrivateConfig(readerConfig);
  return createAndreaReader({
    config: () => config,
    transport: (url, options) => fetch(url, { ...options, signal: AbortSignal.any([signal, options.signal]) }),
  });
}

function toolRegistry(api, commands, liveConfig, services, state, chat) {
  const { quotaStatus, githubReply } = services;
  const statusFleet = (request) => fleetStatus(request, {
    capacity: localFleetNode() === 'bobserver' ? undefined : () => state('local-capacity'),
  });
  const gateStatus = () => localFleetNode() === 'bobserver' ? services.gateStatus() : standaloneGateStatus({ read: () => state('local-gate') });
  const memory = (request, turn) => memoryReply(api, commands, liveConfig, request, turn);
  return {
    fleet: {
      run: async (request) => plain(await statusFleet(request)),
      failure: 'The live fleet status check failed. Current load and jobs are unavailable.',
    },
    gate: {
      run: async () => plain(await gateStatus()),
      failure: 'The live gate status check failed. Current gate state is unavailable.',
    },
    overview: {
      run: async () => {
        const [fleet, gate] = await Promise.allSettled([statusFleet({}), gateStatus()]);
        return plain([
          fleet.status === 'fulfilled' ? fleet.value : 'The live fleet status check failed.',
          gate.status === 'fulfilled' ? gate.value : 'The live gate status check failed.',
        ].join('\n\n'));
      },
      failure: 'The live fleet and gate status check timed out.',
    },
    quota: {
      run: async () => {
        if (localFleetNode() !== 'bobserver') return plain(await standaloneQuotaStatus({ read: () => state('local-quota') }));
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
    'memory-confirm': { run: () => plain('Use the Remember button on this Captain to confirm a fact.') },
    recall: { run: memory, failure: 'The private memory lookup failed. I couldn’t verify a saved fact.' },
    texts: {
      run: async (_, { signal }) => {
        const local = localFleetNode();
        if (local === 'mac-mini') return plain(await (services.readMacAndrea ?? readMacAndrea)(signal));
        const summary = await sshAndreaSummary(signal);
        if (summary !== undefined) return plain(summary);
        if (local === 'nucbox') throw new Error('The local Andrea reader route is unavailable');
        return plain(await summarizeAndrea(andreaReader(signal, services)));
      },
      failure: 'The live read through Andrea failed. Your recent text activity is unavailable.',
    },
    'andrea-help': {
      run: () => plain('Andrea’s messaging access here is read-only. Try “@andrea summarize my texts” for recent activity in your approved chats.'),
    },
    simple: {
      run: async (request, turn) => {
        const answer = await chat.answer(request, turn, liveConfig(turn.owner));
        if (!answer) throw new Error('Fast answer unavailable');
        return plain(answer);
      },
      failure: localFleetNode() === 'bobserver' ? 'The fast model didn’t return an answer in time or the request failed.' :
        'The local Qwen route couldn’t answer. Check LiteLLM and the local model service.'
    },
  };
}

async function prepareTelegramSessionRecorder(api, cfg) {
  try {
    const agent = 'main';
    const session = api.runtime.channel.session;
    let failed = false;
    await session.recordInboundSession({
      storePath: session.resolveStorePath(cfg.session?.store, { agentId: agent }),
      sessionKey: 'agent:main:fo-startup-' + randomUUID(),
      ctx: {},
      createIfMissing: false,
      onRecordError() { failed = true; },
    });
    if (failed) throw new Error('Session startup preparation failed');
    api.logger.info('First Officer Telegram session writer ready.');
  } catch {
    api.logger.warn('First Officer could not prepare the Telegram session writer during startup.');
  }
}

export function registerSegmentOne(api, commands, services) {
  const { incomingOwner, telegramOwner, statusTopic, parseRequest, controlIntent, routeFor } = services;
  const brokered = process.env.FO_STANDBY_TELEGRAM === '1';
  const stagedStandby = brokered && Boolean(process.env.FO_STANDBY_EPOCH);
  const currentConfig = () => api.runtime.config?.current?.() ?? api.config;
  const liveConfig = (owner) => {
    const cfg = currentConfig();
    if (telegramOwner(cfg, account) !== owner) throw new Error('Private owner is unavailable');
    return cfg;
  };
  const cfg = currentConfig();
  if (localFleetNode() === 'nucbox' && isLocalQuickChat(cfg) &&
      !cfg.agents?.list?.length && typeof api.registerService === 'function') {
    api.registerService({
      id: 'fo-telegram-session-prepare', start: () => prepareTelegramSessionRecorder(api, cfg), stop() {},
    });
  }
  const script = resolveCaptainStateScript(currentConfig());
  const verifyBrokerCandidate = async (job, owner) => {
    try {
      const current = await telegramDirect(owner, liveConfig, 'foProjectInspect', { request: job.request }, AbortSignal.timeout(4000));
      if (current?.request?.lane !== job.request.lane) throw new Error('Candidate identity changed');
      if (current.state !== 'patch-ready') return current;
      if (current.commit !== job.commit) return { ...job, state: 'invalid-artifact' };
      const result = await localState('verify-brokered-project', { request: current.request, commit: current.commit }, script);
      return { ...current, ...result };
    } catch {
      return { ...job, state: 'ci-unavailable' };
    }
  };
  const state = async (operation, data, turn) => {
    if (stagedStandby && ['project-daily-due', 'save-project-daily'].includes(operation)) {
      const owner = telegramOwner(currentConfig(), account);
      const method = operation === 'project-daily-due' ? 'foProjectDailyDue' : 'foProjectDailySave';
      return telegramDirect(owner, liveConfig, method, data ?? {}, AbortSignal.timeout(2500));
    }
    if (stagedStandby && operation === 'dispatch-project') {
      return telegramDirect(turn.owner, liveConfig, 'foProjectSubmit', {
        ...data, fo_message_id: brokerMessage(turn.ctx),
      }, turn.signal);
    }
    const view = await localState(operation, data, script);
    if (!stagedStandby || !['view', 'handoff'].includes(operation)) return view;
    try {
      const owner = telegramOwner(currentConfig(), account);
      const status = await telegramDirect(owner, liveConfig, 'foProjectStatus', {}, AbortSignal.timeout(2500));
      if (typeof status?.enabled !== 'boolean' || !Array.isArray(status?.jobs)) throw new Error('Unknown shared coding status');
      let jobs = status.jobs;
      if (operation === 'handoff') {
        const targets = jobs.filter((job) => job.state === 'patch-ready').slice(0, 2);
        const verified = await Promise.all(targets.map((job) => verifyBrokerCandidate(job, owner)));
        jobs = jobs.map((job) => verified.find((item) => item.request.lane === job.request.lane) ?? job);
      }
      return { ...view, activeProjectJobs: jobs, lastProjectDaily: status.lastDaily,
        sharedProjectStatus: status.enabled ? 'ready' : 'disabled' };
    } catch {
      return { ...view, sharedProjectStatus: 'unavailable' };
    }
  };
  const chat = createQuickChat(api.runtime, services.redacted, { requireLocal: localFleetNode() !== 'bobserver' });
  const registry = toolRegistry(api, commands, liveConfig, services, state, chat);
  if (stagedStandby) {
    for (const kind of ['remember', 'recall', 'memory-confirm']) {
      registry[kind] = {
        run: (request, turn) => brokerMemory(request, turn, liveConfig),
        failure: 'Shared private memory is unavailable. I couldn’t verify this request.',
      };
    }
    registry.standby = { run: async () => plain(
      'This standby session can answer chat and live status. Interactive approvals and dispatch need the primary project state.',
    ) };
  }
  if (services.getGithub) {
    const readCI = localFleetNode() === 'bobserver' ? services.getGithub : readIntegrationGithub;
    const projectBrief = localFleetNode() === 'bobserver' ? undefined : createProjectBrief({
      config: () => state('local-projects'),
      read: readProjectGithub,
      record: stagedStandby ? undefined : (recommendation) => state('record-project-recommendation', recommendation),
      check(project, get) {
        const repository = 'repos/' + project.repo;
        return projectCI(repository, project.pr, project.checks,
          (path) => get(path.slice(repository.length + 1)));
      },
    });
    const captain = createCaptain({
      registry, get: readCI, state, projectBrief, dailyProjects: Boolean(projectBrief), inspectProjectJobs: !stagedStandby,
    });
    registry.captain = {
      run: async (request, turn) => plain(await captain.run(request, turn)),
      failure: 'Captain state or evidence is unavailable. Check “captain status” before retrying.',
    };
    let monitor;
    let checking = false;
    api.registerService({
      id: 'fo-captain-monitor',
      start() {
        if (monitor) return;
        monitor = setInterval(async () => {
          if (checking) return;
          checking = true;
          try {
            await captain.tick((text, owner) => telegramSend(api, owner, liveConfig, text, AbortSignal.timeout(4000)));
          } catch {
            api.logger.warn('Captain monitor could not verify its current state or delivery.');
          } finally {
            checking = false;
          }
        }, 5000);
        monitor.unref();
      },
      stop() {
        clearInterval(monitor);
        monitor = undefined;
      },
    });
    let projectMonitor;
    api.registerService({
      id: 'fo-captain-project-monitor',
      start() {
        if (projectMonitor) return;
        projectMonitor = setInterval(() => captain.tickProject().catch(() =>
          api.logger.warn('Project Captain could not verify the current worker result.')), 15_000);
        projectMonitor.unref();
      },
      stop() {
        clearInterval(projectMonitor);
        projectMonitor = undefined;
      },
    });
  }
  const dispatch = createDispatch({
    authorize(event, ctx) {
      const cfg = currentConfig();
      if ((ctx.accountId ?? account) === account && ctx.threadId == null && incomingOwner(cfg, event, ctx)) {
        return telegramOwner(cfg, account);
      }
    },
    classify(text, event) {
      let followUp = false;
      const request = classifyRequest(text, {
        legacyStatus: statusTopic, legacyRequest: parseRequest, isControl: controlIntent,
        isSimple(raw) {
          followUp = isLocalQuickChat(currentConfig()) && quickFollowUp(raw, routeFor, event);
          return followUp || standaloneSimple(raw, routeFor, event);
        },
      });
      if (stagedStandby && (!request ||
          request.kind === 'captain' && !['role', 'brief', 'decisions', 'handoff', 'help', 'advance-project'].includes(request.action))) {
        return { kind: 'standby' };
      }
      if (request?.kind !== 'simple') {
        chat.clear();
        return request;
      }
      return { ...request, followUp };
    },
    acknowledge: (owner, ctx, signal) => acknowledge(api, owner, liveConfig, signal, ctx, brokered),
    projectDelivery: !stagedStandby && ['nucbox', 'mac-mini'].includes(localFleetNode())
      ? projectTelegramDelivery(state, liveConfig, brokered) : undefined,
    registry,
    warn: (line) => api.logger.warn(line),
  });
  // The existing inbound safety hook runs at 250. Guarded operations retain
  // their handlers because control requests pass through to them.
  api.on('before_dispatch', dispatch, { priority: 240, timeoutMs: 27_000 });
}

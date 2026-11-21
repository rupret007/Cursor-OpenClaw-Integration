const textLimit = 1800;
const device = /\b(?:bobserver|mac[ -]?mini|nuc[ -]?box|mac[ -]?book(?: pro)?)\b/;
const readQuestion = /^(?:(?:please|first officer)[ ,]+)?(?:what|which|is|are|how|any|show|check|list|give|tell|do i|can you)\b/;
// Preserve the read-only route with common vowel and repeated-letter misspellings.
const textSummaryRequest = /^(?:(?:can|could) you )?sum{1,3}[ae]ri[sz]e (?:my )?(?:recent )?(?:texts?|text messages|imessages?|sms)$/;

export const nodeIds = ['bobserver', 'mac-mini', 'nucbox', 'macbook-pro'];

export function captainRequest(raw) {
  const normalized = raw.trim().toLowerCase().replace(/[?.!]+$/, '').replace(/ +/g, ' ');
  const daily = { 'brief me': 'brief', 'what needs me': 'decisions', handoff: 'handoff', 'captain status': 'role' };
  if (Object.hasOwn(daily, normalized)) return { kind: 'captain', action: daily[normalized] };
  const task = /^captain this:\s*(?:verify|review|check) (?:the )?ci for (?:integration )?pr #?([1-9]\d{0,7})$/.exec(normalized);
  if (task) return { kind: 'captain', action: 'task', pr: Number(task[1]) };
  const grant = /^captain grant integration-ci epoch ([1-9]\d{0,12})$/.exec(normalized);
  if (grant) return { kind: 'captain', action: 'grant', epoch: Number(grant[1]) };
  const control = /^captain (pause|handback) epoch ([1-9]\d{0,12})$/.exec(normalized);
  if (control) return { kind: 'captain', action: control[1], epoch: Number(control[2]) };
  if (/^captain\b/.test(normalized)) return { kind: 'captain', action: 'help' };
}

function memoryRequest(raw, ask) {
  const remembering = /^remember\b/.test(ask);
  const prefix = /^(?:please\s+)?(?:remember|what did i tell you about|what do you remember about|do you remember about|recall)\s+/i;
  if (!prefix.test(raw)) return { kind: remembering ? 'remember' : 'recall' };
  const args = raw.replace(prefix, '');
  const topic = (value) => /^[a-z][a-z0-9_ -]{0,62}$/i.test(value ?? '')
    ? value.toLowerCase().trim().replace(/ +/g, '-') : undefined;
  if (!remembering) return { kind: 'recall', args: topic(args.replace(/[?.!]+$/, '')) };
  const fact = /^(?:that\s+)?(?:my\s+)?([a-z][a-z0-9_ -]{0,62}?)\s+(?:is|are)\s+(\S[^\r\n]*)$/i.exec(args);
  const named = /^([a-z][a-z0-9_ -]{0,62})\s*:\s*(\S[^\r\n]*)$/i.exec(args);
  const pair = named ?? fact;
  if (pair) return { kind: 'remember', args: topic(pair[1]) + ': ' + pair[2] };
  const simpleTopic = topic(args);
  return { kind: 'remember', args: simpleTopic ? simpleTopic + ': ' + args : undefined };
}

export function classifyRequest(input, { legacyStatus, legacyRequest, isControl, isSimple }) {
  if (typeof input !== 'string' || !input.trim()) return;
  const raw = input.trim();
  const text = raw.slice(0, textLimit).toLowerCase().replace(/[’‘]/g, "'").replace(/\s+/g, ' ');
  const ask = text.replace(/[?.!]+$/, '').replace(/^please +/, '');
  const captain = captainRequest(raw);
  if (captain) return captain;
  if (/^(?:remember|what did i tell you about|(?:what do you|do you) remember about|recall)\b/.test(ask)) {
    return memoryRequest(raw, ask);
  }
  if (isControl(raw) || /^local ci status$/.test(ask)) return;
  if (textSummaryRequest.test(ask)) {
    return { kind: 'texts' };
  }
  const legacy = raw.length <= textLimit ? legacyRequest(raw) : undefined;
  if (legacy?.kind === 'github') return { ...legacy, kind: 'github-pr' };
  if (legacy?.kind === 'draft' || legacy?.kind === 'draft-help') return;

  const question = readQuestion.test(text);
  const github = /\b(?:github|prs?|pull requests?|ci|checks)\b/;
  const openPrs = /\b(?:open (?:prs|pull requests)|(?:prs|pull requests) (?:are )?open|my (?:prs|pull requests))\b/;
  if (openPrs.test(ask) && (question || /^(?:(?:all|my|github) )?open (?:prs|pull requests)\b|^my (?:prs|pull requests)\b/.test(ask))) {
    return { kind: 'github-open' };
  }
  if (question && /\bquotas?\b|\b(?:codex|claude|minimax|gemini) (?:usage|limits)\b/.test(text) ||
      legacy?.kind === 'quota' || /^(?:quotas?|(?:current )?quota status|usage limits)$/.test(ask)) {
    return { kind: 'quota' };
  }

  const subject = raw.length <= textLimit ? legacyStatus(raw) : undefined;
  if (subject === 'all') return { kind: 'overview' };
  if (legacy?.kind === 'gate' || subject === 'gates' ||
      /\b(?:gates?|fullgate)\b/.test(ask) && /\b(?:status|running|current|active)\b/.test(ask) && !/\bdemo\b/.test(ask)) {
    return { kind: 'gate' };
  }
  if (nodeIds.includes(subject)) return { kind: 'fleet', node: subject };
  if (subject === 'load') return { kind: 'fleet', node: 'bobserver' };
  if (legacy?.kind === 'running' || ['cluster', 'lanes'].includes(subject) ||
      /^(?:what(?:'s| is) running|fleet status|fleet load|cluster load|current load)\b/.test(ask) ||
      question && /\b(?:machines?|nodes?|fleet|cluster|load average|cpu load)\b/.test(ask) &&
        /\b(?:load|running|status|busy|health)\b/.test(ask)) {
    return { kind: 'fleet' };
  }
  if (question && device.test(ask) && /\b(?:load|running|busy|status)\b/.test(ask)) return { kind: 'fleet' };
  if (github.test(ask) && /\b(?:current|latest|running|green|passing|failed|failing|status|checks|builds|right now)\b/.test(ask)) {
    return { kind: 'github-help' };
  }
  if (legacy || subject || raw.length > textLimit) return;
  if (/\b(?:gates?|fullgate|fleet|cluster load|quotas?|github|load averages?)\b/.test(ask) &&
      /\b(?:current|latest|status|running|remaining|live|today|update|right now)\b/.test(ask)) {
    return { kind: 'status-help' };
  }
  if (isSimple(raw)) return { kind: 'simple', prompt: raw };
}

export const budgets = Object.freeze({ acknowledgment: 1500, simple: 11_000, status: 18_000 });

export async function bounded(ms, work) {
  const controller = new AbortController();
  let timer;
  try {
    return await Promise.race([
      new Promise((_, reject) => {
        timer = setTimeout(() => {
          const error = new Error('First Officer step exceeded its deadline');
          controller.abort(error);
          reject(error);
        }, ms);
      }),
      Promise.resolve().then(() => work(controller.signal)),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

export function createDispatch({ authorize, classify, acknowledge, registry, warn, timing = budgets }) {
  return async (event, ctx) => {
    const owner = authorize(event, ctx);
    if (!owner || typeof event.content !== 'string' || !event.content.trim()) return;
    const request = classify(event.content, event, ctx);
    // Ack and work start together. Only the ack deadline can delay a fast result.
    const ack = bounded(timing.acknowledgment, (signal) => acknowledge(owner, ctx, signal))
      .catch(() => warn('First Officer Telegram acknowledgment failed or timed out.'));
    if (!request) {
      void ack;
      return;
    }

    const entry = registry[request.kind];
    if (!entry) {
      await ack;
      return { handled: true, text: 'The First Officer tool for this request is unavailable.' };
    }
    const deadline = request.kind === 'simple' ? timing.simple : timing.status;
    const final = await bounded(deadline, (signal) => entry.run(request, { owner, ctx, signal }))
      .catch(() => {
        warn('First Officer request failed or timed out (' + request.kind + ').');
        return { text: entry.failure ?? 'The live First Officer request failed.' };
      });
    await ack;
    return { handled: true, ...final };
  };
}

export const fastProvider = 'litellm';
export const fastModel = 'minimax-m3.1-flash';
export const fastModelRef = fastProvider + '/' + fastModel;

const quickPrompt = 'Answer the current standalone question in plain language and at most three short sentences. ' +
  'You have no tools or current operational evidence. Say when an answer would require a live source. ' +
  'Use Central Time for time references and omit credentials, private contacts, addresses, and private message excerpts.';

function plainChat(text, routeFor, event) {
  return !event.replyToId && !event.replyToBody && !event.media?.length &&
    !event.mediaStagingPending && !/[\r\n]/.test(text) && routeFor(text).reason === 'short-chat';
}

export function standaloneSimple(text, routeFor, event) {
  return text.length <= 1200 && plainChat(text, routeFor, event) && (
      /^(?:hi|hello|hey|good (?:morning|afternoon|evening)|thanks|thank you)[!.]*$/i.test(text) ||
      /^(?:(?:hi|hello|hey)[,!]?[ \t]+)?(?:anyone|are you) there[?!.]*$/i.test(text) ||
      /^(?:define|explain|translate|convert|tell me (?:a joke|a fun fact|about))\s+\S/i.test(text) ||
      /^(?:what (?:is|are|does|causes)|how (?:does|do)|why (?:does|do))\s+(?!(?:it|that|this|these|they|we|i|you|he|she)\b)\S/i.test(text)
    );
}

function configuredFastModel(config) {
  const allowed = config?.plugins?.entries?.['first-officer-conductor']?.llm?.allowedCompletionModels;
  if (!Array.isArray(allowed) || allowed.length !== 1) return fastModelRef;
  const [reference] = allowed;
  if (typeof reference !== 'string' || !/^litellm\/[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$/.test(reference)) {
    return fastModelRef;
  }
  const models = config?.models?.providers?.[fastProvider]?.models;
  if (!Array.isArray(models) || !models.some((model) => reference === fastProvider + '/' + model?.id)) return fastModelRef;
  return reference;
}

export function isLocalQuickChat(config) {
  const model = 'litellm/qwen-local-fast';
  const url = config?.models?.providers?.litellm?.baseUrl;
  return configuredFastModel(config) === model && config?.agents?.defaults?.model?.primary === model &&
    typeof url === 'string' && /^http:\/\/(?:localhost|127\.0\.0\.1|\[::1\]):[1-9]\d{0,4}\/v1\/?$/.test(url);
}

const conversationBytes = 650;
const conversationAgeMs = 5 * 60_000;
const noContext = 'I don’t have enough recent chat context for that follow-up. Please name the topic again.';

export function quickFollowUp(text, routeFor, event) {
  if (Buffer.byteLength(text) > conversationBytes || !plainChat(text, routeFor, event)) return false;
  const ask = text.replace(/[?.!]+$/, '').replace(/^(?:(?:and|now|then|please|could you|can you)[, ]+)+/i, '');
  // An explicit example or term after the colon supplies its own context.
  if (/^(?:define|explain|translate|convert) (?:this|that) (?:[a-z-]+ )*(?:example|term|sentence|phrase|word|text|passage|concept)\b[^:]*:\s*\S/i.test(ask)) return false;
  return /^(?:why|how so|(?:give|show)(?: me)? (?:an|another) example|(?:what about|tell me more about) (?:it|that|them|this)|tell me more)$/i.test(ask) ||
    /^(?:explain|compare|summarize|rewrite|rephrase|expand on|make |how |what |which |why |is |are |can |does |do )\b.*\b(?:it|that|this|them|those|the (?:first|second|last|previous|same) (?:one|example|answer|part))\b/i.test(ask);
}

export function createQuickChat(runtime, redact, now = () => performance.now()) {
  let recent;
  return {
    clear() { recent = undefined; },
    async answer({ prompt, followUp }, { owner, signal }, config) {
      if (!isLocalQuickChat(config)) {
        recent = undefined;
        if (followUp) return noContext;
        return redact(await answerSimple(runtime, prompt, signal, config), 1250);
      }
      const previous = recent?.owner === owner && recent.expiresAt > now() ? recent.exchanges : [];
      const turn = { owner, exchanges: [], expiresAt: 0 };
      recent = turn;
      const cleanPrompt = redact(prompt, 1250);
      const context = followUp ? [...previous] : [];
      let size = Buffer.byteLength(cleanPrompt);
      for (const exchange of context) size += Buffer.byteLength(exchange.prompt) + Buffer.byteLength(exchange.answer);
      while (size > conversationBytes && context.length) {
        const dropped = context.shift();
        size -= Buffer.byteLength(dropped.prompt) + Buffer.byteLength(dropped.answer);
      }
      if (followUp && (!context.length || !cleanPrompt)) return noContext;
      const answer = redact(await answerSimple(runtime, followUp ? cleanPrompt : prompt, signal, config, context), 1250);
      if (recent === turn && cleanPrompt && answer && Buffer.byteLength(cleanPrompt + answer) < conversationBytes) {
        turn.exchanges = [...context.slice(-1), { prompt: cleanPrompt, answer }];
        turn.expiresAt = now() + conversationAgeMs;
      }
      return answer;
    },
  };
}

export function routeOwnerModel(event, config, routeFor) {
  const route = routeFor(event.prompt, event.attachments);
  if (route.reason === 'short-chat') return { ...route, model: configuredFastModel(config).slice(fastProvider.length + 1), fallbacks: [] };
  const prefix = fastProvider + '/';
  const primary = prefix + route.model;
  const aliases = new Set((config?.models?.providers?.[fastProvider]?.models ?? []).map((model) => model.id));
  const configured = config?.agents?.defaults?.model?.fallbacks ?? [];
  const preferred = configured.filter((id) => typeof id === 'string' && id.startsWith(prefix + 'gemini-'));
  const fallbacks = [...new Set([...preferred, ...route.fallbacks, ...configured])].filter((id) =>
    typeof id === 'string' && id !== primary && id.startsWith(prefix) && aliases.has(id.slice(prefix.length)));
  return { ...route, fallbacks };
}

export async function answerSimple(runtime, prompt, signal, config, history = []) {
  signal.throwIfAborted();
  if (typeof runtime.llm?.complete !== 'function') throw new Error('Gateway completion API unavailable');
  const messages = history.flatMap(({ prompt, answer }) => [
    { role: 'user', content: prompt }, { role: 'assistant', content: answer },
  ]);
  messages.push({ role: 'user', content: prompt });
  const result = await runtime.llm.complete({
    model: configuredFastModel(config), purpose: 'first-officer.quick-answer',
    systemPrompt: history.length ? 'Use the recent exchange to resolve references in the last question. ' +
      quickPrompt.replace('standalone ', '') : quickPrompt,
    messages, maxTokens: history.length ? 160 : 384, temperature: 0.2, signal,
  });
  signal.throwIfAborted();
  if (!result || ['error', 'aborted', 'length'].includes(result.stopReason) || typeof result.text !== 'string' ||
      !result.text.trim() || result.text.length > 3200) throw new Error('Fast model did not complete');
  return result.text.trim();
}

export const fastProvider = 'litellm';
export const fastModel = 'minimax-m3.1-flash';
export const fastModelRef = fastProvider + '/' + fastModel;

const quickPrompt = 'Answer the current standalone question in plain language and at most three short sentences. ' +
  'You have no tools or current operational evidence. Say when an answer would require a live source. ' +
  'Use Central Time for time references and omit credentials, private contacts, addresses, and private message excerpts.';

export function standaloneSimple(text, routeFor, event) {
  return text.length <= 1200 && !event.replyToId && !event.replyToBody && !event.media?.length &&
    !event.mediaStagingPending && !/[\r\n]/.test(text) &&
    routeFor(text).reason === 'short-chat' && (
      /^(?:hi|hello|hey|good (?:morning|afternoon|evening)|thanks|thank you)[!.]*$/i.test(text) ||
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

export async function answerSimple(runtime, prompt, signal, config) {
  signal.throwIfAborted();
  if (typeof runtime.llm?.complete !== 'function') throw new Error('Gateway completion API unavailable');
  const result = await runtime.llm.complete({
    model: configuredFastModel(config), purpose: 'first-officer.quick-answer',
    systemPrompt: quickPrompt, messages: [{ role: 'user', content: prompt }],
    maxTokens: 384, temperature: 0.2, signal,
  });
  signal.throwIfAborted();
  if (!result || ['error', 'aborted', 'length'].includes(result.stopReason) || typeof result.text !== 'string' ||
      !result.text.trim() || result.text.length > 3200) throw new Error('Fast model did not complete');
  return result.text.trim();
}

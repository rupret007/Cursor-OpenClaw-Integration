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

export function routeOwnerModel(event, config, routeFor) {
  const route = routeFor(event.prompt, event.attachments);
  if (route.reason === 'short-chat') return { ...route, model: fastModel, fallbacks: [] };
  const prefix = fastProvider + '/';
  const primary = prefix + route.model;
  const aliases = new Set((config?.models?.providers?.[fastProvider]?.models ?? []).map((model) => model.id));
  const configured = config?.agents?.defaults?.model?.fallbacks ?? [];
  const preferred = configured.filter((id) => typeof id === 'string' && id.startsWith(prefix + 'gemini-'));
  const fallbacks = [...new Set([...preferred, ...route.fallbacks, ...configured])].filter((id) =>
    typeof id === 'string' && id !== primary && id.startsWith(prefix) && aliases.has(id.slice(prefix.length)));
  return { ...route, fallbacks };
}

export async function answerSimple(runtime, prompt, signal) {
  signal.throwIfAborted();
  if (typeof runtime.llm?.complete !== 'function') throw new Error('Gateway completion API unavailable');
  const result = await runtime.llm.complete({
    model: fastModelRef, purpose: 'first-officer.quick-answer',
    systemPrompt: quickPrompt, messages: [{ role: 'user', content: prompt }],
    maxTokens: 384, temperature: 0.2, signal,
  });
  signal.throwIfAborted();
  if (!result || ['error', 'aborted', 'length'].includes(result.stopReason) || typeof result.text !== 'string' ||
      !result.text.trim() || result.text.length > 3200) throw new Error('Fast model did not complete');
  return result.text.trim();
}

import { execFile } from 'node:child_process';
import { readFile, stat } from 'node:fs/promises';
import { isIP } from 'node:net';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { promisify } from 'node:util';
import { andreaCommand, parseAndreaSummary } from './andrea-ssh.mjs';
import { describeAndreaActivity, formatAndreaSummary, inspectAndrea } from './tools.mjs';

const execute = promisify(execFile);
const maxBytes = 196_608;
const chatId = /^bb:[^\s\x00-\x1f\x7f]{1,220}$/u;
const directGuid = /^(?:iMessage|SMS|RCS);-;[^;\s\x00-\x1f\x7f]{1,190}$/i;
const groupGuid = /^(?:iMessage|SMS|RCS);\+;[^;\s\x00-\x1f\x7f]{1,190}$/i;
const messageLimit = 20;

function selfThreads(settings) {
  return new Set([settings.BLUEBUBBLES_CANONICAL_SELF_THREAD_JID,
    ...(settings.BLUEBUBBLES_SELF_THREAD_ALIAS_JIDS ?? '').split(',')]
    .map((id) => id?.trim()).filter(Boolean));
}

function directHandle(guid) {
  const handle = guid.split(';')[2].toLowerCase();
  if (!/^\+?[0-9().-]+$/.test(handle)) return handle;
  const digits = handle.replace(/\D/g, '');
  return digits.length === 11 && digits.startsWith('1') ? digits.slice(1) : digits;
}

class ReaderError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

async function loadSettings() {
  const plist = join(homedir(), 'Library/LaunchAgents/com.nanoclaw.mac-mini.plist');
  const { stdout } = await execute('/usr/libexec/PlistBuddy', ['-c', 'Print :WorkingDirectory', plist], {
    timeout: 2000, maxBuffer: 4096,
  });
  const file = join(stdout.trim(), '.env');
  const info = await stat(file);
  if (!info.isFile() || info.uid !== process.getuid() || info.size > 256 * 1024) {
    throw new Error('Andrea settings file is unavailable');
  }
  const settings = Object.create(null);
  for (const line of (await readFile(file, 'utf8')).split('\n')) {
    const match = /^\s*(BLUEBUBBLES_[A-Z0-9_]+)\s*=\s*(.*?)\s*$/.exec(line);
    if (!match) continue;
    const [, name, raw] = match;
    settings[name] = raw.replace(/^(['"])(.*)\1$/, '$2');
  }
  return settings;
}

const loopback = (url) => url.hostname === 'localhost' ||
  (isIP(url.hostname) === 4 && url.hostname.startsWith('127.'));

async function getJson(url, transport, options) {
  let response;
  try {
    response = await transport(url, { method: 'GET', redirect: 'error', ...options });
  } catch {
    throw new ReaderError('api-unreachable');
  }
  if (response.status === 401 || response.status === 403) throw new ReaderError('api-authentication');
  if (!response.ok || !response.body) throw new ReaderError('api-response');
  const reader = response.body.getReader();
  const parts = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maxBytes) throw new ReaderError('api-response');
      parts.push(Buffer.from(value));
    }
    return JSON.parse(Buffer.concat(parts).toString('utf8'));
  } finally {
    await reader.cancel();
  }
}

export function createMacAndreaReader(settings, transport = fetch, counts) {
  const token = settings.BLUEBUBBLES_CONTROL_TOKEN;
  const owner = settings.BLUEBUBBLES_CANONICAL_SELF_THREAD_JID;
  const excluded = selfThreads(settings);
  const base = new URL(settings.BLUEBUBBLES_CONTROL_BASE_URL ||
    'http://localhost:' + (settings.BLUEBUBBLES_CONTROL_PORT || '4315'));
  if (base.protocol !== 'http:' || !loopback(base) ||
      !base.port || base.username || base.password || base.pathname !== '/' || base.search || base.hash ||
      typeof token !== 'string' || !/^[!-~]{16,1024}$/.test(token) || !chatId.test(owner ?? '')) {
    throw new Error('Andrea loopback reader settings are invalid');
  }

  function get(path) {
    return getJson(new URL(path, base), transport, {
      signal: AbortSignal.timeout(4000), headers: { Authorization: 'Bearer ' + token },
    });
  }

  return {
    status: () => get('/v1/bluebubbles/status'),
    async chats() {
      const { chats } = await get('/v1/bluebubbles/chats?limit=100');
      if (!Array.isArray(chats) || chats.length > 100) throw new Error('Andrea chat list was invalid');
      if (counts) {
        counts.listedChats = chats.length;
        counts.groupChats = chats.filter((row) => row?.isGroup === true).length;
        counts.excludedChats = chats.filter((row) => excluded.has(row?.chatJid)).length;
      }
      return { complete: chats.length < 100, chats: chats.filter((row) =>
        row?.isGroup === false && typeof row.chatJid === 'string' &&
        chatId.test(row.chatJid) && !excluded.has(row.chatJid)) };
    },
    async messages(id) {
      if (typeof id !== 'string' || !chatId.test(id) || excluded.has(id)) throw new Error('Andrea chat was excluded');
      const { messages } = await get('/v1/bluebubbles/chats/' + encodeURIComponent(id) + '/messages?limit=20');
      if (!Array.isArray(messages) || messages.length > 20) throw new Error('Andrea message list was invalid');
      if (counts) {
        counts.returnedRows += messages.length;
        counts.botRows += messages.filter((row) => row?.isBotMessage === true).length;
        counts.rowsWithValidTimestamp += messages.filter((row) => Number.isFinite(Date.parse(row?.timestamp))).length;
      }
      return messages.filter((row) => row?.chatJid === id && row.isBotMessage === false &&
        typeof row.isFromMe === 'boolean' && typeof row.preview === 'string');
    },
  };
}

function providerPlan(settings) {
  const excluded = selfThreads(settings);
  const selfHandles = new Set([...excluded]
    .filter((id) => id.startsWith('bb:') && directGuid.test(id.slice(3)))
    .map((id) => directHandle(id.slice(3))));
  const allowed = new Set([settings.BLUEBUBBLES_ALLOWED_CHAT_GUIDS, settings.BLUEBUBBLES_ALLOWED_CHAT_GUID]
    .filter(Boolean).join(',').split(',').map((value) => value.trim())
    .filter((guid) => !excluded.has('bb:' + guid) &&
      (groupGuid.test(guid) || (directGuid.test(guid) && !selfHandles.has(directHandle(guid))))));
  const password = settings.BLUEBUBBLES_PASSWORD;
  const bases = [settings.BLUEBUBBLES_BASE_URL, ...(settings.BLUEBUBBLES_BASE_URL_CANDIDATES ?? '').split(',')]
    .filter((value) => URL.canParse(value ?? '')).map((value) => new URL(value))
    .filter((url) => (url.protocol === 'https:' || (url.protocol === 'http:' && loopback(url))) &&
      !url.username && !url.password && url.pathname === '/' && !url.search && !url.hash);
  if (!bases.length || typeof password !== 'string' || !/^[!-~]{1,1024}$/.test(password)) return;
  return { allowed, password, bases };
}

function providerRows(body, guid) {
  const rows = body?.data ?? body?.messages;
  if (!Array.isArray(rows) || rows.length > messageLimit) throw new ReaderError('api-response');
  const seen = new Set();
  const grouped = groupGuid.test(guid);
  return rows.flatMap((row) => {
    const preview = [row?.text, row?.body, row?.message].find((value) => typeof value === 'string' && value.trim());
    const id = row?.guid ?? row?.messageGuid ?? row?.id;
    const time = row?.dateCreated ?? row?.date ?? row?.dateCreatedEpoch;
    const ms = typeof time === 'number' ? (time > 10_000_000_000 ? time : 1000 * time) :
      typeof time === 'string' ? Date.parse(time) : NaN;
    if (!preview || typeof id !== 'string' || !id || seen.has(id) ||
        !Number.isFinite(ms) || Math.abs(ms) > 8_640_000_000_000_000 ||
        typeof row.isFromMe !== 'boolean' || row.isBotMessage === true ||
        row.isSystemMessage === true || row.associatedMessageGuid ||
        (Array.isArray(row.chats) && row.chats.some((chat) =>
          (typeof chat?.isGroup === 'boolean' && chat.isGroup !== grouped) ||
          (chat?.guid ?? chat?.chatGuid ?? guid) !== guid))) return [];
    seen.add(id);
    return [{ timestamp: new Date(ms).toISOString(), preview: preview.slice(0, 220), isFromMe: row.isFromMe }];
  });
}

async function inspectMacHistory(settings, transport, now) {
  const local = createMacAndreaReader(settings, transport);
  const plan = providerPlan(settings);
  const list = local.chats();
  // Overlap the scope proof with the cache reads and retain the same time
  // budget even when four live history reads are needed.
  const status = plan?.allowed.size ? local.status().then((result) => result?.status).catch(() => null) : null;
  const cached = await inspectAndrea({ chats: () => list, messages: local.messages }, now);
  if (cached.counts.recentTextPreviews) return { view: cached, source: 'cache' };
  if (!plan) return { view: cached, source: 'provider-unconfigured' };
  if (!plan.allowed.size) return { view: cached, source: 'no-authorized-chats' };
  const active = await status;
  if (active?.chatScope !== 'allowlist') return { view: cached, source: 'scope-unverified' };
  const declared = active.activeBaseUrl;
  const base = typeof declared === 'string' && URL.canParse(declared)
    ? plan.bases.find((url) => url.href === new URL(declared).href)
    : plan.bases[0];
  if (!base) return { view: cached, source: 'provider-unconfigured' };
  const indexed = new Map((await list).chats.map((chat) => [chat.chatJid, chat]));
  // Exact direct GUIDs from the existing allowlist are sufficient for a
  // point read. The bounded cache list can omit a cold or newly synced chat.
  const direct = [...plan.allowed].filter((guid) => directGuid.test(guid));
  const grouped = direct.length === 0;
  const candidates = (grouped ? [...plan.allowed] : direct).map((guid) => {
    const chatJid = 'bb:' + guid;
    return indexed.get(chatJid) ?? { chatJid };
  });
  const view = await inspectAndrea({
    chats: async () => ({ chats: candidates, complete: true }),
    async messages(id) {
      const guid = id.slice(3);
      if (!plan.allowed.has(guid)) throw new ReaderError('settings-invalid');
      const url = new URL('/api/v1/chat/' + encodeURIComponent(guid) + '/message', base);
      for (const key of ['guid', 'password', 'token']) url.searchParams.set(key, plan.password);
      url.searchParams.set('limit', String(messageLimit));
      url.searchParams.set('offset', '0');
      url.searchParams.set('sort', 'DESC');
      return providerRows(await getJson(url, transport, { signal: AbortSignal.timeout(3000) }), guid);
    },
  }, now);
  return { view, source: 'live-provider', grouped, cached };
}

export async function runMacAndrea(command, readSettings = loadSettings, transport = fetch, now = Date.now()) {
  if (command !== andreaCommand) throw new Error('Only the Andrea summary operation is allowed');
  const { view, source, grouped, cached } = await inspectMacHistory(await readSettings(), transport, now);
  // The previous gateway can still read the one-to-one result during a
  // staged upgrade. The updated client validates the group summary separately.
  const result = grouped ? { version: 1, summary: formatAndreaSummary(cached, { allowEmpty: true }), source,
    groupSummary: formatAndreaSummary(view, { grouped: true }) } :
    { version: 1, summary: formatAndreaSummary(view), source };
  if (view.counts.recentTextPreviews) result.activity = describeAndreaActivity(view.groups);
  return result;
}

export async function readMacAndrea(signal, read = runMacAndrea) {
  signal.throwIfAborted();
  const result = await read(andreaCommand, undefined, (url, options) =>
    fetch(url, { ...options, signal: AbortSignal.any([signal, options.signal]) }));
  signal.throwIfAborted();
  return parseAndreaSummary(JSON.stringify(result));
}

async function diagnosticRead(readSettings, transport, read, counts) {
  let settings;
  try {
    settings = await readSettings();
  } catch {
    throw new ReaderError('settings-unavailable');
  }
  let reader;
  try {
    reader = createMacAndreaReader(settings, transport, counts);
  } catch {
    throw new ReaderError('settings-invalid');
  }
  try {
    return await read(reader);
  } catch (error) {
    if (error instanceof ReaderError) throw error;
    throw new ReaderError('api-response');
  }
}

export async function diagnoseMacAndrea(readSettings = loadSettings, transport = fetch, now = Date.now()) {
  const source = { listedChats: 0, groupChats: 0, excludedChats: 0, returnedRows: 0,
    botRows: 0, rowsWithValidTimestamp: 0 };
  return diagnosticRead(readSettings, transport, async (reader) => {
    const { counts } = await inspectAndrea(reader, now);
    return { ...source, ...counts };
  }, source);
}

export async function diagnoseMacAndreaSync(readSettings = loadSettings, transport = fetch, now = Date.now()) {
  const select = (value, allowed) => allowed.includes(value) ? value : 'unknown';
  const age = (value, limit) => {
    const elapsed = now - Date.parse(value ?? '');
    return !Number.isFinite(elapsed) || elapsed < -30_000 ? 'unknown' : elapsed <= limit ? 'recent' : 'older';
  };
  return diagnosticRead(readSettings, transport, async (reader) => {
    const { status } = await reader.status();
    if (!status || typeof status !== 'object' || Array.isArray(status)) throw new ReaderError('api-response');
    return {
      channel: status.connected === true ? 'connected' : status.connected === false ? 'disconnected' : 'unknown',
      scope: select(status.chatScope, ['allowlist', 'contacts_only', 'all_synced']),
      transport: select(status.transportState, ['not_checked', 'reachable', 'auth_failed', 'unreachable']),
      webhook: select(status.webhookRegistrationState, ['not_configured', 'registered', 'missing', 'auth_failed', 'unreachable']),
      shadowPoll: age(status.shadowPollLastOkAt, 60 * 60_000),
      lastInbound: age(status.lastInboundAt, 48 * 60 * 60_000),
    };
  });
}

async function diagnoseLiveMacAndrea() {
  const { view, source } = await inspectMacHistory(await loadSettings(), fetch, Date.now());
  return { source, selectedChats: view.counts.selectedChats, readFailures: view.failures,
    recentTextPreviews: view.counts.recentTextPreviews };
}

async function main() {
  const option = process.argv[2];
  const localDiagnostic = process.env.SSH_ORIGINAL_COMMAND === undefined &&
    process.argv.length === 3 && ['--diagnose', '--diagnose-sync', '--diagnose-live'].includes(option);
  try {
    const diagnostic = { '--diagnose': diagnoseMacAndrea, '--diagnose-sync': diagnoseMacAndreaSync,
      '--diagnose-live': diagnoseLiveMacAndrea };
    const result = localDiagnostic ? await diagnostic[option]() : await runMacAndrea(process.env.SSH_ORIGINAL_COMMAND);
    console.log(JSON.stringify(result));
  } catch (error) {
    if (localDiagnostic) {
      console.log(JSON.stringify({ problem: error instanceof ReaderError ? error.code : 'node-diagnostic' }));
    } else {
      console.error('Andrea read-only summary failed.');
    }
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  void main();
}

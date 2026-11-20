import { execFile } from 'node:child_process';
import { readFile, stat } from 'node:fs/promises';
import { isIP } from 'node:net';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { promisify } from 'node:util';
import { andreaCommand } from './andrea-ssh.mjs';
import { inspectAndrea, summarizeAndrea } from './tools.mjs';

const execute = promisify(execFile);
const maxBytes = 196_608;
const chatId = /^bb:[^\s\x00-\x1f\x7f]{1,220}$/u;

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

export function createMacAndreaReader(settings, transport = fetch, counts) {
  const token = settings.BLUEBUBBLES_CONTROL_TOKEN;
  const owner = settings.BLUEBUBBLES_CANONICAL_SELF_THREAD_JID;
  const excluded = new Set([owner, ...(settings.BLUEBUBBLES_SELF_THREAD_ALIAS_JIDS ?? '').split(',').map((id) => id.trim())]);
  const base = new URL(settings.BLUEBUBBLES_CONTROL_BASE_URL ||
    'http://localhost:' + (settings.BLUEBUBBLES_CONTROL_PORT || '4315'));
  const ipv4Loopback = isIP(base.hostname) === 4 && base.hostname.startsWith('127.');
  if (base.protocol !== 'http:' || (base.hostname !== 'localhost' && !ipv4Loopback) ||
      !base.port || base.username || base.password || base.pathname !== '/' || base.search || base.hash ||
      typeof token !== 'string' || !/^[!-~]{16,1024}$/.test(token) || !chatId.test(owner ?? '')) {
    throw new Error('Andrea loopback reader settings are invalid');
  }

  async function get(path) {
    let response;
    try {
      response = await transport(new URL(path, base), {
        method: 'GET', redirect: 'error', signal: AbortSignal.timeout(4000),
        headers: { Authorization: 'Bearer ' + token },
      });
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
        if (size > maxBytes) throw new Error('Andrea response exceeded its size limit');
        parts.push(Buffer.from(value));
      }
    } finally {
      await reader.cancel();
    }
    return JSON.parse(Buffer.concat(parts).toString('utf8'));
  }

  return {
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

export async function runMacAndrea(command, readSettings = loadSettings, transport = fetch) {
  if (command !== andreaCommand) throw new Error('Only the Andrea summary operation is allowed');
  return { version: 1, summary: await summarizeAndrea(createMacAndreaReader(await readSettings(), transport)) };
}

export async function diagnoseMacAndrea(readSettings = loadSettings, transport = fetch, now = Date.now()) {
  const source = { listedChats: 0, groupChats: 0, excludedChats: 0, returnedRows: 0,
    botRows: 0, rowsWithValidTimestamp: 0 };
  let settings;
  try {
    settings = await readSettings();
  } catch {
    throw new ReaderError('settings-unavailable');
  }
  let reader;
  try {
    reader = createMacAndreaReader(settings, transport, source);
  } catch {
    throw new ReaderError('settings-invalid');
  }
  try {
    const { counts } = await inspectAndrea(reader, now);
    return { ...source, ...counts };
  } catch (error) {
    if (error instanceof ReaderError) throw error;
    throw new ReaderError('api-response');
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const localDiagnostic = process.env.SSH_ORIGINAL_COMMAND === undefined &&
    process.argv.length === 3 && process.argv[2] === '--diagnose';
  try {
    console.log(JSON.stringify(await (localDiagnostic
      ? diagnoseMacAndrea() : runMacAndrea(process.env.SSH_ORIGINAL_COMMAND))));
  } catch (error) {
    if (localDiagnostic) {
      console.log(JSON.stringify({ problem: error instanceof ReaderError ? error.code : 'node-diagnostic' }));
    } else {
      console.error('Andrea read-only summary failed.');
    }
    process.exitCode = 1;
  }
}

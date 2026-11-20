import { execFile } from 'node:child_process';
import { lstat, readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';

export const andreaCommand = 'first-officer-andrea-summary-v1';
const execute = promisify(execFile);
const title = 'Andrea · synced one-to-one text activity from the last 48 hours.';
const lineFormats = [
  /^\d{1,2} incoming, \d{1,2} outgoing across [1-4] recent conversations\.$/,
  /^The latest message was incoming in [1-4] of those conversations\.$/,
  /^Topics detected in previews · (?:scheduling|travel or pickup|payments or orders|follow-up)(?:, (?:scheduling|travel or pickup|payments or orders|follow-up))*\.$/,
  /^Andrea returned no recent previews in the chats checked\.$/,
  /^Andrea could not read [1-4] of the selected conversations\.$/,
  /^Checked the four most recent conversations\.$/,
  /^Andrea returned a partial chat list\.$/,
];

export function parseAndreaSummary(payload) {
  const { version, summary } = JSON.parse(payload) ?? {};
  const lines = typeof summary === 'string' && summary.length <= 1000 ? summary.split('\n') : [];
  if (version !== 1 || lines.shift() !== title || lines.length < 1 ||
      lines.length > lineFormats.length || lines.some((line) => !lineFormats.some((format) => format.test(line)))) {
    throw new Error('Andrea summary response was invalid');
  }
  return summary;
}

export async function sshAndreaSummary(signal, {
  directory = join(homedir(), '.ssh/openclaw-fo-andrea'),
  run = execute,
} = {}) {
  const folder = await lstat(directory).catch((error) => {
    if (error.code !== 'ENOENT') throw error;
  });
  if (!folder) return;
  const uid = process.getuid();
  if (!folder.isDirectory() || folder.uid !== uid || folder.mode & 0o077) {
    throw new Error('Andrea SSH directory must be private');
  }
  const identity = join(directory, 'id_ed25519');
  const pins = join(directory, 'known_hosts');
  const route = join(directory, 'route.json');
  for (const path of [identity, pins, route]) {
    const stat = await lstat(path);
    if (!stat.isFile() || stat.uid !== uid || stat.nlink !== 1 || stat.mode & 0o077 || stat.size > 8192) {
      throw new Error('Andrea SSH route files must be private');
    }
  }
  const { host, user } = JSON.parse(await readFile(route, 'utf8'));
  if (typeof host !== 'string' || !/^[a-z0-9][a-z0-9.-]{0,126}$/i.test(host) ||
      typeof user !== 'string' || !/^[a-z_][a-z0-9_-]{0,59}$/i.test(user)) throw new Error('Invalid Andrea SSH route');
  const { stdout } = await run('/usr/bin/nice', [
    '-n', '10', '/usr/bin/ssh', '-F', '/dev/null', '-nT', '-oBatchMode=yes',
    '-oConnectTimeout=4', '-oConnectionAttempts=1', '-oStrictHostKeyChecking=yes', '-oUpdateHostKeys=no',
    '-oGlobalKnownHostsFile=/dev/null', '-oUserKnownHostsFile=' + pins, '-oHostKeyAlgorithms=ssh-ed25519',
    '-oIdentityAgent=none', '-oIdentitiesOnly=yes', '-oClearAllForwardings=yes',
    '-i', identity, '-l', user, host, andreaCommand,
  ], { timeout: 14_000, maxBuffer: 8192, killSignal: 'SIGKILL', signal });
  return parseAndreaSummary(stdout);
}

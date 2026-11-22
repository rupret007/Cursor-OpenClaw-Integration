import { execFile } from 'node:child_process';
import { lstat, readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';

export const andreaCommand = 'first-officer-andrea-summary-v1';
const execute = promisify(execFile);
const title = 'Andrea · synced one-to-one text activity from the last 48 hours.';
const groupTitle = 'Andrea · activity in approved group chats from the last 48 hours.';
const countLine = /^\d{1,2} incoming, \d{1,2} outgoing across ([1-4]) recent conversations\.$/;
const lineFormats = [
  countLine,
  /^The latest message was incoming in [1-4] of those conversations\.$/,
  /^Topics detected in previews · (?:scheduling|travel or pickup|payments or orders|follow-up)(?:, (?:scheduling|travel or pickup|payments or orders|follow-up))*\.$/,
  /^Andrea returned no recent previews in the chats checked\.$/,
  /^Andrea could not read [1-4] of the selected conversations\.$/,
  /^Checked the four most recent conversations\.$/,
  /^Andrea returned a partial chat list\.$/,
];

const activityTopics = new Map([
  ['scheduling', 'scheduling'], ['travel', 'travel'], ['logistics', 'pickups and logistics'],
  ['work', 'work'], ['events', 'events'], ['family', 'family'], ['payments', 'payments'],
  ['orders', 'orders and deliveries'], ['food', 'food'], ['shared-material', 'shared files or links'],
  ['follow-up', 'follow-ups'],
]);
const activityDevelopments = new Map([
  ['question', 'questions'], ['request', 'action requests'], ['plan-confirmed', 'plan confirmations'],
  ['plan-changed', 'changes of plans'], ['update', 'updates'], ['shared-item', 'shared items'],
]);
const latestNotes = new Map([
  ['ask-time', 'asks about timing'], ['ask-location', 'asks about a location'],
  ['ask-confirmation', 'asks for confirmation'], ['ask-question', 'asks a question'],
  ['request-action', 'asks someone to do something'], ['plan-confirmed', 'confirms a plan'],
  ['plan-changed', 'raises a change of plans'], ['share-update', 'shares an update'],
  ['share-item', 'shares a link or file'], ['acknowledgment', 'is a brief acknowledgment'],
  ['other', null],
]);

const followUpLabels = new Map([
  ['ask-time', 'timing questions'], ['ask-location', 'location questions'],
  ['ask-confirmation', 'confirmation requests'], ['ask-question', 'questions'], ['request-action', 'action requests'],
]);

function formatFollowUps(items, conversationsRead) {
  if (!Array.isArray(items) || items.length > 4 ||
      !items.every((item) => exactKeys(item, ['intent', 'conversations']) && followUpLabels.has(item.intent) &&
        Number.isInteger(item.conversations) && item.conversations >= 1 && item.conversations <= 4) ||
      new Set(items.map((item) => item.intent)).size !== items.length ||
      items.reduce((total, item) => total + item.conversations, 0) > conversationsRead) return '';
  return items.map(({ intent, conversations }) => followUpLabels.get(intent) + ' in ' +
    conversations + (conversations === 1 ? ' conversation' : ' conversations')).join(', ');
}

function exactKeys(value, keys) {
  return value && typeof value === 'object' && !Array.isArray(value) &&
    Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}

function validLabels(list, labels, maximum) {
  return Array.isArray(list) && list.length <= maximum && new Set(list).size === list.length &&
    list.every((label) => labels.has(label));
}

function validActivity(activity) {
  if (!exactKeys(activity, ['topics', 'developments', 'latest']) ||
      !validLabels(activity.topics, activityTopics, 4) ||
      !validLabels(activity.developments, activityDevelopments, 3)) return false;
  const { latest } = activity;
  return latest === null || (exactKeys(latest, ['intent', 'topic', 'reply']) && latestNotes.has(latest.intent) &&
    (latest.topic === null || activityTopics.has(latest.topic)) && ['later', 'earlier', 'none'].includes(latest.reply));
}

function formatActivity(heading, lines, { topics, developments, latest }, followUp) {
  const result = [heading];
  if (topics.length) result.push('Topics · ' + topics.map((key) => activityTopics.get(key)).join(', ') + '.');
  if (developments.length) {
    result.push('Recent activity · ' + developments.map((key) => activityDevelopments.get(key)).join(', ') + '.');
  }
  if (latest) {
    let note = latestNotes.get(latest.intent);
    const topic = activityTopics.get(latest.topic);
    if (topic && latest.intent === 'other') note = 'mentions ' + topic;
    if (topic && ['ask-question', 'share-update'].includes(latest.intent)) note += ' about ' + topic;
    if (note) {
      result.push('Latest incoming · ' + note + '.');
      if (latest.reply === 'earlier') result.push('It came after your last outgoing message in that conversation.');
      if (latest.reply === 'later') result.push('You posted in that conversation afterward.');
    }
  }
  if (followUp) result.push('May need your reply · ' + followUp + '.');
  if (result.length === 1) result.push('The recent previews did not give Andrea a clear topic or request.');
  for (const line of lines) {
    if (line.startsWith('The latest message was incoming') || line.startsWith('Topics detected in previews')) continue;
    result.push(line.replace('across 1 recent conversations.', 'across 1 recent conversation.'));
  }
  return result.join('\n');
}

export function parseAndreaSummary(payload) {
  const { version, summary: directSummary, groupSummary, activity, followUps } = JSON.parse(payload) ?? {};
  const grouped = groupSummary !== undefined;
  const heading = grouped ? groupTitle : title;
  const summary = grouped ? groupSummary : directSummary;
  const lines = typeof summary === 'string' && summary.length <= 1000 ? summary.split('\n') : [];
  if (version !== 1 || lines.shift() !== heading || lines.length < 1 ||
      lines.length > lineFormats.length || lines.some((line) => !lineFormats.some((format) => format.test(line)))) {
    throw new Error('Andrea summary response was invalid');
  }
  const counts = countLine.exec(lines[0]);
  if (counts && validActivity(activity)) {
    return formatActivity(heading, lines, activity, formatFollowUps(followUps, Number(counts[1])));
  }
  return summary;
}

export async function sshAndreaSummary(signal, {
  directory = join(homedir(), process.env.FO_LOCAL_NODE === 'nucbox'
    ? '.ssh/openclaw-fo-andrea-nuc' : '.ssh/openclaw-fo-andrea'),
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

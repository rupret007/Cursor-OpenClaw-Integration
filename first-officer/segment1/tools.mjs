import { execFile } from 'node:child_process';
import { constants } from 'node:fs';
import { open } from 'node:fs/promises';
import { homedir, loadavg } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';

const exec = promisify(execFile);
const nodes = new Map([
  ['bobserver', 'Bobserver'], ['mac-mini', 'Mac Mini'],
  ['nucbox', 'NucBox'], ['macbook-pro', 'MacBook Pro'],
]);
const maxStatusBytes = 128 * 1024;
const maxAgeMs = 300_000;
const recentMs = 48 * 60 * 60 * 1000;
const clock = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/Chicago', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
});
const stamp = (at) => clock.format(at) + ' CT';
const natural = (value) => Number.isSafeInteger(value) && value >= 0;
const recent = (at, now, age = maxAgeMs) => Number.isFinite(at) && at <= now + 30_000 && now - at <= age;
const validLoads = (loads) => loads.length === 3 && loads.every((value) => Number.isFinite(value) && value >= 0);
const formatLoad = (loads) => loads.map((value) => value.toFixed(2)).join(' / ');

export async function readFleetSnapshot(path = join(homedir(), 'work/status/STATUS.json')) {
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.uid !== process.getuid() || stat.mode & 0o022 || stat.size > maxStatusBytes) {
      throw new Error('Invalid fleet status publisher output');
    }
    const buffer = Buffer.alloc(maxStatusBytes + 1);
    const { bytesRead } = await file.read(buffer, 0, buffer.length, 0);
    if (bytesRead !== stat.size) throw new Error('Fleet status changed while reading');
    const result = JSON.parse(buffer.subarray(0, bytesRead).toString('utf8'));
    if (result.v !== 1 || !natural(result.ts) || Math.abs(Date.parse(result.generated_at) - result.ts * 1000) >= 1000 ||
        !Number.isFinite(Date.parse(result.generated_at)) || !result.host) {
      throw new Error('Invalid fleet status format');
    }
    return result;
  } finally {
    await file.close();
  }
}

export async function fleetStatus({ node }, { read = readFleetSnapshot, load = loadavg, now = Date.now } = {}) {
  // Local load and remote collector availability are independent.
  const snapshot = await read().catch(() => undefined);
  const checkedAt = now();
  const fresh = recent(snapshot?.ts * 1000, checkedAt);
  const lines = ['Fleet load (1 / 5 / 15 minutes) and tracked work'];
  for (const [id, name] of nodes) {
    if (node && node !== id) continue;
    if (id === 'bobserver') {
      const loads = load();
      const jobs = fresh && natural(snapshot.host?.heavy_jobs)
        ? snapshot.host.heavy_jobs + ' heavy jobs as of ' + stamp(snapshot.ts * 1000)
        : 'job count unavailable';
      lines.push(validLoads(loads)
        ? name + ' · ' + formatLoad(loads) + ' at ' + stamp(checkedAt) + ' · ' + jobs
        : name + ' · live load unavailable · ' + jobs);
      continue;
    }
    const sample = fresh ? snapshot.nodes?.[id] : undefined;
    const observedAt = Date.parse(sample?.timestamp);
    const loads = [sample?.load1, sample?.load5, sample?.load15];
    if (!sample || sample.missing || sample.stale || !recent(observedAt, checkedAt)) {
      lines.push(name + ' · current load and tracked jobs unavailable');
      continue;
    }
    const jobs = [
      natural(sample.codex_jobs) ? sample.codex_jobs + ' Codex jobs' : 'Codex count unavailable',
      natural(sample.test_jobs) ? sample.test_jobs + ' test jobs' : 'test count unavailable',
    ];
    lines.push(name + ' · ' + (validLoads(loads) ? formatLoad(loads) : 'load unavailable') +
      ' at ' + stamp(observedAt) + ' · ' + jobs.join(', '));
  }
  if (!node) {
    const counts = ['running', 'active', 'queued', 'stalled'].flatMap((state) =>
      fresh && natural(snapshot.lane_counts?.[state]) ? [snapshot.lane_counts[state] + ' ' + state] : [],
    );
    lines.push(counts.length ? 'Lane counts at ' + stamp(snapshot.ts * 1000) + ' · ' + counts.join(', ') :
      'Current lane counts unavailable.');
  }
  return lines.join('\n');
}

const prQuery = `
query FirstOfficerOpenPullRequests {
  viewer {
    login
    pullRequests(first: 10, states: OPEN, orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount
      pageInfo { hasNextPage }
      nodes {
        number isDraft headRefOid
        repository { nameWithOwner }
        commits(last: 1) { nodes { commit { oid statusCheckRollup { state } } } }
      }
    }
  }
}`;
const repoSlug = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\/[A-Za-z0-9_.-]{1,100}$/;
const head = /^[a-f0-9]{40}$/;
const checks = new Map([
  ['SUCCESS', 'passing'], ['FAILURE', 'failing'], ['ERROR', 'error'],
  ['PENDING', 'pending'], ['EXPECTED', 'expected checks pending'],
]);

async function queryGithub(query, signal) {
  const { stdout } = await exec('/usr/bin/nice', [
    '-n', '10', '/usr/bin/gh', 'api', 'graphql', '--hostname', 'github.com', '-f', 'query=' + query,
  ], {
    timeout: 8000, killSignal: 'SIGKILL', maxBuffer: 128 * 1024, signal,
    env: { HOME: homedir(), PATH: '/usr/bin:/bin', GH_PROMPT_DISABLED: '1' },
  });
  return JSON.parse(stdout);
}

export async function openPullRequests(signal, { query = queryGithub, expectedLogin } = {}) {
  const result = await query(prQuery, signal);
  const viewer = result?.data?.viewer;
  const pulls = viewer?.pullRequests;
  if (result?.errors?.length || typeof expectedLogin !== 'string' ||
      viewer?.login?.toLowerCase() !== expectedLogin.toLowerCase() || !Array.isArray(pulls?.nodes) ||
      !natural(pulls.totalCount) || pulls.nodes.length > 10 ||
      pulls.nodes.length !== Math.min(pulls.totalCount, 10) ||
      pulls.pageInfo?.hasNextPage !== (pulls.totalCount > 10)) throw new Error('GitHub PR list could not be verified');
  if (!pulls.totalCount) return 'GitHub shows no open PRs for the configured account as of ' + stamp(Date.now()) + '.';

  const rows = pulls.nodes.map((pr) => {
    const slug = pr?.repository?.nameWithOwner;
    if (!repoSlug.test(slug ?? '') || !natural(pr.number) || !pr.number || !head.test(pr.headRefOid ?? '')) {
      throw new Error('GitHub PR identity could not be verified');
    }
    const commit = pr.commits?.nodes?.[0]?.commit;
    const rollup = commit?.statusCheckRollup;
    const ci = commit?.oid !== pr.headRefOid || rollup === undefined ? 'CI unavailable for this head' :
      rollup === null ? 'no check rollup reported' : (checks.get(rollup.state) ?? 'CI unavailable for this head');
    return slug + ' #' + pr.number + (pr.isDraft === true ? ' (draft)' : '') + ' · ' +
      pr.headRefOid.slice(0, 8) + ' · ' + ci;
  });
  const total = pulls.totalCount;
  return [
    'GitHub · ' + total + ' open PR' + (total === 1 ? '' : 's') + ' at ' + stamp(Date.now()) + '.',
    ...(total > rows.length ? ['Showing the 10 most recently updated.'] : []),
    ...rows,
  ].join('\n');
}

const themes = new Map([
  ['scheduling', /\b(?:meet|meeting|appointment|schedule|calendar|tomorrow|reschedule)\b/i],
  ['travel or pickup', /\b(?:flight|airport|pickup|pick up|drop off|arrive|driving|ride)\b/i],
  ['payments or orders', /\b(?:payment|invoice|receipt|order|refund|paid|bill)\b/i],
  ['follow-up', /\b(?:reminder|follow up|follow-up|check in|checking in|update|confirm)\b/i],
]);

export async function summarizeAndrea(reader, now = Date.now()) {
  const list = await reader.chats();
  // The reader can observe a new preview while this turn is already in flight.
  const inWindow = (at) => recent(at, now, recentMs);
  const candidates = list.chats.filter((chat) => inWindow(Date.parse(chat.lastMessageAt)))
    .sort((a, b) => Date.parse(b.lastMessageAt) - Date.parse(a.lastMessageAt));
  const chats = candidates.slice(0, 4);
  const reads = await Promise.allSettled(chats.map((chat) => reader.messages(chat.chatJid)));
  const failures = reads.filter((read) => read.status === 'rejected').length;
  if (failures && failures === reads.length) throw new Error('Andrea previews unavailable');
  const groups = reads.filter((read) => read.status === 'fulfilled').map((read) => read.value
    .filter((row) => inWindow(Date.parse(row.timestamp))).sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp)));
  const messages = groups.flat();
  const incoming = messages.filter((message) => !message.isFromMe).length;
  const tags = [...themes].filter(([, pattern]) => messages.some((message) => pattern.test(message.preview))).map(([label]) => label);
  const lines = ['Andrea · synced one-to-one text activity from the last 48 hours.'];
  if (messages.length) {
    const latestIncoming = groups.filter((group) => group[0]?.isFromMe === false).length;
    lines.push(incoming + ' incoming, ' + (messages.length - incoming) + ' outgoing across ' +
      groups.filter((group) => group.length).length + ' recent conversations.');
    if (latestIncoming) lines.push('The latest message was incoming in ' + latestIncoming + ' of those conversations.');
    if (tags.length) lines.push('Topics detected in previews · ' + tags.join(', ') + '.');
  } else {
    lines.push('Andrea returned no recent previews in the chats checked.');
  }
  if (failures) lines.push('Andrea could not read ' + failures + ' of the selected conversations.');
  if (candidates.length > chats.length) lines.push('Checked the four most recent conversations.');
  if (!list.complete) lines.push('Andrea returned a partial chat list.');
  return lines.join('\n');
}

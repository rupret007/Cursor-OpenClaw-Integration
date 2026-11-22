import { execFile } from 'node:child_process';
import { constants } from 'node:fs';
import { access, open } from 'node:fs/promises';
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

export function localFleetNode(value = process.env.FO_LOCAL_NODE ?? 'bobserver') {
  if (!nodes.has(value)) throw new Error('Unknown local fleet host');
  return value;
}

export function fleetSnapshotIsLocal(snapshot, local = localFleetNode()) {
  return Boolean(snapshot?.host) && (snapshot.host.node ?? 'bobserver') === local;
}

export async function standaloneGateStatus({ read, now = Date.now, local = localFleetNode() }) {
  const gate = await read();
  if (gate?.node !== local || !recent(gate.observed * 1000, now()) ||
      typeof gate.hold !== 'boolean' || typeof gate.running !== 'boolean') {
    throw new Error('Current local gate evidence is unavailable');
  }
  const state = gate.hold ? 'HOLD is active' : gate.running ? 'Full gate is running' : 'Gate is idle';
  return state + ' · observed ' + stamp(gate.observed * 1000) + '.';
}

export async function standaloneQuotaStatus({ read, now = Date.now }) {
  const quota = await read();
  const time = now();
  if (!recent(quota?.observed * 1000, time, 60_000)) throw new Error('Local quota evidence is unavailable');
  const windows = [['5-hour', quota.session], ['weekly', quota.weekly]].map(([label, window]) => {
    if (!Number.isFinite(window?.used) || window.used < 0 || window.used > 100 ||
        !Number.isSafeInteger(window.reset) || window.reset * 1000 <= time) {
      throw new Error('Local quota windows are unavailable');
    }
    return label + ' · ' + window.used + '% used · resets ' + stamp(window.reset * 1000);
  });
  return ['Local Codex quota · checked ' + stamp(quota.observed * 1000) + '.', ...windows,
    'Other provider quota windows are unavailable from this local check.'].join('\\n');
}

export function validLocalCapacity(reading, local, now) {
  return reading?.node === local && recent(reading.observed * 1000, now, 60_000) && natural(reading.used) &&
    Number.isSafeInteger(reading.limit) && reading.limit >= 1 && reading.limit <= 100;
}

export async function fleetStatus({ node, localOnly }, {
  read = readFleetSnapshot, load = loadavg, now = Date.now, local = localFleetNode(), capacity,
} = {}) {
  // Local load and remote collector availability are independent.
  const [snapshot, live] = await Promise.all([
    read().catch(() => undefined), capacity?.().catch(() => undefined),
  ]);
  const checkedAt = now();
  const fresh = recent(snapshot?.ts * 1000, checkedAt);
  const localEvidence = fresh && fleetSnapshotIsLocal(snapshot, local);
  if (localOnly) node = local;
  const lines = ['Fleet load (1 / 5 / 15 minutes) and tracked work'];
  for (const [id, name] of nodes) {
    if (node && node !== id) continue;
    if (id === local) {
      const loads = load();
      let jobs = 'job count unavailable';
      if (capacity && validLocalCapacity(live, local, checkedAt)) {
        jobs = live.used + '/' + live.limit + ' tracked heavy processes as of ' + stamp(live.observed * 1000);
      } else if (!capacity && localEvidence && natural(snapshot.host?.heavy_jobs)) {
        jobs = snapshot.host.heavy_jobs + ' heavy jobs as of ' + stamp(snapshot.ts * 1000);
      }
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
      localEvidence && natural(snapshot.lane_counts?.[state]) ? [snapshot.lane_counts[state] + ' ' + state] : [],
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

async function githubCli(args, signal) {
  const candidates = process.platform === 'darwin'
    ? ['/opt/homebrew/bin/gh', '/usr/local/bin/gh']
    : ['/usr/bin/gh', join(homedir(), '.local/bin/gh')];
  const installed = await Promise.all(candidates.map((path) =>
    access(path, constants.X_OK).then(() => path, () => null)));
  const github = installed.find(Boolean);
  if (!github) throw new Error('The local GitHub CLI is unavailable');
  const { stdout } = await exec('/usr/bin/nice', [
    '-n', '10', github, 'api', ...args, '--hostname', 'github.com',
  ], {
    timeout: 8000, killSignal: 'SIGKILL', maxBuffer: 128 * 1024, signal,
    env: { HOME: homedir(), PATH: '/usr/bin:/bin', GH_PROMPT_DISABLED: '1' },
  });
  return JSON.parse(stdout);
}

function queryGithub(query, signal) {
  return githubCli(['graphql', '-f', 'query=' + query], signal);
}

export function readIntegrationGithub(path, signal) {
  const prefix = 'repos/rupret007/Cursor-OpenClaw-Integration/';
  const route = path.slice(prefix.length);
  if (!path.startsWith(prefix) ||
      !/^(?:pulls\/[1-9]\d{0,7}|commits\/[a-f0-9]{40}\/check-runs\?per_page=100&filter=latest)$/.test(route)) {
    throw new Error('The GitHub route is outside the CI review scope');
  }
  return githubCli([path], signal);
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

const activityTopics = [
  ['travel', /\b(?:airport|flight|hotel|layover|road trip|vacation|boarding|train|rideshare)\b/],
  ['logistics', /\b(?:pick[ -]?up|drop[ -]?off|bring|carpool|delivery|parking|address|directions|entrance|ride)\b/],
  ['work', /\b(?:at work|to work|for work|work (?:hours|shift|schedule)|office|client|coworker|project|deadline|review meeting|pull request|deploy(?:ment)?)\b/],
  ['events', /\b(?:party|birthday|wedding|concert|game night|rehearsal|tickets?|ceremony|showtime)\b/],
  ['family', /\b(?:mom|dad|parents?|grandma|grandpa|kids|family|siblings?)\b/],
  ['payments', /\b(?:payments?|invoice|receipt|refund|paid|paying|reimburse(?:ment)?|bill)\b/],
  ['orders', /\b(?:placed (?:an |the |your |my )?order|orders?|shipment|shipped|package|tracking number)\b/],
  ['food', /\b(?:breakfast|lunch|dinner|pizza|food|groceries|restaurant|meal|menu|snacks?)\b/],
  ['shared-material', /\b(?:attachment|document|slides|screenshot|spreadsheets?|photos?|pdf|link to)\b/],
  ['scheduling', /\b(?:meet(?:ing)?|appointments?|schedule|calendar|reschedul\w*|availability|what time|which day|(?:mon|tues|wednes|thurs|fri|satur|sun)day)\b|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b/],
  ['follow-up', /\b(?:reminder|follow[ -]up|check(?:ing)? in|quick update|touch base|circle back)\b/],
];

const activityIntents = [
  ['ask-time', /^(?:(?:hey[,!]?|(?:does )?anyone know|(?:can|could) (?:you|someone|everyone) confirm) )?(?:what time\b|(?:on )?which day\b|when\s+(?:are|is|do|does|did|will|can|could|should|would)\b)/],
  ['ask-location', /^(?:(?:hey[,!]?|(?:does )?anyone know) )?(?:what(?:'s| is) the address\b|which (?:entrance|location)\b|where\s+(?:are|is|do|does|did|will|can|could|should|would)\b)/],
  ['ask-confirmation', /\b(?:please confirm|(?:can|could|would) (?:you|someone|everyone) confirm|let me know if (?:that|this) works)\b|^(?:are we still|is (?:that|this|it) (?:still )?(?:right|correct|on|confirmed)|does (?:that|this) (?:still )?work)\b/],
  ['request-action', /^(?:hey[,!]?\s+)?(?:please\s+\w+|(?:can|could|would|will) (?:you|someone|anyone)\s+\w+|let (?:me|us) know|don't forget to)\b/],
  ['ask-question', /\?|^(?:(?:what|who|why|how|which)\s+(?:are|is|do|does|did|will|can|could|should|would)|any(?:one)? (?:ideas|thoughts|know))\b/],
  ['plan-changed', /^(?:(?:quick update|fyi)[,:]\s*)?(?:change of plans|(?:we|i) (?:need|have) to (?:reschedule|cancel|postpone)|(?:we|i) (?:rescheduled|canceled|cancelled|postponed)|(?:(?:the|our|my) )?(?:meeting|appointment|event|call|reservation|flight) (?:is|was|has been|got) (?:rescheduled|canceled|cancelled|postponed|moved))\b/],
  ['plan-confirmed', /^(?:(?:quick update|fyi)[,:]\s*)?(?:we(?:'re| are) (?:still on|all set)|see you (?:at|on|tomorrow|tonight)|(?:(?:the|our|my) )?(?:meeting|appointment|event|call|reservation|flight) (?:is|was|has been) (?:confirmed|booked)|(?:we|i) (?:confirmed|booked) (?:our |the |a )?(?:meeting|appointment|reservation|flight))\b/],
  ['share-item', /^(?:here(?:'s| is) (?:a |the )?(?:link|file|document|photo|screenshot)|attaching|i (?:attached|uploaded))\b/],
  ['share-update', /^(?:quick update|status update|fyi)\b|\b(?:i (?:just )?(?:finished|completed|sent|paid|arrived)|(?:order|package) (?:shipped|arrived)|(?:it|order|package) (?:is|was) (?:ready|delivered))\b/],
  ['acknowledgment', /^(?:thanks(?: so much)?|thank you|got it|ok(?:ay)?|sounds good|will do|great)[.!\s]*$/],
];

const activityDevelopments = new Map([
  ['ask-time', 'question'], ['ask-location', 'question'], ['ask-confirmation', 'question'],
  ['ask-question', 'question'], ['request-action', 'request'], ['plan-changed', 'plan-changed'],
  ['plan-confirmed', 'plan-confirmed'], ['share-item', 'shared-item'], ['share-update', 'update'],
]);

export function describeAndreaActivity(groups) {
  const messages = groups.flatMap((group, conversation) => group.map((row) => ({
    conversation, timestamp: Date.parse(row.timestamp), preview: row.preview, isFromMe: row.isFromMe,
  }))).filter(({ timestamp, preview, isFromMe }) =>
    Number.isFinite(timestamp) && typeof preview === 'string' && preview.trim() && typeof isFromMe === 'boolean')
    .sort((a, b) => b.timestamp - a.timestamp);
  const topics = new Set();
  const developments = new Set();
  let latest = null;

  for (const message of messages) {
    const preview = message.preview.trim().toLowerCase().replaceAll('’', "'").replace(/\s+/g, ' ');
    const plain = preview.replace(/https?:\/\/\S+/g, '').trim();
    const includesLink = plain !== preview;
    const labels = activityTopics.filter(([label, pattern]) =>
      (label === 'shared-material' && includesLink) || pattern.test(plain)).map(([label]) => label);
    const intent = activityIntents.find(([, pattern]) => pattern.test(plain))?.[0] ??
      (includesLink ? 'share-item' : 'other');
    if (!labels.length && intent === 'ask-time') labels.push('scheduling');
    if (!labels.length && intent === 'ask-location') labels.push('logistics');
    for (const label of labels) topics.add(label);
    const development = activityDevelopments.get(intent);
    if (development) developments.add(development);

    if (latest === null && !message.isFromMe) {
      const outgoing = messages.filter((candidate) => candidate.conversation === message.conversation && candidate.isFromMe);
      // A later outgoing preview shows activity in the same chat, not which message it answered.
      const reply = outgoing.some(({ timestamp }) => timestamp > message.timestamp) ? 'later' :
        outgoing.some(({ timestamp }) => timestamp < message.timestamp) ? 'earlier' : 'none';
      latest = { intent, topic: labels[0] ?? null, reply };
    }
  }

  return { topics: [...topics].slice(0, 4), developments: [...developments].slice(0, 3), latest };
}

export async function inspectAndrea(reader, now = Date.now()) {
  const list = await reader.chats();
  // The reader can observe a new preview while this turn is already in flight.
  const inWindow = (at) => recent(at, now, recentMs);
  const latest = (chat) => Math.max(...[chat.lastMessageAt, chat.lastInboundAt, chat.lastOutboundAt]
    .map((at) => Date.parse(at)).filter((at) => Number.isFinite(at) && at <= now + 30_000));
  const candidates = [...list.chats].sort((a, b) => {
    const first = latest(a);
    const second = latest(b);
    return first === second ? 0 : second - first;
  });
  // Chat metadata can lag the preview index. Confirm recency from each
  // message itself, while keeping the same four-read bound.
  const chats = candidates.slice(0, 4);
  const reads = await Promise.allSettled(chats.map((chat) => reader.messages(chat.chatJid)));
  const failures = reads.filter((read) => read.status === 'rejected').length;
  const previews = reads.filter((read) => read.status === 'fulfilled').map((read) => read.value);
  const groups = previews.map((rows) => rows.filter((row) =>
    inWindow(Date.parse(row.timestamp)) && typeof row.preview === 'string' && row.preview.trim())
    .sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp)));
  return {
    groups, failures, complete: list.complete,
    counts: {
      eligibleChats: candidates.length, freshChatMetadata: candidates.filter((chat) => inWindow(latest(chat))).length,
      selectedChats: chats.length, readFailures: failures, acceptedRows: previews.flat().length,
      recentTextPreviews: groups.flat().length,
    },
  };
}

export async function summarizeAndrea(reader, now = Date.now()) {
  return formatAndreaSummary(await inspectAndrea(reader, now));
}

export function formatAndreaSummary({ groups, failures, complete, counts }, { grouped = false, allowEmpty = false } = {}) {
  if (failures && failures === counts.selectedChats && !allowEmpty) throw new Error('Andrea previews unavailable');
  const messages = groups.flat();
  const incoming = messages.filter((message) => !message.isFromMe).length;
  const tags = [...themes].filter(([, pattern]) => messages.some((message) => pattern.test(message.preview))).map(([label]) => label);
  const lines = [grouped ? 'Andrea · activity in approved group chats from the last 48 hours.' :
    'Andrea · synced one-to-one text activity from the last 48 hours.'];
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
  if (counts.eligibleChats > counts.selectedChats) lines.push('Checked the four most recent conversations.');
  if (!complete) lines.push('Andrea returned a partial chat list.');
  return lines.join('\n');
}

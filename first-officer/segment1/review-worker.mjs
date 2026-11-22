import { pathToFileURL } from 'node:url';
import { integrationCI } from './captain.mjs';
import { readIntegrationGithub } from './tools.mjs';

export async function verifyReview(request, { read = readIntegrationGithub, signal = AbortSignal.timeout(20_000) } = {}) {
  if (!request || Object.keys(request).length !== 3 || !/^fo-ci-[a-f0-9]{24}$/.test(request.lane ?? '') ||
      !/^[a-f0-9]{40}$/.test(request.head ?? '') || !Number.isSafeInteger(request.pull_request) ||
      request.pull_request < 1 || request.pull_request > 99_999_999) {
    throw new Error('Invalid Captain review request');
  }
  const pr = request.pull_request;
  const result = await integrationCI(pr, (path) => read(path, signal));
  signal.throwIfAborted();
  if (result.head !== request.head || !['passing', 'failing'].includes(result.ci)) {
    throw new Error('CI did not return terminal evidence for the queued head');
  }
  return { lane: request.lane, pr, head: result.head, ci: result.ci };
}

async function main() {
  try {
    const [lane, head, pr] = process.argv.slice(2);
    const result = await verifyReview({ lane, head, pull_request: Number(pr) });
    process.stdout.write(JSON.stringify(result) + '\n');
  } catch {
    process.stderr.write('Captain could not verify CI for the exact queued head.\n');
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) void main();

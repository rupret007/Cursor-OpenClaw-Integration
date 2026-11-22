import { createServer } from 'node:http2';
import { isAbsolute, basename } from 'node:path';
import { pathToFileURL } from 'node:url';
import * as restate from '@restatedev/restate-sdk/node';

const script = process.env.FO_CAPTAIN_REVIEW_SCRIPT ?? '';
if (!isAbsolute(script) || basename(script) !== 'review-worker.mjs') {
  throw new Error('Expected the pinned Captain review script');
}
const { verifyReview } = await import(pathToFileURL(script));
const port = Number(process.env.FO_CAPTAIN_REVIEW_PORT ?? 9080);
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid worker port');

const service = restate.service({
  name: 'CaptainReview',
  handlers: {
    verify: async (ctx, request) => ctx.run('verify-exact-ci', async () => {
      try {
        return await verifyReview(request);
      } catch {
        throw new restate.TerminalError('Captain could not verify CI for the exact queued head');
      }
    }),
  },
});
createServer(restate.createEndpointHandler({ services: [service] })).listen(port, 'localhost');

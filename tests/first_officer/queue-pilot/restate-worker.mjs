import {createServer} from 'node:http2'
import {DatabaseSync} from 'node:sqlite'
import {setTimeout as delay} from 'node:timers/promises'
import * as restate from '@restatedev/restate-sdk/node'

const db = new DatabaseSync(process.env.PILOT_EFFECTS)
db.exec('PRAGMA busy_timeout=5000; PRAGMA journal_mode=WAL')
const insert = db.prepare('INSERT OR IGNORE INTO effects VALUES (?, ?, ?, ?, ?)')
const record = restate.handlers.handler({idempotencyRetention: 5000}, async (ctx, input) => {
  await ctx.run('effect', async () => {
    const started = Date.now()
    if (input.lane === 'slow') await delay(1500)
    // The primary key is the logical request. Its row is the entire synthetic side effect.
    const row = insert.run(input.lane, input.head, input.pull_request, started, Date.now())
    if (input.lane === 'crash' && row.changes === 1) await delay(30000)
    return true
  })
  return {done: true}
})
const service = restate.service({name: 'QueuePilot', handlers: {record}})
createServer(restate.createEndpointHandler({services:[service]})).listen(Number(process.env.PILOT_WORKER_PORT), 'localhost')

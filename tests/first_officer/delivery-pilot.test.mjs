
import assert from 'node:assert/strict';
import test from 'node:test';
import { createCaptain } from '../../first-officer/segment1/captain.mjs';

async function ask(delivery) {
  const calls = [];
  const view = {
    lease: { epoch: 2, holder: 'first-officer', state: 'held' },
    grant: { epoch: 2 }, armed: true,
    jobs: [{ pr: 39, lane: 'fixture', head: 'a'.repeat(40), state: 'verified', delivery }],
  };
  const captain = createCaptain({ state: async (operation) => {
    calls.push(operation);
    assert.equal(operation, 'view');
    return structuredClone(view);
  } });
  const text = await captain.run({ action: 'decisions' }, { signal: AbortSignal.timeout(3000) });
  assert.deepEqual(calls, ['view']);
  assert.equal(view.jobs[0].state, 'verified');
  return text;
}

test('verified CI preserves the verdict and calls out unconfirmed final delivery', async () => {
  const text = await ask('delivery-unverified');
  assert.match(text, /Required CI passed for that exact head/);
  assert.match(text, /delivery.{0,60}(unconfirmed|unverified|could not be confirmed)/i);
  assert.match(text, /check|inspect/i);
});

test('a confirmed completion does not create a delivery decision', async () => {
  assert.match(await ask('reported'), /You don’t need to do anything right now/);
});

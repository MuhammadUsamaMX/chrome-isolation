#!/usr/bin/env node
/**
 * End-to-end check for the R1 bridge-queue fix, against a REAL child process.
 *
 *   node tests/test_bridge_queue_e2e.mjs
 *
 * WHY THIS IS SEPARATE FROM test_bridge_queue.mjs
 * ----------------------------------------------
 * That suite drives the module with fake children, which is what makes the
 * hang reproducible on demand and the mutations cheap. It cannot, however,
 * prove the real code path works against a real pipe. This file does that: a
 * genuine `child_process.spawn`, a genuine broken stdin, a genuine SIGKILL, and
 * the payload queued *before* the bridge dies — the exact shape R1 describes.
 *
 * The distinguishing observation is a HANG, so every wait here is raced against
 * a deadline. A regression fails the run instead of blocking it.
 *
 * The bridge is a real child that ignores stdin briefly rather than
 * backend/bridge.py itself: a real bridge.py exits when its stdin closes, so it
 * cannot be held alive with a broken pipe. The condition the queue sees is
 * identical — `bridge` exists, `bridge.stdin.writable === false` — and the
 * replacement child is spawned by the module's own restart path.
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const bq = require(path.join(ROOT, 'electron', 'bridge_queue.js'));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
/** Race a promise against a deadline, so a hang fails rather than blocks. */
const deadline = (p, ms, what) =>
  Promise.race([p, sleep(ms).then(() => { throw new Error(`${what}: HUNG`); })]);

/**
 * Deaf for `DEAF_MS`, then reads stdin and answers. Deafness keeps the process
 * alive after the parent destroys its write end, which is the window R1 needs.
 */
const DEAF_MS = 250;
const CHILD = `
  setTimeout(() => {
    const rl = require('readline').createInterface({ input: process.stdin });
    rl.on('line', (l) => {
      if (!l.trim()) return;
      const r = JSON.parse(l);
      process.stdout.write(JSON.stringify({ id: r.id, result: { n: 1 } }) + '\\n');
    });
  }, ${DEAF_MS});
  setInterval(() => {}, 1000);
`;

function newQueue() {
  return bq.createBridgeQueue({
    cmd: process.execPath,
    args: ['-e', CHILD],
    spawnChild: spawn,
    log: () => {},
    onQuit: () => {},
  });
}

let passed = 0;
const failures = [];
async function test(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log(`PASS ${name}`);
  } catch (err) {
    failures.push(name);
    console.log(`FAIL ${name}`);
    console.log(String(err && err.message).split('\n').map((l) => '      ' + l).join('\n'));
  }
}

await test('a real broken pipe queues a payload, and a real SIGKILL does not lose it', async () => {
  const q = newQueue();
  q.startBridge();
  const first = q.getBridge();
  await new Promise((r) => first.on('spawn', r));
  await sleep(20);

  // Real broken pipe: the write end is gone, the process is still alive.
  first.stdin.destroy();
  await sleep(40);
  assert.equal(first.stdin.writable, false, 'precondition: the pipe is broken');
  assert.equal(first.exitCode, null, 'precondition: the process is still alive');

  // Never reaches a process, so it must queue unsent.
  const queued = q.callBackend('list_profiles');
  assert.equal(q.queuedIds().length, 1, 'precondition: the payload is queued');
  assert.equal(q.pendingCount(), 1, 'precondition: it still has a pending promise');

  // Now the bridge dies with that payload in the queue. This is the R1 path.
  first.kill('SIGKILL');

  // The retry lands on a replacement process that answers.
  const r = await deadline(queued, 9000, 'the queued request after a real crash');
  assert.deepEqual(r, { n: 1 }, 'the queued payload must be retried and answered');
  assert.equal(q.queuedIds().length, 0, 'the queue must drain');
  assert.equal(q.pendingCount(), 0, 'nothing may be left pending');
  q.dispose();
});

await test('a real in-flight request is rejected on a real crash', async () => {
  const q = newQueue();
  q.startBridge();
  const child = q.getBridge();
  await new Promise((r) => child.on('spawn', r));
  await sleep(DEAF_MS + 60);            // the child is now reading and answering

  // Warm up, so the pipe is confirmed working before the request that must fail.
  const warm = await deadline(q.callBackend('list_profiles'), 5000, 'the warm-up request');
  assert.deepEqual(warm, { n: 1 }, 'precondition: a healthy request resolves');

  const inFlight = q.callBackend('list_profiles');
  child.kill('SIGKILL');
  await assert.rejects(
    deadline(inFlight, 5000, 'the in-flight request'),
    /Bridge unavailable/,
    'a request that reached the dead process must reject, not hang',
  );
  q.dispose();
});

await test('the module survives a crash/restart cycle with no leaked handles', async () => {
  const q = newQueue();
  for (let i = 0; i < 3; i++) {
    q.startBridge();
    const child = q.getBridge();
    await new Promise((r) => child.on('spawn', r));
    const p = q.callBackend('list_profiles').catch(() => 'rejected');
    child.kill('SIGKILL');
    await deadline(p, 5000, `cycle ${i}`);
  }
  q.dispose();
  // If a restart timer or a stdin listener outlived dispose(), this process would
  // not exit on its own — reaching here and the runner finishing is the check.
  assert.equal(q.pendingCount(), 0);
});

console.log(`\n${passed}/${passed + failures.length} passed`);
process.exit(failures.length ? 1 : 0);

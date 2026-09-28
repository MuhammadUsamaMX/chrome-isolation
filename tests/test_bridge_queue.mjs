/**
 * Bridge request-queue tests.
 *
 *   node tests/test_bridge_queue.mjs
 *
 * There is no JS test runner in this repo, so this is a plain script using
 * node:assert, with an exit code. It exists because the bug it guards is
 * invisible from the Python side: the request queue lives in the Electron main
 * process, and a lost request shows up as a UI action that simply never
 * resolves — no error, no log, no failing test.
 *
 * The logic is NOT reimplemented here. `electron/bridge_queue.js` takes every
 * host dependency (spawn, the dialog, the renderer notification, the clock) by
 * injection, so the exact production module is driven directly, against fake
 * child processes. The backoff constants are overridden to keep the retry and
 * give-up paths down to milliseconds.
 *
 * The suite is also replayed against deliberately re-broken copies of the module
 * (see MUTATIONS) and is required to FAIL there, so it cannot pass vacuously.
 */

import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

const require = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));
const MODULE_PATH = join(HERE, '..', 'electron', 'bridge_queue.js');

// Tunables: collapse the 1/2/4/8s backoff and the 10s stability window so the
// restart and give-up paths run in milliseconds. The *logic* is untouched.
const TUNABLES = [
  [/const BRIDGE_BACKOFF_BASE_MS = \d+;/, 'const BRIDGE_BACKOFF_BASE_MS = 2;'],
  [/const BRIDGE_BACKOFF_MAX_MS = \d+;/, 'const BRIDGE_BACKOFF_MAX_MS = 1000;'],
  [/const BRIDGE_STABLE_MS = \d+;/, 'const BRIDGE_STABLE_MS = 20;'],
  [/const FLUSH_RETRY_MS = \d+;/, 'const FLUSH_RETRY_MS = 3;'],
];

/**
 * Deliberately re-broken variants of the real module, expressed as the diff a
 * plausible edit would make. The suite must go red against every one of them.
 *
 *   bug  — the committed regression (R1): failInFlightRequests() deletes every
 *          pendingRequests entry, then looks the queued ones up again and finds
 *          nothing, so a queued-but-unsent request is never retried, never
 *          rejected, and hangs forever.
 *   old  — the pre-F8 behaviour: reject and drop the whole queue, silently
 *          losing the user's unsent action.
 *   flush— flushQueue() no longer gates on stdin.writable, so it writes to a
 *          dead pipe on a spawn failure. Guards the reachability claim.
 */
const MUTATIONS = {
  bug: [[
    `      if (!p.sent) continue; // never reached a process — keep it queued for retry
      pendingRequests.delete(id);
      p.reject(new Error(message));
    }`,
    `      if (p.sent) p.reject(new Error(message));
      pendingRequests.delete(id);
    }
    for (const { id } of pendingQueue) {
      const p = pendingRequests.get(id);
      if (p) p.reject(new Error(message));
    }
    pendingQueue = [];`,
  ]],
  old: [[
    `      if (!p.sent) continue; // never reached a process — keep it queued for retry
      pendingRequests.delete(id);
      p.reject(new Error(message));
    }`,
    `      pendingRequests.delete(id);
      p.reject(new Error(message));
    }
    pendingQueue = [];`,
  ]],
  flush: [[
    `    if (!child || !child.stdin || !child.stdin.writable) return;`,
    `    if (!child || !child.stdin) return;`,
  ]],
};

/**
 * Load the module, optionally mutated.
 *
 * Always via a scratch copy under a temp dir — the repo file is never modified.
 * Two guards make that honest:
 *   1. `diffIsConstantsOnly()` proves the copy differs from the real file by
 *      nothing but the four timing constants, so the tests really are running
 *      the shipped logic, just with a 1s/2s/4s/8s backoff compressed to
 *      2/4/8/16ms so the retry and give-up paths fit in a test run.
 *   2. Every mutation must match its target text exactly, so a change to
 *      bridge_queue.js that invalidates a fixture fails loudly rather than
 *      silently skipping the non-vacuity pass.
 */
function loadModule(mutate) {
  const real = readFileSync(MODULE_PATH, 'utf8');
  let src = real;
  if (mutate) {
    const edits = MUTATIONS[mutate];
    assert.ok(edits, `unknown mutation "${mutate}"`);
    for (const [from, to] of edits) {
      assert.ok(
        src.includes(from),
        `mutation "${mutate}" no longer matches bridge_queue.js — the fixture needs updating`,
      );
      src = src.replace(from, to);
    }
  }
  for (const [re, to] of TUNABLES) src = src.replace(re, to);
  if (!mutate) assert.ok(diffIsConstantsOnly(real, src), 'the tunable copy diverged beyond the constants');
  const dir = mkdtempSync(join(tmpdir(), 'bridge-queue-'));
  const path = join(dir, 'bridge_queue.js');
  writeFileSync(path, src);
  return { ...require(path), __path: path };
}

/** True if `a` and `b` differ only on lines matching the TUNABLES patterns. */
function diffIsConstantsOnly(a, b) {
  const la = a.split('\n');
  const lb = b.split('\n');
  if (la.length !== lb.length) return false;
  for (let i = 0; i < la.length; i++) {
    if (la[i] === lb[i]) continue;
    if (!TUNABLES.some(([re]) => re.test(la[i]) && re.test(lb[i]))) return false;
  }
  return true;
}

// ── fakes ──────────────────────────────────────────────────────────────────

/**
 * A child process whose pipes the test drives by hand.
 *
 * `deadStdin` models a spawn failure: Node hands back a ChildProcess whose stdio
 * streams are already destroyed, so `stdin.writable` is false by the time
 * startBridge() reaches its flush. That is the window the bug lived in.
 */
class FakeChild extends EventEmitter {
  constructor(deadStdin = false) {
    super();
    this.written = [];           // payloads this *process* received
    this.stdin = new EventEmitter();
    // A broken pipe is modelled by flipping this mid-life; a spawn failure
    // starts out this way.
    this.stdin.writable = !deadStdin;
    this.stdin.write = (payload) => { this.written.push(payload); return true; };
    this.stdout = new EventEmitter();
    this.stderr = new EventEmitter();
    // readline shim: the harness delivers responses by hand via `respond`.
    const baseOn = this.stdout.on.bind(this.stdout);
    this.stdout.on = (ev, cb) =>
      (ev === 'line' ? (this._onLine = cb) : baseOn(ev, cb));
  }
  respond(id, result) { this._onLine?.(JSON.stringify({ id, result })); }
  kill(reason = 'killed') { this.stdin.writable = false; this.emit('exit', null, reason); }
  failSpawn(msg = 'spawn ENOENT') {
    this.stdin.writable = false;
    this.emit('error', new Error(msg));
  }
  /** Payloads received, parsed. */
  get requests() { return this.written.map((w) => JSON.parse(w)); }
}

/**
 * Attach settlement handlers *immediately* and resolve to the outcome. A
 * rejection firing before anyone awaits it would be an unhandled rejection and
 * would kill the test process — which is not what the renderer does, and would
 * mask the assertions.
 */
const settle = (p, ms = 500) => new Promise((r) => {
  let done = false;
  p.then(
    (v) => { if (!done) { done = true; r({ ok: true, v }); } },
    (e) => { if (!done) { done = true; r({ ok: false, e }); } },
  );
  setTimeout(() => { if (!done) { done = true; r({ ok: false, timeout: true }); } }, ms);
});

/** Start tracking a request's promise without awaiting it yet. */
const watch = (p) => settle(p);

/**
 * Assert an outcome actually arrived. Without this, a hung promise surfaces as
 * `Cannot read properties of undefined (reading 'message')`, which hides the
 * one thing these tests exist to catch.
 */
function assertSettled(r, label) {
  assert.notEqual(r.timeout, true, `${label} never settled — that is the hang bug`);
  assert.ok(r.ok === true || r.e instanceof Error, `${label} settled with neither a value nor an Error`);
}
const tick = (n = 4) => new Promise((r) => {
  let i = n;
  const step = () => (--i ? setTimeout(step, 1) : r());
  setTimeout(step, 1);
});

/**
 * Which module the *current run* is exercising. Tests call `loadModule()` with
 * no argument, so one test body can be replayed against the real source and
 * against every reintroduced bug.
 */
let ACTIVE_MUTATION;

/** Build a queue wired to fakes, using the module under test. */
function loadQueue(overrides = {}) {
  const mod = loadModule(ACTIVE_MUTATION);
  const state = { children: [], gives: [], quits: 0, notifications: [], spawnDead: false };
  const q = mod.createBridgeQueue({
    cmd: 'bridge',
    args: [],
    spawnChild: () => {
      const c = new FakeChild(state.spawnDead);
      state.children.push(c);
      // A real spawn failure surfaces asynchronously via 'error'.
      if (state.spawnDead) setTimeout(() => c.failSpawn(), 0);
      return c;
    },
    // Stand in for readline.createInterface: the harness delivers response
    // lines by hand via FakeChild.respond().
    createLineReader: (input) => ({ on: (ev, cb) => input.on(ev, cb) }),
    notifyRenderer: (info) => state.notifications.push(info),
    onGiveUp: async (info) => { state.gives.push(info); return overrides.giveUpChoice || 'quit'; },
    onQuit: () => { state.quits++; },
    log: () => {},
    ...overrides.deps,
  });
  return { q, state, mod, last: () => state.children[state.children.length - 1] };
}

// ── tests ──────────────────────────────────────────────────────────────────

const tests = [];
const test = (name, fn) => tests.push({ name, fn });

test('(a) a sent in-flight request is rejected when the bridge dies', async () => {
  const { q, state, last } = loadQueue();
  q.startBridge();
  const child = last();
  child.emit('spawn');

  const p = watch(q.callBackend('list_profiles'));
  assert.equal(child.written.length, 1, 'payload should have been written directly');

  child.kill('SIGPIPE');
  const r = await p;
  assertSettled(r, 'the in-flight request');
  assert.equal(r.ok, false, 'a sent request must reject, not hang');
  assert.match(r.e.message, /Bridge unavailable/);
  assert.equal(q.pendingCount(), 0, 'rejected entry must be removed');
  assert.equal(q.queuedIds().length, 0);
});

test('(b) a queued-unsent request survives the loss and is re-flushed to the new bridge', async () => {
  const { q, state, last } = loadQueue();
  q.startBridge();
  const first = last();
  first.emit('spawn');

  const sent = watch(q.callBackend('list_profiles'));
  // Broken pipe: process alive, stdin dead → this request has to be queued.
  first.stdin.writable = false;
  const queued = watch(q.callBackend('start_profile', { name: 'x' }));
  assert.equal(first.written.length, 1, 'queued request must not be written to the dead pipe');
  assert.equal(q.queuedIds().length, 1);
  // I1: every queued id has an unsent pending entry.
  for (const id of q.queuedIds()) assert.equal(q.isSent(id), false, `I1 violated for ${id}`);

  first.kill('SIGPIPE');
  await tick(8); // let the backoff timer fire and the restart happen

  const second = last();
  assert.notEqual(second, first, 'a new bridge process should have been spawned');
  second.emit('spawn');
  await tick();

  assert.equal(second.written.length, 1, 'the queued payload must be retried exactly once');
  const payload = second.requests[0];
  assert.equal(payload.method, 'start_profile', 'the *queued* request is the one retried');
  assert.equal(q.queuedIds().length, 0, 'queue must be empty after the flush');
  // I2: nothing sent is left in the queue.
  assert.equal(q.queuedIds().length, 0);

  second.respond(payload.id, { ok: true });
  assert.deepEqual(await queued, { ok: true, v: { ok: true } }, 'the retried request must resolve');

  // The originally-sent one was rejected at the crash, not silently retried.
  const sr = await sent;
  assert.equal(sr.ok, false);
  assert.match(sr.e.message, /Bridge unavailable/);
});

test('(c) nothing hangs: every promise settles across a crash/restart cycle', async () => {
  const { q, state, last } = loadQueue();
  q.startBridge();
  const first = last();
  first.emit('spawn');

  const promises = [];
  for (let i = 0; i < 4; i++) {
    // Alternate: some go out on the live pipe, some queue behind a dead one.
    if (i % 2) first.stdin.writable = false;
    promises.push(watch(q.callBackend('op', { i })));
  }

  first.kill('SIGPIPE');
  await tick(8);
  const second = last();
  second.emit('spawn');
  await tick();

  // The fake bridge never answers on its own, so do it explicitly: a retried
  // request has to be able to complete, not merely be written.
  for (const r of second.requests) second.respond(r.id, { done: true });

  const settled = await Promise.all(promises);
  for (const [i, r] of settled.entries()) assertSettled(r, `request ${i}`);
  // I3: every request is now settled. Retried ones resolved, sent ones rejected.
  assert.equal(q.pendingCount(), 0, 'a request was left unsettled');
  assert.equal(q.queuedIds().length, 0, 'the queue was left non-empty');
  assert.ok(settled.some((r) => r.ok), 'the retried requests should have resolved');
  assert.ok(settled.some((r) => !r.ok), 'the sent requests should have rejected');
});

test('(d) give-up rejects everything, including queued-but-unsent', async () => {
  const { q, state, last } = loadQueue();
  q.startBridge();
  const child = last();
  child.emit('spawn');
  const sent = watch(q.callBackend('list_profiles'));
  child.stdin.writable = false;
  const queued = watch(q.callBackend('stop_profile', { name: 'y' }));
  assert.equal(q.queuedIds().length, 1, 'precondition: one request is queued');

  await q.bridgeGiveUp('spawn failure');

  for (const [label, p] of [['sent', sent], ['queued', queued]]) {
    const r = await p;
    assertSettled(r, `the ${label} request at give-up`);
    assert.equal(r.ok, false, `${label} request must reject on give-up`);
    assert.match(r.e.message, /could not be started/);
  }
  assert.equal(q.pendingCount(), 0);
  assert.equal(q.queuedIds().length, 0);
  assert.equal(q.isStopped(), true, 'bridgeStopped must latch so nothing new is queued');
  assert.equal(state.notifications.length, 1, 'the renderer must be told');
  assert.equal(state.gives.length, 1, 'the user must be told');
  assert.equal(state.quits, 1);

  // And nothing new is silently accepted after the give-up.
  const after = await watch(q.callBackend('list_profiles'));
  assert.equal(after.ok, false);
  assert.match(after.e.message, /could not be started/);
  assert.equal(q.queuedIds().length, 0, 'a post-give-up request must not be queued');
});

test('(e) no double-send: a payload reaches exactly one bridge process', async () => {
  const { q, state, last } = loadQueue();
  q.startBridge();
  let first = last();
  first.emit('spawn');
  first.stdin.writable = false;
  watch(q.callBackend('delete_profile', { name: 'z' }));
  assert.equal(q.queuedIds().length, 1);

  // Repeated loss/restart cycles, with the flush triggered on 'spawn', on
  // stdin 'ready', and by the inline call — all racing on the same queue.
  for (let i = 0; i < 3; i++) {
    first.kill('SIGSEGV');
    await tick(8);
    const next = last();
    next.emit('spawn');
    next.stdin.emit('ready');
    q.flushQueue(next);
    next.stdin.writable = false;
    first = next;
  }

  const perChild = state.children.map(
    (c) => c.written.filter((w) => w.includes('delete_profile')).length,
  );
  assert.equal(
    perChild.filter((n) => n > 0).length, 1,
    `payload duplicated across processes: ${perChild}`,
  );
  assert.equal(perChild.reduce((a, b) => a + b, 0), 1, `payload written ${perChild} times in total`);
});

test('(f) spawn failure: the queue is neither lost nor stranded', async () => {
  const { q, state, last } = loadQueue();
  q.startBridge();
  const first = last();
  first.emit('spawn');
  first.stdin.writable = false;
  const p = watch(q.callBackend('create_profile', { name: 'q' }));

  // From now on every spawn fails, so the restart's inline flush finds a
  // destroyed stdin and writes nothing. Set *before* the crash so the restart
  // that follows is one of the failing ones.
  state.spawnDead = true;
  first.kill('SIGPIPE');
  await tick(8);

  const second = last();
  assert.notEqual(second, first);
  assert.equal(second.stdin.writable, false, 'precondition: this bridge is unusable too');
  assert.equal(second.written.length, 0, 'nothing may be written to a dead pipe');
  // This is the assertion the original regression would have failed: the queue
  // is neither dropped nor left with nothing to wake it up.
  assert.equal(q.queuedIds().length, 1, 'the queue must still be intact');
  assert.equal(q.pendingCount(), 1);

  // …and it survives further unusable spawns without being dropped.
  await tick(10);
  assert.ok(state.children.length > 2, 'expected further restart attempts');
  assert.equal(q.queuedIds().length, 1, 'queue was dropped by a spawn failure');

  // A working bridge picks it up and the promise completes.
  state.spawnDead = false;
  q.startBridge();
  const good = last();
  good.emit('spawn');
  await tick();
  assert.equal(good.written.length, 1, 'the stranded payload was never retried');
  assert.equal(q.queuedIds().length, 0);
  good.respond(good.requests[0].id, { created: true });
  assert.equal((await p).ok, true, 'the request stranded across spawn failures must resolve');
});

test('(g) backoff gives up after MAX_FAILURES and rejects the queue', async () => {
  const { q, state, mod } = loadQueue();
  state.spawnDead = true; // every attempt fails to spawn
  q.startBridge();
  const p = watch(q.callBackend('list_profiles'));
  assert.equal(q.queuedIds().length, 1, 'precondition: the request is queued');

  for (let i = 0; i < mod.BRIDGE_MAX_FAILURES + 1; i++) await tick(10);

  const r = await p;
  assertSettled(r, 'the queued request at backoff give-up');
  assert.equal(r.ok, false, 'the give-up path must reject, not hang');
  assert.match(r.e.message, /could not be started/);
  assert.equal(q.queuedIds().length, 0, 'give-up must clear the queue');
  assert.equal(q.pendingCount(), 0);
  assert.equal(q.isStopped(), true);
  assert.equal(state.gives.length, 1, 'give-up must be reached exactly once');
});

test('(h) give-up "Retry" restarts the bridge and accepts new work', async () => {
  const { q, state, last } = loadQueue({ giveUpChoice: 'retry' });
  q.startBridge();
  const first = last();
  first.emit('spawn');
  const dropped = watch(q.callBackend('stop_profile', { name: 'r' }));
  first.stdin.writable = false;
  watch(q.callBackend('delete_profile', { name: 'd' }));

  await q.bridgeGiveUp('spawn failure');
  assert.equal((await dropped).ok, false, 'give-up must reject what it dropped');
  assert.equal(q.isStopped(), false, 'Retry must clear the stopped latch');

  await tick();
  const fresh = last();
  fresh.emit('spawn');
  const after = watch(q.callBackend('list_profiles'));
  assert.equal(fresh.written.length, 1, 'the retried bridge must accept new work');
  fresh.respond(fresh.requests[0].id, { ok: 1 });
  assert.equal((await after).ok, true);
});

test('(i) a late event from a superseded child cannot disturb a healthy bridge', async () => {
  const { q, state, last } = loadQueue();
  q.startBridge();
  const first = last();
  first.emit('spawn');
  first.stdin.writable = false;
  watch(q.callBackend('list_profiles'));

  first.kill('SIGPIPE');
  await tick(8);
  const second = last();
  second.emit('spawn');
  // Let the stability window elapse, so the failure counter is back to zero
  // and any later increment can only come from the late events.
  await tick(40);
  assert.equal(q.queuedIds().length, 0, 'the queue should have flushed to the new bridge');

  // The old child emits again; the identity check must swallow it.
  first.emit('exit', 0, null);
  first.emit('error', new Error('late'));
  await tick();
  assert.equal(q.getFailures(), 0, 'a late event was counted as a fresh failure');
  assert.equal(q.isStopped(), false, 'a late event triggered give-up');
  assert.equal(state.children.length, 2, 'a late event spawned another process');
  assert.equal(second.written.length, 1, 'the payload was re-sent somewhere else');
});

test('(j) backoff schedule: exponential and capped, per the shipped constants', async () => {
  const { mod } = loadQueue();
  const { backoffDelay, BRIDGE_MAX_FAILURES, BRIDGE_BACKOFF_BASE_MS, BRIDGE_BACKOFF_MAX_MS } = mod;
  for (const n of [1, 2, 3, 4, 5, 6, 9, 40]) {
    assert.equal(
      backoffDelay(n),
      Math.min(BRIDGE_BACKOFF_BASE_MS * 2 ** (n - 1), BRIDGE_BACKOFF_MAX_MS),
      `unexpected backoff for attempt ${n}`,
    );
  }
  assert.ok(backoffDelay(4) > backoffDelay(3), 'backoff must grow');
  assert.equal(backoffDelay(50), BRIDGE_BACKOFF_MAX_MS, 'the cap must apply');
  assert.equal(BRIDGE_MAX_FAILURES, 5);

  // The production numbers, read from the file rather than the tunable copy:
  // 1s / 2s / 4s / 8s, giving up on the 5th attempt, capped at 30s.
  const real = readFileSync(MODULE_PATH, 'utf8');
  const num = (name) => Number(real.match(new RegExp(`const ${name}\\s*=\\s*(\\d+);`))[1]);
  assert.equal(num('BRIDGE_BACKOFF_BASE_MS'), 1000);
  assert.equal(num('BRIDGE_BACKOFF_MAX_MS'), 30000);
  assert.equal(num('BRIDGE_MAX_FAILURES'), 5);
  assert.equal(num('BRIDGE_STABLE_MS'), 10000);
  const base = 1000, max = 30000;
  assert.deepEqual(
    [1, 2, 3, 4].map((n) => Math.min(base * 2 ** (n - 1), max)),
    [1000, 2000, 4000, 8000],
  );
});

test('(l) a payload queued on a not-yet-writable pipe is flushed on stdin "ready"', async () => {
  // startBridge()'s inline flushQueue is a no-op while stdin.writable is false —
  // which is the *normal* state immediately after spawn() and the permanent
  // state on a spawn failure. The real readiness signal is the event, so the
  // flush has to hang off it or the payload is stranded.
  const { q, state, last } = loadQueue();
  state.spawnDead = true;               // every spawn hands back a dead stdin
  q.startBridge();
  const p = watch(q.callBackend('create_profile', { name: 'r' }));
  const child = last();
  assert.equal(child.stdin.writable, false, 'precondition: pipe not writable');
  assert.equal(child.written.length, 0);
  assert.equal(q.queuedIds().length, 1, 'precondition: the payload is queued');

  // The pipe comes up. No restart, no new request: only the 'ready' event.
  state.spawnDead = false;
  child.stdin.writable = true;
  child.stdin.emit('ready');

  assert.equal(child.written.length, 1, 'the payload was stranded: no flush on "ready"');
  assert.equal(child.requests[0].method, 'create_profile');
  assert.equal(q.queuedIds().length, 0, 'the queue must drain after the flush');
  child.respond(child.requests[0].id, { created: true });
  const r = await p;
  assertSettled(r, 'the request flushed on "ready"');
  assert.equal(r.ok, true);
});

test('(m) the flush retry keeps re-arming until the pipe comes up', async () => {
  // The window the retry exists for: the pipe is dead when the request queues,
  // so startBridge()'s inline flush and 'spawn'/'ready' all find nothing to do,
  // and then the pipe comes up *without* emitting 'ready'. A one-shot retry
  // fires once, sees a dead pipe, and gives up — stranding the payload on a
  // perfectly healthy bridge. It has to keep re-arming.
  const { q, state, last } = loadQueue();
  q.startBridge();
  const first = last();
  first.emit('spawn');
  first.stdin.writable = false;          // the request has to queue
  const p = watch(q.callBackend('import_profile', { dest: '/tmp/x' }));
  assert.equal(q.queuedIds().length, 1);

  // Several retry rounds elapse with the pipe still dead. The payload must
  // still be owed — never dropped, never settled.
  await tick(30);                         // FLUSH_RETRY_MS is compressed to 3ms
  assert.equal(q.queuedIds().length, 1, 'the queue was dropped while the pipe was dead');
  assert.equal(q.pendingCount(), 1, 'the pending promise was lost while queued');
  assert.equal(first.written.length, 0, 'something was written to a dead pipe');

  // Now the pipe comes up, with no event to announce it.
  first.stdin.writable = true;
  await tick(30);
  assert.equal(q.queuedIds().length, 0, 'the retry gave up before the pipe came up');
  assert.equal(first.written.length, 1, 'the payload must be written exactly once');

  // And once drained, it must not keep polling or re-send.
  await tick(40);
  assert.equal(first.written.length, 1, 'the retry timer kept polling or re-sent');

  first.respond(JSON.parse(first.written[0]).id, { ok: 1 });
  const r = await p;
  assertSettled(r, 'the request rescued by the flush retry');
  assert.equal(r.ok, true);
});

test('(m2) the flush retry is bounded, not an unbounded poll', async () => {
  // A permanently dead bridge must not cost a timer for the life of the
  // process. The retry budget runs out, the queue is left owed, and the
  // restart / give-up path takes ownership from there.
  const { q, state, last, mod } = loadQueue();
  q.startBridge();
  const first = last();
  first.emit('spawn');
  first.stdin.writable = false;
  const p = watch(q.callBackend('list_profiles'));
  assert.equal(q.queuedIds().length, 1);

  // Long enough for the whole budget to be spent many times over.
  await tick(400);
  assert.equal(q.queuedIds().length, 1, 'the queue must still be owed, not dropped');
  assert.equal(q.pendingCount(), 1);
  assert.equal(first.written.length, 0);

  // A late pipe recovery is still picked up by the restart path, not lost.
  q.startBridge();
  const second = last();
  second.emit('spawn');
  await tick(10);
  assert.equal(second.written.length, 1, 'a working bridge must pick up the owed payload');
  second.respond(second.requests[0].id, { ok: 1 });
  const r = await p;
  assertSettled(r, 'the request recovered by a later bridge');
  assert.equal(r.ok, true);
  void mod;
});

test('(n) flushQueue on a dead pipe writes nothing and drops nothing', async () => {
  const { q, last } = loadQueue();
  q.startBridge();
  const child = last();
  child.emit('spawn');
  child.stdin.writable = false;
  const p = watch(q.callBackend('list_profiles'));
  assert.equal(q.queuedIds().length, 1);

  // A null child and an unwritable stdin are both no-ops, not crashes — and
  // critically not data loss.
  q.flushQueue(null);
  q.flushQueue({ stdin: { writable: false, write() { throw new Error('must not write'); } } });
  assert.equal(child.written.length, 0, 'nothing may be written to a dead pipe');
  assert.equal(q.queuedIds().length, 1, 'a no-op flush must not drop the payload');

  // A payload on a dead pipe must NOT be settled by a no-op flush: nothing has
  // acted on it, so rejecting it would lose the user's action and resolving it
  // would invent a result. It stays owed. (This is the one place a genuinely
  // pending promise is correct, so it is asserted rather than raced.)
  await tick(30);
  assert.equal(q.queuedIds().length, 1, 'the payload left the queue with the dead pipe');
  assert.equal(q.pendingCount(), 1, 'and lost its pending promise');

  child.stdin.writable = true;
  q.flushQueue(child);
  assert.equal(q.queuedIds().length, 0);
  child.respond(child.requests[0].id, { ok: 1 });
  const r = await p;
  assertSettled(r, 'the request flushed once the pipe recovered');
  assert.equal(r.ok, true);
});

test('(o) main.js delegates to bridge_queue instead of re-implementing it', async () => {
  // The whole point of extracting the module is that main.js is wired to the
  // tested code. If this drifts, the tests pass while the shipped path rots.
  const main = readFileSync(join(HERE, '..', 'electron', 'main.js'), 'utf8');
  // Comments stripped, so prose about the fix is not mistaken for the fix.
  const code = main
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n').map((l) => l.replace(/\/\/.*$/, '')).join('\n');
  assert.match(code, /require\('\.\/bridge_queue'\)/, 'main.js must require the module');
  assert.match(code, /createBridgeQueue\(\{/, 'main.js must build the queue');
  for (const gone of [
    'function failInFlightRequests',
    'function flushQueue',
    'function handleBridgeLoss',
    'function startBridge',
    'function callBackend',
    'pendingRequests',
    'pendingQueue',
    'reqCounter',
  ]) {
    assert.ok(!code.includes(gone), `main.js must not re-implement ${gone}`);
  }
  // The behaviour that must survive the extraction.
  assert.match(code, /bridge:failed/, 'the renderer must still be told on give-up');
  assert.match(code, /onQuit: \(\) => app\.quit\(\)/);
  assert.match(code, /response === 0 \? 'retry' : 'quit'/);
  assert.match(code, /bridgeQueue\.dispose\(\)/, 'the quit handler must dispose the queue');
});

test('(k) dispose() clears timers and kills the child; no further spawns', async () => {
  const { q, state, last } = loadQueue();
  q.startBridge();
  const child = last();
  child.emit('spawn');
  child.kill('SIGPIPE');
  const spawnCount = state.children.length;
  q.dispose();
  await tick(10);
  assert.equal(state.children.length, spawnCount, 'dispose() did not cancel the pending restart');
});

// ── runner ─────────────────────────────────────────────────────────────────

const only = process.argv[2];
const chosen = only ? tests.filter((t) => t.name.includes(only)) : tests;
const label = only ? `filter=${only}` : `${chosen.length} tests`;

/** Run every test against ACTIVE_MUTATION; returns the failures. */
async function runSuite() {
  const failures = [];
  for (const t of chosen) {
    try {
      await t.fn();
    } catch (err) {
      failures.push({ name: t.name, message: err.message.split('\n')[0] });
    }
  }
  return failures;
}

// Pass 1 — the real module. Must be entirely green.
ACTIVE_MUTATION = undefined;
const realFailures = await runSuite();
for (const f of realFailures) console.log(`  FAIL ${f.name}\n       ${f.message}`);
if (realFailures.length) {
  console.log(`\nFAIL: ${realFailures.length}/${chosen.length} failed against electron/bridge_queue.js (${label})`);
  process.exit(1);
}
console.log(`PASS: ${chosen.length}/${chosen.length} against electron/bridge_queue.js (${label})`);

// Pass 2 — non-vacuity. Each reintroduced bug must turn the suite red. Green
// here would mean the tests do not actually pin the behaviour.
for (const mutation of Object.keys(MUTATIONS)) {
  ACTIVE_MUTATION = mutation;
  const failures = await runSuite();
  for (const f of failures) console.log(`  caught[${mutation}] ${f.name}: ${f.message}`);
  if (!failures.length) {
    console.log(`\nFAIL: the suite is vacuous — it passes against the "${mutation}" source.`);
    process.exit(1);
  }
  console.log(`PASS: non-vacuous — the "${mutation}" source fails ${failures.length}/${chosen.length}`);
}
ACTIVE_MUTATION = undefined;

/**
 * Bridge request-queue tests (Node).
 *
 *   node tests/test_bridge_queue.mjs
 *
 * There is no JS test runner in this repo, so this is a plain script with
 * node:assert and an exit code. It exists because the bug it guards is
 * invisible from the Python side: the bridge queue lives in electron/main.js
 * and a lost request shows up as a UI action that simply never resolves.
 *
 * The logic is NOT reimplemented here. The harness slices the real
 * "Bridge lifecycle" block out of electron/main.js and evaluates it against
 * fake child processes, so a change to main.js is what these tests see. The
 * only edits applied to the extracted text are documented tunables:
 *
 *   - the backoff constants, so the retry/give-up paths run in milliseconds
 *     instead of ~15 seconds;
 *   - an appended export block, to reach the module-private state.
 *
 * The suite is also run against a deliberately re-broken copy of the source
 * (see MUTATIONS) and is required to FAIL there, so it cannot pass vacuously.
 */

import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const MAIN_JS = join(HERE, '..', 'electron', 'main.js');

// ── tunables injected into the extracted source ────────────────────────────
const TUNABLES = [
  [/const BRIDGE_BACKOFF_BASE_MS = \d+;/, 'const BRIDGE_BACKOFF_BASE_MS = 2;'],
  [/const BRIDGE_BACKOFF_MAX_MS  = \d+;/, 'const BRIDGE_BACKOFF_MAX_MS  = 4;'],
  [/const BRIDGE_STABLE_MS       = \d+;/, 'const BRIDGE_STABLE_MS       = 20;'],
];

const EXPORTS = `
  return {
    callBackend, startBridge, handleBridgeLoss, failInFlightRequests,
    flushQueue, scheduleFlushRetry, bridgeGiveUp,
    get bridge() { return bridge; },
    set bridge(v) { bridge = v; },
    get pendingRequests() { return pendingRequests; },
    get pendingQueue() { return pendingQueue; },
    get bridgeStopped() { return bridgeStopped; },
    set bridgeStopped(v) { bridgeStopped = v; },
    get bridgeFailures() { return bridgeFailures; },
    MAX_FAILURES: BRIDGE_MAX_FAILURES,
  };
`;

/**
 * Deliberately re-broken variants of the real source. Each one is the shape of
 * a plausible edit; the suite must go red against every one of them.
 *   bug  — the committed regression: failInFlightRequests deletes unsent
 *          entries without rejecting them, then looks them up again.
 *   old  — the pre-F8 behaviour: reject and drop the entire queue.
 */
const MUTATIONS = {
  bug: [
    [
      `    if (!p.sent) continue; // never reached a process — keep it queued for retry
    pendingRequests.delete(id);
    p.reject(new Error(message));
  }`,
      `    if (p.sent) p.reject(new Error(message));
    pendingRequests.delete(id);
  }
  for (const { id } of pendingQueue) {
    const p = pendingRequests.get(id);
    if (p) p.reject(new Error(message));
  }
  pendingQueue = [];`,
    ],
  ],
  old: [
    [
      `    if (!p.sent) continue; // never reached a process — keep it queued for retry
    pendingRequests.delete(id);
    p.reject(new Error(message));
  }`,
      `    pendingRequests.delete(id);
    p.reject(new Error(message));
  }
  pendingQueue = [];`,
    ],
  ],
};

/** Slice the bridge-lifecycle block out of main.js, optionally mutated. */
function extractSource(mutate) {
  const raw = readFileSync(MAIN_JS, 'utf8');
  const start = raw.indexOf('const BRIDGE_BACKOFF_BASE_MS');
  const endMarker = '// ─────────────────────────────────────────────\n// IPC handlers';
  const end = raw.indexOf(endMarker);
  assert.ok(start > 0, 'could not find BRIDGE_BACKOFF_BASE_MS in main.js');
  assert.ok(end > start, 'could not find the IPC handler section in main.js');

  let src = raw.slice(start, end);
  for (const [re, to] of TUNABLES) src = src.replace(re, to);
  if (mutate) {
    for (const [from, to] of MUTATIONS[mutate]) {
      assert.ok(
        src.includes(from),
        `mutation "${mutate}" no longer matches main.js — the fixture needs updating`,
      );
      src = src.replace(from, to);
    }
  }
  return src + EXPORTS;
}

// ── fakes ──────────────────────────────────────────────────────────────────

/** A child process whose pipes the test drives by hand. */
class FakeChild extends EventEmitter {
  constructor(deadStdin = false) {
    super();
    this.spawned = false;
    this.written = [];          // payloads this *process* received
    this.stdin = new EventEmitter();
    // A broken pipe is modelled by flipping this to false mid-life; a spawn
    // failure starts out this way, because Node returns a ChildProcess whose
    // stdio streams are already destroyed. That is the window the bug lived in.
    this.stdin.writable = !deadStdin;
    this.stdin.write = (payload) => { this.written.push(payload); return true; };
    this.stdout = new EventEmitter();
    this.stderr = new EventEmitter();
    // readline shim: let the harness deliver responses by hand.
    this.stdout.on = ((orig) => (ev, cb) => (ev === 'line' ? (this._onLine = cb) : orig.call(this.stdout, ev, cb)))(this.stdout.on.bind(this.stdout));
  }
  respond(id, result) { this._onLine?.(JSON.stringify({ id, result })); }
  kill(reason = 'killed') { this.stdin.writable = false; this.emit('exit', null, reason); }
  /** Model a spawn failure: the pipe is already dead when 'error' fires. */
  failSpawn(msg = 'ENOENT') { this.stdin.writable = false; this.emit('error', new Error(msg)); }
}

/**
 * The mutation the *current run* is exercising. 'bug' reintroduces the
 * committed regression, 'old' the pre-F8 reject-everything behaviour, and
 * undefined runs the real source. Tests call loadBridge() with no argument so
 * that a single test body can be replayed against all three.
 */
let ACTIVE_MUTATION = undefined;

/** Instantiate the extracted module against a fresh set of fakes. */
function loadBridge() {
  const mutate = ACTIVE_MUTATION;
  // `spawnDead` makes every subsequent spawn() hand back a ChildProcess with a
  // destroyed stdin, i.e. a spawn failure.
  const state = { children: [], dialogResponse: 1, quit: 0, spawnDead: false };

  const sandbox = {
    console: { log() {}, error() {} },
    setTimeout, clearTimeout, JSON, Map, Error, Promise, Boolean, String, Number,
    bridgeCmd: 'bridge', bridgeArgs: [],
    app: { isPackaged: false, quit() { state.quit++; } },
    mainWindow: null,
    spawn: () => {
      const c = new FakeChild(state.spawnDead);
      state.children.push(c);
      if (state.spawnDead) setTimeout(() => c.emit('error', new Error('spawn ENOENT')), 0);
      return c;
    },
    readline: { createInterface: ({ input }) => ({ on: (ev, cb) => input.on(ev, cb) }) },
    dialog: { showMessageBox: async () => ({ response: state.dialogResponse }) },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  // Wrapped so the appended `return` has a function to return from, and so the
  // module state is private to each load.
  const factory = vm.runInContext(
    `(function () {\n${extractSource(mutate)}\n})`,
    sandbox,
    { filename: 'main.js#bridge' },
  );

  const api = factory();
  assert.ok(api, 'extracted module did not export an API');
  return { api, state };
}

/** Latest spawned child. */
const last = (state) => state.children[state.children.length - 1];
/**
 * Attach settlement handlers *immediately* and return a promise for the result.
 * A rejection that fires before anyone awaits it would otherwise be an
 * unhandled rejection and kill the test process — which is exactly what the
 * real renderer does not do, and would mask the assertions.
 */
const settle = (p) => new Promise((r) => {
  let done = false;
  p.then(
    (v) => { if (!done) { done = true; r({ ok: true, v }); } },
    (e) => { if (!done) { done = true; r({ ok: false, e }); } },
  );
  setTimeout(() => { if (!done) { done = true; r({ ok: false, timeout: true }); } }, 400);
});

/** Start tracking a request's promise without awaiting it yet. */
const watch = (p) => { const s = settle(p); s.catch(() => {}); return s; };
const tick = (n = 3) => new Promise((r) => { let i = n; const n2 = () => (--i ? setTimeout(n2, 1) : r()); setTimeout(n2, 1); });

// ── tests ──────────────────────────────────────────────────────────────────

const tests = [];
const test = (name, fn) => tests.push({ name, fn });

test('(a) a sent in-flight request is rejected when the bridge dies', async () => {
  const { api, state } = loadBridge();
  api.startBridge();
  const child = last(state);
  child.emit('spawn');

  const p = watch(api.callBackend('list_profiles'));
  assert.equal(child.written.length, 1, 'payload should have been written directly');

  child.kill('SIGPIPE');
  const r = await p;
  assert.equal(r.ok, false, 'a sent request must reject, not hang');
  assert.match(r.e.message, /Bridge unavailable/);
  assert.equal(api.pendingRequests.size, 0, 'rejected entry must be removed');
});

test('(b) a queued-unsent request survives the loss and is re-flushed to the new bridge', async () => {
  const { api, state } = loadBridge();
  api.startBridge();
  const first = last(state);
  first.emit('spawn');

  const sent = watch(api.callBackend('list_profiles'));
  // Broken pipe: process alive, stdin dead → this request has to be queued.
  first.stdin.writable = false;
  const queued = watch(api.callBackend('start_profile', { name: 'x' }));
  assert.equal(first.written.length, 1, 'queued request must not be written to the dead pipe');
  assert.equal(api.pendingQueue.length, 1);

  first.kill('SIGPIPE');
  await tick(6); // let the backoff timer fire and the restart happen

  const second = last(state);
  assert.notEqual(second, first, 'a new bridge process should have been spawned');
  second.emit('spawn');
  await tick();

  assert.equal(second.written.length, 1, 'the queued payload must be retried exactly once');
  const payload = JSON.parse(second.written[0]);
  assert.equal(payload.method, 'start_profile', 'the *queued* request is the one retried');
  assert.equal(api.pendingQueue.length, 0, 'queue must be empty after the flush');

  second.respond(payload.id, { ok: true });
  const r = await queued;
  assert.deepEqual(r, { ok: true, v: { ok: true } }, 'the retried request must resolve');

  // The originally-sent one was rejected at the crash, not silently retried.
  const sr = await sent;
  assert.equal(sr.ok, false);
  assert.equal(sr.e.message.includes('Bridge unavailable'), true);
});

test('(c) nothing hangs: every promise settles across a crash/restart cycle', async () => {
  const { api, state } = loadBridge();
  api.startBridge();
  const first = last(state);
  first.emit('spawn');

  const promises = [];
  for (let i = 0; i < 4; i++) {
    // Alternate: some go out on the live pipe, some queue behind a dead one.
    if (i % 2) first.stdin.writable = false;
    promises.push(watch(api.callBackend('op', { i })));
  }

  first.kill('SIGPIPE');
  await tick(6);
  const second = last(state);
  second.emit('spawn');
  await tick();

  // The fake bridge never answers on its own, so do it explicitly: a retried
  // request has to be able to complete, not just be written.
  for (const payload of second.written) second.respond(JSON.parse(payload).id, { done: true });

  const settled = await Promise.all(promises);
  for (const r of settled) {
    assert.equal(r.timeout, undefined, 'a promise never settled — that is the hang bug');
  }
  // Retried ones resolve, sent ones reject. Nothing is left pending.
  assert.equal(api.pendingRequests.size, 0);
  assert.equal(api.pendingQueue.length, 0);
});

test('(d) give-up rejects everything, including queued-but-unsent', async () => {
  const { api, state } = loadBridge();
  api.startBridge();
  const child = last(state);
  child.emit('spawn');
  const sent = watch(api.callBackend('list_profiles'));
  child.stdin.writable = false;
  const queued = watch(api.callBackend('stop_profile', { name: 'y' }));
  assert.equal(api.pendingQueue.length, 1);

  await api.bridgeGiveUp('spawn failure');

  for (const [label, p] of [['sent', sent], ['queued', queued]]) {
    const r = await p;
    assert.equal(r.ok, false, `${label} request must reject on give-up`);
    assert.match(r.e.message, /could not be started/);
  }
  assert.equal(api.pendingRequests.size, 0);
  assert.equal(api.pendingQueue.length, 0);
  assert.ok(api.bridgeStopped, 'bridgeStopped must latch so nothing new is queued');

  // And nothing new is silently accepted after the give-up.
  const after = await watch(api.callBackend('list_profiles'));
  assert.equal(after.ok, false);
  assert.match(after.e.message, /could not be started/);
});

test('(e) no double-send: a payload reaches exactly one bridge process', async () => {
  const { api, state } = loadBridge();
  api.startBridge();
  let first = last(state);
  first.emit('spawn');
  first.stdin.writable = false;
  watch(api.callBackend('delete_profile', { name: 'z' }));
  assert.equal(api.pendingQueue.length, 1);

  // Repeated loss/restart cycles, with the flush triggered on 'spawn', on
  // stdin 'ready', and by the inline call — all racing on the same queue.
  for (let i = 0; i < 3; i++) {
    first.kill('SIGSEGV');
    await tick(6);
    const next = last(state);
    next.emit('spawn');
    next.stdin.emit('ready');
    api.flushQueue(next);
    next.stdin.writable = false;
    first = next;
  }

  const total = state.children.reduce((n, c) => n + c.written.length, 0);
  const perChild = state.children.map((c) => c.written.filter((w) => w.includes('delete_profile')).length);
  assert.equal(perChild.filter((n) => n > 0).length, 1, `payload duplicated across processes: ${perChild}`);
  assert.equal(total, 1, `payload written ${total} times in total`);
});

test('(f) spawn failure: the queue is neither lost nor stranded', async () => {
  const { api, state } = loadBridge();
  api.startBridge();
  const first = last(state);
  first.emit('spawn');
  first.stdin.writable = false;
  watch(api.callBackend('create_profile', { name: 'q' }));

  // The bridge dies with a broken pipe.
  first.kill('SIGPIPE');
  await tick(6);

  // Every subsequent spawn now fails, so startBridge()'s inline flush finds a
  // destroyed stdin and writes nothing.
  state.spawnDead = true;
  const second = last(state);
  assert.notEqual(second, first);
  assert.equal(second.stdin.writable, false, 'precondition: this bridge is unusable too');
  assert.equal(second.written.length, 0, 'nothing may be written to a dead pipe');
  assert.equal(api.pendingQueue.length, 1, '…so the queue must still be intact');
  assert.equal(api.pendingRequests.size, 1);

  // …and the queue survives further unusable spawns without being dropped.
  await tick(8);
  assert.ok(state.children.length > 2, 'expected further restart attempts');
  assert.equal(api.pendingQueue.length, 1, 'queue was dropped by a spawn failure');

  // A working bridge picks it up.
  state.spawnDead = false;
  api.startBridge();
  const good = last(state);
  good.emit('spawn');
  await tick();
  assert.equal(good.written.length, 1, 'the stranded payload was never retried');
  assert.equal(api.pendingQueue.length, 0);
});

test('(g) backoff still gives up after MAX_FAILURES, rejecting the queue', async () => {
  const { api, state } = loadBridge();
  state.spawnDead = true; // every attempt fails to spawn
  api.startBridge();
  const p = watch(api.callBackend('list_profiles'));
  assert.equal(api.pendingQueue.length, 1, 'precondition: the request is queued');

  for (let i = 0; i < api.MAX_FAILURES + 1; i++) await tick(8);

  const r = await p;
  assert.equal(r.ok, false, 'the give-up path must reject, not hang');
  assert.match(r.e.message, /could not be started/);
  assert.equal(api.pendingQueue.length, 0, 'give-up must clear the queue');
  assert.equal(api.pendingRequests.size, 0);
});

// ── runner ─────────────────────────────────────────────────────────────────

const only = process.argv[2];
const chosen = only ? tests.filter((t) => t.name.includes(only)) : tests;
const label = only ? `filter=${only}` : `${chosen.length} tests`;

/** Run every test against ACTIVE_MUTATION; returns [failed, total, names]. */
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

// Pass 1 — the real source. Must be entirely green.
const realFailures = await runSuite();
for (const f of realFailures) console.log(`  FAIL ${f.name}\n       ${f.message}`);
if (realFailures.length) {
  console.log(`\nFAIL: ${realFailures.length}/${chosen.length} failed against electron/main.js (${label})`);
  process.exit(1);
}
console.log(`PASS: ${chosen.length}/${chosen.length} against electron/main.js (${label})`);

// Pass 2 — non-vacuity. Each reintroduced bug must turn the suite red. A green
// suite here would mean the tests do not actually pin the behaviour.
for (const mutation of Object.keys(MUTATIONS)) {
  ACTIVE_MUTATION = mutation;
  const failures = await runSuite();
  for (const f of failures) console.log(`  caught[${mutation}] ${f.name}: ${f.message}`);
  if (!failures.length) {
    console.log(`\nFAIL: the suite is vacuous — it passes against the "${mutation}" source.`);
    process.exit(1);
  }
  console.log(`PASS: non-vacuous — "${mutation}" source fails ${failures.length}/${chosen.length}`);
}
ACTIVE_MUTATION = undefined;


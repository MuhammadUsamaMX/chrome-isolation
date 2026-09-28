'use strict';
/**
 * Bridge request queue + restart policy (F8 / T6, review finding R1).
 *
 * WHY A SEPARATE MODULE
 * ---------------------
 * `electron/main.js` cannot be `require()`d outside Electron (line 1 imports
 * `electron`). That is exactly why `failInFlightRequests()` shipped a
 * regression that survived review: the original implementation deleted *every*
 * `pendingRequests` entry in its first loop, so the second loop's
 * `pendingRequests.get(id)` was always `undefined`, and a queued-but-unsent
 * request was never retried, never rejected, and hung forever — the renderer
 * waited on a UI action that could never settle. Code with no test is a defect
 * waiting to be written, so the whole bridge lifecycle lives here where a plain
 * `node` script can reach it: see `tests/test_bridge_queue.mjs`.
 *
 * Nothing here imports `electron` or `child_process`. Every host dependency
 * (spawn, the give-up dialog, the renderer notification, logging, the clock) is
 * injected by `main.js`, which is also what makes the tests able to drive a real
 * child process, or a synthetic one, through the same code.
 *
 * INVARIANTS (asserted in the tests, relied on everywhere below)
 * ----------------------------------------------------------
 *  I1. Every id in `pendingQueue` has an entry in `pendingRequests` with
 *      `sent === false`.
 *  I2. No `sent === true` entry is ever in `pendingQueue` — `flushQueue` moves
 *      an entry out of the queue *before* writing it.
 *  I3. A `sent: true` request is settled by exactly one of: a response line,
 *      `failInFlightRequests`, or `bridgeGiveUp`. There is no path that leaves
 *      it unsettled while the app keeps running.
 *  I4. A `sent: false` request is settled by exactly one of: `flushQueue`
 *      (retry) or `bridgeGiveUp`. `failInFlightRequests` must never touch it.
 */

/** Backoff schedule — see the block comment in `main.js` for the rationale. */
const BRIDGE_BACKOFF_BASE_MS = 1000;
const BRIDGE_BACKOFF_MAX_MS = 30000;
const BRIDGE_MAX_FAILURES = 5;
const BRIDGE_STABLE_MS = 10000;
/** Retry cadence for a payload queued onto a not-yet-writable pipe. */
const FLUSH_RETRY_MS = 50;
/**
 * Upper bound on consecutive retries for one stranded queue: 50 * 20 = 1s of
 * polling. A bridge whose pipe is not up after that is not coming back soon, and
 * the restart / give-up path owns the queue from there.
 */
const FLUSH_RETRY_MAX = 20;

/**
 * Delay before restart number `failures` (1-based).
 * Exported so the schedule itself is assertable without running a real timer.
 */
function backoffDelay(failures) {
  return Math.min(
    BRIDGE_BACKOFF_BASE_MS * Math.pow(2, failures - 1),
    BRIDGE_BACKOFF_MAX_MS,
  );
}

/**
 * @param {object} deps
 * @param {(cmd: string, args: string[], opts: object) => object} deps.spawnChild
 *        Required. Must return an EventEmitter with `stdin`, `stdout`, `stderr`.
 * @param {string} deps.cmd                 Command to spawn.
 * @param {string[]} [deps.args]            Arguments for it.
 * @param {(input: object) => object} [deps.createLineReader]
 *        Defaults to `readline.createInterface({ input })`.
 * @param {(chunk: Buffer) => void} [deps.forwardStderr] Optional dev forwarding.
 * @param {(info: {reason: string, message: string}) => void} [deps.notifyRenderer]
 * @param {(info: object) => Promise<'retry'|'quit'>} [deps.onGiveUp]
 *        The user-facing dialog. Returning 'retry' restarts the bridge.
 * @param {() => void} [deps.onQuit]
 * @param {(...args: any[]) => void} [deps.log]
 * @param {{setTimeout: Function, clearTimeout: Function}} [deps.timers]
 *        Injected so the give-up path can be driven without real 1/2/4/8s waits.
 */
function createBridgeQueue(deps) {
  if (!deps || typeof deps.spawnChild !== 'function') {
    throw new Error('bridge_queue: deps.spawnChild (a function) is required');
  }
  if (!deps.cmd) throw new Error('bridge_queue: deps.cmd is required');

  const cmd = deps.cmd;
  const args = deps.args || [];
  const timers = deps.timers || { setTimeout, clearTimeout };
  const log = deps.log || ((...a) => console.error(...a));
  const forwardStderr = deps.forwardStderr || null;
  const notifyRenderer = deps.notifyRenderer || (() => {});
  const onGiveUp = deps.onGiveUp || (async () => 'quit');
  const onQuit = deps.onQuit || (() => {});
  const createLineReader = deps.createLineReader
    || ((input) => require('readline').createInterface({ input }));

  let bridge = null;
  const pendingRequests = new Map(); // id → { resolve, reject, sent }
  let pendingQueue = [];              // { id, payload } queued while the bridge is not up yet
  let reqCounter = 0;
  let bridgeFailures = 0;   // consecutive restarts that never reached 'stable'
  let bridgeStopped = null; // set to an Error once we have given up
  let restartTimer = null;
  let stableTimer = null;
  let flushRetryTimer = null;

  /**
   * Reject only the requests that were actually written to a bridge that is now
   * dead. Queued-but-unsent requests are retried, not lost: a payload that never
   * reached a process cannot have been acted on, so there is nothing to lose.
   *
   * By invariant I1/I2 the `sent === false` entries below are exactly the queued
   * ones, so `continue`-ing past them leaves the queue intact for the next
   * `flushQueue()` — no deletion, no rejection, no hang.
   *
   * The one path that must clear the queue is `bridgeGiveUp()`, where no restart
   * is coming and an unsent request would otherwise hang forever.
   */
  function failInFlightRequests(message) {
    for (const [id, p] of pendingRequests) {
      if (!p.sent) continue; // never reached a process — keep it queued for retry
      pendingRequests.delete(id);
      p.reject(new Error(message));
    }
    // pendingQueue is deliberately left untouched: those payloads are still owed
    // to the user and the next startBridge() will write them to the new process.
  }

  /**
   * Settle every pending promise, queued or not, and stop the restart loop.
   * This is the give-up path; unlike `failInFlightRequests` it must not leave
   * anything unsettled, because no further flush is ever coming.
   */
  function rejectAllPending(err) {
    for (const [, p] of pendingRequests) p.reject(err);
    pendingRequests.clear();
    pendingQueue = [];
  }

  /** Give up restarting: stop the loop and surface the failure to the user. */
  async function bridgeGiveUp(reason) {
    const message = `The Chrome Isolation backend (${cmd}) could not be started: ${reason}`;
    bridgeStopped = new Error(message);
    rejectAllPending(bridgeStopped);
    if (stableTimer) { timers.clearTimeout(stableTimer); stableTimer = null; }
    if (restartTimer) { timers.clearTimeout(restartTimer); restartTimer = null; }
    bridge = null;
    log(`Bridge gave up after ${BRIDGE_MAX_FAILURES} consecutive failures: ${message}`);

    const info = { reason, message };
    // Tell the renderer (if a window exists) so the UI can stop pretending the
    // backend is reachable; it is not enough to write to the console.
    try {
      notifyRenderer(info);
    } catch (_) { /* window torn down */ }

    const choice = await onGiveUp({
      info,
      maxFailures: BRIDGE_MAX_FAILURES,
      message,
    });
    if (choice === 'retry') {
      bridgeStopped = null;
      bridgeFailures = 0;
      startBridge();
    } else {
      onQuit();
    }
  }

  /** Common path for 'error' and 'exit': back off, or give up. */
  function handleBridgeLoss(reason) {
    failInFlightRequests(`Bridge unavailable: ${reason}`);
    bridge = null;
    if (stableTimer) { timers.clearTimeout(stableTimer); stableTimer = null; }

    bridgeFailures += 1;
    if (bridgeFailures >= BRIDGE_MAX_FAILURES) {
      bridgeGiveUp(reason);
      return;
    }
    const delay = backoffDelay(bridgeFailures);
    log(`Bridge lost (${reason}). Restart attempt ${bridgeFailures}/${BRIDGE_MAX_FAILURES - 1} in ${delay}ms…`);
    if (restartTimer) timers.clearTimeout(restartTimer);
    restartTimer = timers.setTimeout(() => { restartTimer = null; startBridge(); }, delay);
  }

  /**
   * Write every queued payload to `child`.
   *
   * Safe to call more than once and from more than one place ('spawn',
   * stdin 'ready', a post-spawn tick): the queue is emptied *before* any write,
   * and each id is marked `sent = true` in the same synchronous block, so a
   * payload can never be handed to two different bridge processes. Entries whose
   * pending promise is already gone are dropped rather than written — a payload
   * nobody is waiting for must not reach a bridge.
   *
   * If stdin is not writable yet, nothing is sent and nothing is dropped: the
   * queue stays intact and the caller is expected to call this again (on
   * 'spawn' / 'ready') or the next `startBridge()` will retry it.
   */
  function flushQueue(child) {
    if (!pendingQueue.length) return;
    if (!child || !child.stdin || !child.stdin.writable) return;
    const queued = pendingQueue;
    pendingQueue = [];
    for (const { id, payload } of queued) {
      const p = pendingRequests.get(id);
      if (!p) continue; // already rejected (give-up) — do not send it
      p.sent = true;
      try {
        child.stdin.write(payload);
      } catch (err) {
        // The pipe broke between the writable check and the write. Treat it as
        // sent (the process may have received it) and let the error/exit
        // handlers reject it; re-queueing here would risk a double send.
        log('Bridge stdin write failed:', err.message);
      }
    }
  }

  /**
   * Keep retrying the flush while a payload is owed and the pipe is not up.
   *
   * Covers the window where the process is alive but its stdin only becomes
   * writable a moment later, so nothing else would ever trigger a retry.
   *
   * This re-arms itself while the queue is non-empty, which the previous
   * one-shot version did not: it fired once, found the pipe still dead, and
   * gave up — so a pipe that came up a moment later stranded the payload with
   * no pending timer and no 'ready' event to wake it. Bounded by
   * FLUSH_RETRY_MAX so a permanently dead bridge costs a bounded amount of
   * polling rather than a timer for the life of the process; when the budget is
   * spent the queue is left owed, and the restart/give-up path owns it from
   * there. If the bridge dies in the meantime, 'error'/'exit' fire and the
   * restart path takes over.
   */
  let flushRetries = 0;
  function scheduleFlushRetry() {
    if (flushRetries >= FLUSH_RETRY_MAX) return;
    if (flushRetryTimer) return;
    flushRetryTimer = timers.setTimeout(() => {
      flushRetryTimer = null;
      // The queue drained — nothing owed, so stop and let the next request
      // schedule a fresh retry if it needs one.
      if (!pendingQueue.length) { flushRetries = 0; return; }
      flushRetries += 1;
      flushQueue(bridge);
      if (pendingQueue.length) scheduleFlushRetry();
      else flushRetries = 0;
    }, FLUSH_RETRY_MS);
    if (flushRetryTimer && flushRetryTimer.unref) flushRetryTimer.unref();
  }

  function startBridge() {
    if (bridgeStopped) return;
    bridge = deps.spawnChild(cmd, args, { stdio: ['pipe', 'pipe', 'pipe'] });
    const child = bridge;

    const rl = createLineReader(child.stdout);
    // A bridge that dies hard (SIGKILL) makes stdout emit ECONNRESET, and
    // readline re-emits it on the Interface. With no listener that is an
    // uncaught exception and it takes the whole app down — the same class of bug
    // the stdin 'error' handler below exists for, on the read side.
    if (rl.on) rl.on('error', (err) => log('Bridge stdout error:', err.message));
    if (child.stdout && child.stdout.on) {
      child.stdout.on('error', (err) => log('Bridge stdout error:', err.message));
    }
    rl.on('line', (line) => {
      try {
        const resp = JSON.parse(line);
        const pending = pendingRequests.get(resp.id);
        if (!pending) return;
        pendingRequests.delete(resp.id);
        if (resp.error) pending.reject(new Error(resp.error));
        else pending.resolve(resp.result);
      } catch (e) { /* ignore non-JSON lines */ }
    });

    if (child.stderr && child.stderr.on) {
      child.stderr.on('data', (d) => { if (forwardStderr) forwardStderr(d); });
    }

    // A broken pipe must not become an unhandled 'error' event (which would take
    // the whole app down); the 'exit' handler below does the real recovery.
    child.stdin.on('error', (err) => {
      log('Bridge stdin error:', err.message);
    });

    // Both 'error' (spawn failure) and 'exit' land here. 'lost' + the identity
    // check make sure a single failure is counted once, and that a late event
    // from a superseded child cannot null out a healthy bridge.
    let lost = false;
    const onLoss = (reason) => {
      if (lost) return;
      lost = true;
      if (bridge !== child) return;
      handleBridgeLoss(reason);
    };

    child.on('error', (err) => onLoss(`failed to start (${err.message})`));
    child.on('exit', (code, signal) =>
      onLoss(signal ? `killed by ${signal}` : `exited with code ${code}`));

    // Surviving the stability window resets the backoff.
    if (bridgeFailures > 0) {
      stableTimer = timers.setTimeout(() => {
        stableTimer = null;
        bridgeFailures = 0;
      }, BRIDGE_STABLE_MS);
    }

    // Flush requests queued while the bridge was starting.
    //
    // The old code did this once, inline, guarded by `stdin.writable`. That guard
    // is not a reliable readiness signal: on a spawn failure the pipe is already
    // dead by the time this line runs, so the flush silently did nothing and —
    // with the queue no longer being discarded — those requests would have been
    // left with nothing to wake them up. 'spawn' is the real readiness event, so
    // the flush is now event-driven and idempotent; the inline call only covers
    // the already-writable case and is a no-op otherwise.
    child.on('spawn', () => flushQueue(child));
    if (child.stdin.on) child.stdin.on('ready', () => flushQueue(child));
    flushQueue(child);
  }

  /** Send a request to the Python bridge and return a Promise. */
  function callBackend(method, params = {}) {
    return new Promise((resolve, reject) => {
      // After the give-up path, reject immediately instead of queueing payloads
      // that will never be written.
      if (bridgeStopped) { reject(bridgeStopped); return; }
      const id = String(++reqCounter);
      const payload = JSON.stringify({ id, method, params }) + '\n';
      if (bridge && bridge.stdin.writable) {
        pendingRequests.set(id, { resolve, reject, sent: true });
        bridge.stdin.write(payload);
      } else {
        pendingRequests.set(id, { resolve, reject, sent: false });
        pendingQueue.push({ id, payload });
        // Nothing else may wake this payload up: a restart would, but if this
        // bridge is merely not-writable-yet there is no restart coming.
        scheduleFlushRetry();
      }
    });
  }

  /** Release timers and kill the child. Call from the app's 'quit' handler. */
  function dispose() {
    if (restartTimer) { timers.clearTimeout(restartTimer); restartTimer = null; }
    if (stableTimer) { timers.clearTimeout(stableTimer); stableTimer = null; }
    if (flushRetryTimer) { timers.clearTimeout(flushRetryTimer); flushRetryTimer = null; }
    if (bridge) { try { bridge.kill(); } catch (_) { /* already dead */ } }
    bridge = null;
  }

  return {
    // Lifecycle
    startBridge,
    dispose,
    // Requests
    callBackend,
    // Exposed for tests and for the one caller in main.js that owns the dialog
    failInFlightRequests,
    bridgeGiveUp,
    flushQueue,
    scheduleFlushRetry,
    // Introspection (read-only views; the test asserts invariants I1/I2 on these)
    getBridge: () => bridge,
    getFailures: () => bridgeFailures,
    isStopped: () => bridgeStopped !== null,
    getStopError: () => bridgeStopped,
    pendingIds: () => [...pendingRequests.keys()],
    pendingCount: () => pendingRequests.size,
    isSent: (id) => {
      const p = pendingRequests.get(id);
      return p ? p.sent : undefined;
    },
    queuedIds: () => pendingQueue.map((e) => e.id),
    queuedPayloads: () => pendingQueue.map((e) => e.payload),
  };
}

module.exports = {
  BRIDGE_BACKOFF_BASE_MS,
  BRIDGE_BACKOFF_MAX_MS,
  BRIDGE_MAX_FAILURES,
  BRIDGE_STABLE_MS,
  FLUSH_RETRY_MS,
  backoffDelay,
  createBridgeQueue,
};

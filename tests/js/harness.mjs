/* Runs an add-on background script in a sandbox, with fakes for everything it
 * talks to.
 *
 * The background scripts are plain classic scripts: no imports, no exports, each
 * one a top-level `var` holding an object. Thunderbird loads them into one shared
 * page scope. `node:vm` reproduces exactly that — a fresh global object per test
 * whose properties are the globals we hand it — so the add-on source can be tested
 * as it ships, with no build step and no test-only branches in it.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

/**
 * Evaluate `addon/<relPath>` in a new context.
 *
 * @param {string} relPath  repo-relative to `addon/`, e.g. "background/log.js".
 * @param {object} globals  what the script may see. Standard JavaScript built-ins
 *   come with the context; anything the browser or Node supplies (`console`,
 *   `setTimeout`, `TextEncoder`, `btoa`, `browser`, the other `tbx*` scripts) does
 *   not, so pass what the script under test uses.
 * @returns {object} the context, on which the script's top-level `var`s are
 *   properties.
 */
export function loadScript(relPath, globals = {}) {
  const source = fs.readFileSync(path.join(ROOT, "addon", relPath), "utf8");
  const context = vm.createContext({ ...globals });
  vm.runInContext(source, context, { filename: relPath });
  return context;
}

/** A tbxLog that keeps what it was told instead of printing it. */
export function fakeLog() {
  const records = [];
  const record =
    (level) =>
    (...args) =>
      records.push({ level, text: args.map(String).join(" ") });
  return {
    init: async () => {},
    setVerbose() {},
    isVerbose: false,
    debug: record("debug"),
    info: record("info"),
    warn: record("warn"),
    error: record("error"),
    records,
  };
}

/**
 * Timers a test drives by hand.
 *
 * The transport is almost entirely timeouts, so real ones would make its tests
 * both slow and flaky. `advance` fires what is due in order and yields to the
 * microtask queue after each one, so an `async` handler is finished before the
 * next timer runs. `advance(0)` is therefore also the way to settle pending
 * promises without moving the clock.
 */
export function fakeClock() {
  const FUSE = 10000; // a repeating timer with a zero delay would never end
  let timers = [];
  let sequence = 0;
  let now = 0;

  const settle = () => new Promise((resolve) => setImmediate(resolve));

  function arm(fn, delay, repeat, args) {
    const timer = {
      id: (sequence += 1),
      time: now + Math.max(0, Number(delay) || 0),
      repeat,
      fn,
      args,
    };
    timers.push(timer);
    return timer.id;
  }

  /** The next timer at or before `target`; ties go to whichever was armed first. */
  function due(target) {
    let best = null;
    for (const timer of timers) {
      if (timer.time > target) {
        continue;
      }
      if (!best || timer.time < best.time || (timer.time === best.time && timer.id < best.id)) {
        best = timer;
      }
    }
    return best;
  }

  return {
    setTimeout: (fn, delay, ...args) => arm(fn, delay, null, args),
    setInterval: (fn, delay, ...args) => arm(fn, delay, Math.max(1, Number(delay) || 1), args),
    clearTimeout: (id) => {
      timers = timers.filter((timer) => timer.id !== id);
    },
    clearInterval: (id) => {
      timers = timers.filter((timer) => timer.id !== id);
    },
    now: () => now,
    async advance(ms) {
      const target = now + Math.max(0, ms);
      for (let fired = 0; ; fired += 1) {
        const timer = due(target);
        if (!timer) {
          break;
        }
        if (fired >= FUSE) {
          throw new Error(`fakeClock fired ${FUSE} timers without reaching ${target}ms`);
        }
        now = timer.time;
        if (timer.repeat === null) {
          timers = timers.filter((other) => other !== timer);
        } else {
          timer.time = now + timer.repeat;
        }
        timer.fn(...timer.args);
        await settle();
      }
      now = target;
      await settle();
    },
  };
}

/**
 * A WebSocket the test plays the daemon on.
 *
 * `open`, `receive` and `serverClose` are the daemon's side; `send`, `close` and
 * the `readyState` are the add-on's, recorded for assertions.
 */
export function fakeWebSocketClass() {
  class FakeWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = FakeWebSocket.CONNECTING;
      this.sent = [];
      this.closeCalls = [];
      this.onopen = null;
      this.onmessage = null;
      this.onerror = null;
      this.onclose = null;
      FakeWebSocket.instances.push(this);
    }

    send(data) {
      this.sent.push(JSON.parse(data));
    }

    close(code, reason) {
      this.closeCalls.push({ code, reason });
      this.serverClose(code, reason);
    }

    /** The daemon completed the HTTP upgrade. */
    open() {
      this.readyState = FakeWebSocket.OPEN;
      if (this.onopen) {
        this.onopen();
      }
    }

    /** The daemon sent a frame. */
    receive(message) {
      if (this.onmessage) {
        this.onmessage({ data: JSON.stringify(message) });
      }
    }

    /** The connection ended, from either side. */
    serverClose(code, reason) {
      this.readyState = FakeWebSocket.CLOSED;
      if (this.onclose) {
        this.onclose({ code, reason });
      }
    }
  }

  FakeWebSocket.CONNECTING = 0;
  FakeWebSocket.OPEN = 1;
  FakeWebSocket.CLOSING = 2;
  FakeWebSocket.CLOSED = 3;
  FakeWebSocket.instances = [];
  return FakeWebSocket;
}

/**
 * The `browser` global, answering only what the background scripts ask for.
 *
 * @param {object|Function} pairing  what `tbx.readBridgeFile()` resolves to; a
 *   function is called per read, so a test can decide when the read finishes.
 */
export function fakeBrowser({ pairing = null, manifestVersion = "9.9.9" } = {}) {
  const browser = {
    _written: [],
    runtime: {
      getManifest: () => ({ version: manifestVersion }),
      onSuspend: { addListener() {} },
    },
    tbx: {
      readBridgeFile: async () => (typeof pairing === "function" ? pairing() : pairing),
      appInfo: async () => ({ name: "Thunderbird", version: "155.0" }),
      availableModules: async () => ({ loaded: [], methods: [], resolvable: [] }),
      keepAlive: async () => ({ enabled: true, intervalMs: 20000, idleTimeoutMs: 30000 }),
      grantOptionalPermission: async () => ({ alreadyHad: true, granted: true }),
      writeStatus: async (report) => {
        browser._written.push(report);
      },
    },
    storage: {
      local: {
        get: async () => ({}),
        set: async () => {},
      },
    },
  };
  return browser;
}

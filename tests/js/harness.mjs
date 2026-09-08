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
  return runInContext(vm.createContext({ ...globals }), relPath);
}

/**
 * Evaluate `addon/<relPath>` in a context another script already ran in.
 *
 * Thunderbird loads every background script into one page scope, so a script
 * that reads a `var` another one declared has to be tested that way — passing it
 * in as a global would hide a load-order mistake.
 *
 * @returns {object} the same context, now carrying this script's top-level `var`s.
 */
export function runInContext(context, relPath) {
  const source = fs.readFileSync(path.join(ROOT, "addon", relPath), "utf8");
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
        return "<profile>/tbmcp-addon-status.json"; // the real one answers with the path
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

/**
 * The `browser.messages` half of the WebExtension API, over headers held in
 * memory.
 *
 * Thunderbird answers `list` and `query` with a `MessageList`: one page of
 * messages plus an `id` that is present only while further pages remain, walked
 * with `continueList` and thrown away with `abortList`. Every paging bug this
 * suite guards against lives in that shape, so the fake reproduces it exactly —
 * including `returnMessageListId`, which replaces the first page with a bare id
 * string. List ids are minted in order (`list-1`, `list-2`, …) so a test can say
 * which walk it means.
 *
 * @param {object} folders  folder id -> its headers, in the order Thunderbird
 *   would return them.
 * @param {number} pageSize  page size for `list`.
 * @param {number} queryPageSize  page size for `query`, unless the query names
 *   one with `messagesPerPage`.
 */
export function fakeMessages({ folders = {}, pageSize = 10, queryPageSize = 100 } = {}) {
  const lists = new Map();
  const calls = [];
  let sequence = 0;

  /** Start a walk over `items` and hand back its id. */
  function open(items, size) {
    sequence += 1;
    const id = `list-${sequence}`;
    lists.set(id, { items, offset: 0, size: Math.max(1, size) });
    return id;
  }

  /** The next page of a live list; the last one carries no id and closes it. */
  function page(id) {
    const list = lists.get(id);
    const messages = list.items.slice(list.offset, list.offset + list.size);
    list.offset += messages.length;
    if (list.offset >= list.items.length) {
      lists.delete(id);
      return { messages };
    }
    return { id, messages };
  }

  /** The filters `messages.query` supports that these tests exercise. */
  function matches(header, queryInfo, folderId) {
    if (queryInfo.subject && !String(header.subject || "").includes(queryInfo.subject)) {
      return false;
    }
    if (queryInfo.folderId) {
      const wanted = Array.isArray(queryInfo.folderId) ? queryInfo.folderId : [queryInfo.folderId];
      if (!wanted.includes(folderId)) {
        return false;
      }
    }
    // A folder id is "<accountId>://<path>", so the account is its prefix.
    if (queryInfo.accountId && folderId.split(":/")[0] !== queryInfo.accountId) {
      return false;
    }
    return true;
  }

  return {
    calls,
    async list(folderId, options) {
      calls.push({ method: "list", args: [folderId, options] });
      return page(open([...(folders[folderId] || [])], pageSize));
    },
    async query(queryInfo = {}) {
      calls.push({ method: "query", args: [queryInfo] });
      const hits = [];
      for (const [folderId, headers] of Object.entries(folders)) {
        for (const header of headers) {
          if (matches(header, queryInfo, folderId)) {
            hits.push(header);
          }
        }
      }
      const id = open(hits, queryInfo.messagesPerPage || queryPageSize);
      // Thunderbird's schema: the flag "will change the return value of this
      // function and return the messageListId directly".
      return queryInfo.returnMessageListId ? id : page(id);
    },
    async continueList(id) {
      calls.push({ method: "continueList", args: [id] });
      if (!lists.has(id)) {
        throw new Error("Unknown or expired list");
      }
      return page(id);
    },
    async abortList(id) {
      calls.push({ method: "abortList", args: [id] });
      lists.delete(id);
    },
  };
}

/* --------------------------------------------------------- the privileged half */

/** Must match `src/tbmcp/addon_build.py::MODULE_MARKER`; the splice below is that
 *  build step, reproduced so a test runs what ships. */
const MODULE_MARKER = "/* ===TBX_MODULES=== (build_xpi.py splices experiment/modules/*.js here) */";

/** The GlodaMsgSearcher URL, which more than one caller here needs to name. */
const GLODA_SEARCHER_URL = "resource:///modules/gloda/GlodaMsgSearcher.sys.mjs";

/**
 * A gloda full-text searcher over a fixed corpus.
 *
 * Gloda is callback-driven: a query hands its collection listener the rows it
 * found, then says it is done. `limit(n)` is the only knob the add-on turns, and
 * the number of rows that come back for a given `n` is exactly what tells
 * `gloda.search` whether the corpus was deeper than it looked — so the fake
 * honours it to the row.
 *
 * @param {object[]} corpus  synthetic gloda messages, in relevance order.
 * @param {number[]} scores  per-row scores; defaults to descending integers.
 */
export function fakeGlodaSearcherClass({ corpus = [], scores = null } = {}) {
  return class FakeGlodaMsgSearcher {
    constructor(listener, query, matchAll) {
      this.listener = listener;
      this.fulltextTerms = String(query || "")
        .split(/\s+/)
        .filter(Boolean);
      this.matchAll = matchAll;
      this.scores = scores || corpus.map((_message, index) => corpus.length - index);
      this.collection = null;
      this.query = null;
    }

    buildFulltextQuery() {
      const searcher = this;
      return {
        limit(n) {
          this.n = n;
        },
        getCollection(collectionListener) {
          const rows = corpus.slice(0, this.n === undefined ? corpus.length : this.n);
          searcher.retrieved = rows.length;
          collectionListener.onItemsAdded(rows);
          collectionListener.onQueryCompleted();
          return { items: rows };
        },
      };
    }
  };
}

/**
 * The globals Thunderbird pre-injects into the ext-*.js sandbox, faked.
 *
 * Deliberately missing: `setTimeout` and every other DOM global. The privileged
 * half runs in a plain system-principal sandbox with none of them, and code that
 * reaches for one has to fail here the way it fails in Thunderbird.
 *
 * @param {object} modules  resource URL -> its exports, merged over the defaults;
 *   an unnamed URL imports as `{}`. One URL always answers with one object, so a
 *   test can inspect what the code under test did to it.
 * @param {object} prefs  preference name -> value, for `Services.prefs`.
 */
export function fakeSandbox({ modules = {}, prefs = {} } = {}) {
  const timer = {
    armed: [],
    cleared: [],
    setTimeout(fn, ms, ...args) {
      const id = setTimeout(fn, ms, ...args);
      timer.armed.push(id);
      return id;
    },
    clearTimeout(id) {
      timer.cleared.push(id);
      clearTimeout(id);
    },
  };

  const records = [];
  const record =
    (level) =>
    (...args) =>
      records.push({ level, text: args.map(String).join(" ") });

  const known = {
    "resource://gre/modules/Timer.sys.mjs": timer,
    "resource://gre/modules/ExtensionUtils.sys.mjs": {
      ExtensionError: class ExtensionError extends Error {},
    },
    "resource:///modules/MailServices.sys.mjs": {
      MailServices: { accounts: { accounts: [], allIdentities: [] } },
    },
    [GLODA_SEARCHER_URL]: { GlodaMsgSearcher: fakeGlodaSearcherClass() },
    ...modules,
  };
  const unknown = new Map();

  return {
    ChromeUtils: {
      importESModule(url) {
        if (known[url]) {
          return known[url];
        }
        if (!unknown.has(url)) {
          unknown.set(url, {});
        }
        return unknown.get(url);
      },
    },
    Services: {
      prefs: {
        getBoolPref: (name, fallback = false) =>
          name in prefs ? Boolean(prefs[name]) : fallback,
        getIntPref: (name, fallback = 0) => (name in prefs ? Number(prefs[name]) : fallback),
      },
      appinfo: {
        name: "Thunderbird",
        version: "155.0",
        appBuildID: "20260101000000",
        platformVersion: "155.0",
        OS: "WINNT",
      },
      dirsvc: { get: (key) => ({ path: `/fake/${key}` }) },
      locale: { appLocaleAsBCP47: "en-US" },
    },
    Cc: {},
    Ci: {},
    Cu: {},
    Cr: {},
    IOUtils: {
      exists: async () => false,
      readJSON: async () => null,
      writeJSON: async () => {},
      write: async () => {},
    },
    PathUtils: { join: (...parts) => parts.join("/") },
    ExtensionAPI: class ExtensionAPI {},
    console: {
      records,
      debug: record("debug"),
      info: record("info"),
      log: record("log"),
      warn: record("warn"),
      error: record("error"),
    },
    // Block-scoped helpers publish themselves here; in Thunderbird it is undefined
    // and the publishing statement does nothing.
    TBX_TEST_HOOKS: {},
  };
}

/**
 * Evaluate the privileged half — core.js with `modules` spliced in — in one context.
 *
 * The splice is the build step, not a convenience: core.js's `H`, `mod`,
 * `TBX_MODULES` and `MODULE_URLS` are top-level `const`s, so a module loaded as a
 * separate script would not see any of them. `this.tbx = class …` at the top level
 * of core.js puts the API class on the context.
 *
 * @param {object} globals  usually `fakeSandbox()`, possibly with overrides.
 * @param {string[]} modules  base names under `addon/experiment/modules/`.
 * @returns {object} the context, with the API class at `ctx.tbx`.
 */
export function loadExperiment(globals = {}, { modules = [] } = {}) {
  const experiment = path.join(ROOT, "addon", "experiment");
  const core = fs.readFileSync(path.join(experiment, "core.js"), "utf8");
  if (!core.includes(MODULE_MARKER)) {
    throw new Error("experiment/core.js has no ===TBX_MODULES=== marker");
  }
  const chunks = modules.map((name) => {
    const body = fs.readFileSync(path.join(experiment, "modules", `${name}.js`), "utf8");
    return (
      "/* ---------------------------------------------------------------\n" +
      ` * experiment/modules/${name}.js\n` +
      " * --------------------------------------------------------------- */\n" +
      `${body.trimEnd()}\n`
    );
  });
  const context = vm.createContext({ ...globals });
  vm.runInContext(core.replace(MODULE_MARKER, chunks.join("\n")), context, {
    filename: "experiment/implementation.js",
  });
  return context;
}

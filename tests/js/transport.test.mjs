/* The dial-out half of the bridge, driven without a Thunderbird or a daemon.
 *
 * Everything here reproduces a failure seen against a real one: a handshake that
 * never completes, a probe that never resolves, two sockets racing, or a retry
 * loop that hammers a daemon that will never answer.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { fakeBrowser, fakeClock, fakeLog, fakeWebSocketClass, loadScript } from "./harness.mjs";

const PAIRING = { version: 1, port: 4711, token: "tok" };

/** Let already-resolved promises run to completion. */
const settle = () => new Promise((resolve) => setImmediate(resolve));

/** Values built inside the vm carry that realm's prototypes; strip them. */
function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

function quietConsole() {
  const noop = () => {};
  return { log: noop, info: noop, warn: noop, error: noop, debug: noop };
}

function capabilityStub(overrides = {}) {
  return {
    appInfo: async () => ({ name: "Thunderbird", version: "async" }),
    describe: async () => ({ experiment: true, source: "async" }),
    appInfoSync: () => ({ name: "Thunderbird", version: "sync" }),
    describeSync: () => ({ experiment: true, source: "sync" }),
    ...overrides,
  };
}

/** Load transport.js with every global it touches faked. */
function makeWorld({ pairing = PAIRING, capabilities = {} } = {}) {
  const clock = fakeClock();
  const WebSocket = fakeWebSocketClass();
  const log = fakeLog();
  const browser = fakeBrowser({ pairing });
  const context = loadScript("background/transport.js", {
    browser,
    WebSocket,
    tbxLog: log,
    tbxRegistry: { invoke: async () => ({}), methods: () => [] },
    tbxCapabilities: capabilityStub(capabilities),
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    setInterval: clock.setInterval,
    clearInterval: clock.clearInterval,
    TextEncoder,
    btoa,
    JSON,
    console: quietConsole(),
    Date,
  });
  const world = {
    clock,
    WebSocket,
    log,
    browser,
    transport: context.tbxTransport,
    /** Start the transport and return the socket it opens. */
    async start(identity = { app: null, capabilities: null }, options = undefined) {
      world.transport.start(identity, options);
      await clock.advance(0);
      return WebSocket.instances[0];
    },
    errors: () => log.records.filter((record) => record.level === "error").map((r) => r.text),
  };
  return world;
}

describe("hello", () => {
  it("goes out as soon as the socket opens, even while a probe is hanging", async () => {
    const world = makeWorld({
      capabilities: {
        appInfo: () => new Promise(() => {}),
        describe: () => new Promise(() => {}),
      },
    });
    const ws = await world.start({
      app: { name: "Thunderbird", version: "155.0" },
      capabilities: { experiment: true },
    });

    ws.open();

    assert.deepEqual(ws.sent[0], {
      t: "hello",
      token: "tok",
      protocol: 1,
      addonVersion: "9.9.9",
      app: { name: "Thunderbird", version: "155.0" },
      capabilities: { experiment: true },
    });
  });

  it("falls back to the cached snapshots when started without an identity", async () => {
    const world = makeWorld();
    const ws = await world.start({ app: null, capabilities: null });

    ws.open();

    assert.deepEqual(ws.sent[0].app, { name: "Thunderbird", version: "sync" });
    assert.deepEqual(ws.sent[0].capabilities, { experiment: true, source: "sync" });
  });

  it("carries values refreshed after the last welcome", async () => {
    const world = makeWorld();
    const ws = await world.start({ app: { version: "seed" }, capabilities: { source: "seed" } });
    ws.open();
    ws.receive({ t: "welcome" });
    await world.clock.advance(0);

    ws.serverClose(1006, "daemon exited");
    await world.clock.advance(600);
    const next = world.WebSocket.instances[1];
    next.open();

    assert.deepEqual(next.sent[0].app, { name: "Thunderbird", version: "async" });
    assert.deepEqual(next.sent[0].capabilities, { experiment: true, source: "async" });
  });

  it("keeps the identity it has when the refresh fails", async () => {
    const world = makeWorld({
      capabilities: {
        appInfo: async () => {
          throw new Error("privileged half is gone");
        },
      },
    });
    const ws = await world.start({ app: { version: "seed" }, capabilities: { source: "seed" } });
    ws.open();
    ws.receive({ t: "welcome" });
    await world.clock.advance(0);

    ws.serverClose(1006, "daemon exited");
    await world.clock.advance(600);
    const next = world.WebSocket.instances[1];
    next.open();

    assert.deepEqual(next.sent[0].app, { version: "seed" });
  });
});

describe("capability snapshots", () => {
  function loadCapabilities() {
    const browser = fakeBrowser();
    const context = loadScript("background/capabilities.js", {
      browser,
      tbxLog: fakeLog(),
      tbxRegistry: { methods: () => ["mail.list"] },
      console: quietConsole(),
    });
    return { capabilities: context.tbxCapabilities, browser };
  }

  it("appInfoSync falls back to the manifest until a probe has answered", async () => {
    const { capabilities } = loadCapabilities();

    assert.deepEqual(plain(capabilities.appInfoSync()), {
      name: "Thunderbird",
      version: "unknown",
      addon: "9.9.9",
    });

    await capabilities.appInfo();
    assert.deepEqual(plain(capabilities.appInfoSync()), {
      name: "Thunderbird",
      version: "155.0",
    });
  });

  it("describeSync has describe()'s shape, without a probe of its own", async () => {
    const { capabilities } = loadCapabilities();

    const before = plain(capabilities.describeSync());
    assert.deepEqual(before.privilegedModules, []);
    assert.equal(before.experiment, true);
    assert.deepEqual(before.methods, ["mail.list"]);

    const described = plain(await capabilities.describe());
    assert.deepEqual(plain(capabilities.describeSync()), described);
    assert.deepEqual(described.privilegedModules, { loaded: [], methods: [], resolvable: [] });
  });
});

describe("handshake watchdog", () => {
  it("closes a socket that never gets its welcome", async () => {
    const world = makeWorld();
    const ws = await world.start();

    ws.open();
    assert.equal(world.transport.status().state, "handshaking");
    await world.clock.advance(8000);

    assert.deepEqual(ws.closeCalls, [{ code: 4000, reason: "no welcome" }]);
    // A daemon that accepts and then ignores us is a failure, not a wait.
    assert.equal(world.transport.status().attempt, 1);
    assert.deepEqual(world.errors(), [
      "no welcome from the daemon within 8000ms after hello — closing the socket; " +
        "`tbmcp doctor` shows the daemon's view",
    ]);
  });

  it("leaves a welcomed socket alone", async () => {
    const world = makeWorld();
    const ws = await world.start();

    ws.open();
    ws.receive({ t: "welcome" });
    await world.clock.advance(8000);

    assert.deepEqual(ws.closeCalls, []);
    assert.deepEqual(world.errors(), []);
    assert.equal(world.transport.status().state, "connected");
  });
});

describe("connect watchdog", () => {
  it("closes a socket that never finishes connecting", async () => {
    const world = makeWorld();
    const ws = await world.start();

    await world.clock.advance(15000);

    assert.deepEqual(ws.closeCalls, [{ code: 4000, reason: "connect timeout" }]);
    assert.equal(world.errors().length, 1);
    assert.match(world.errors()[0], /port 4711/);
    assert.equal(world.transport.status().attempt, 1);
  });

  it("leaves a socket that opened in time alone", async () => {
    const world = makeWorld();
    const ws = await world.start();

    ws.open();
    ws.receive({ t: "welcome" });
    await world.clock.advance(15000);

    assert.deepEqual(ws.closeCalls, []);
    assert.deepEqual(world.errors(), []);
  });
});

describe("re-entrancy", () => {
  it("does not open a second socket while a connect is still reading the pairing", async () => {
    let release;
    const reading = new Promise((resolve) => (release = resolve));
    const world = makeWorld({ pairing: () => reading });

    world.transport.start({ app: null, capabilities: null });
    await world.clock.advance(10000); // the supervisor ticks while the read hangs
    assert.deepEqual(world.WebSocket.instances, []);

    release(PAIRING);
    await world.clock.advance(0);

    assert.equal(world.WebSocket.instances.length, 1);
  });

  it("ignores a close from a socket it has already replaced", async () => {
    const world = makeWorld();
    const first = await world.start();
    first.open();
    first.receive({ t: "welcome" });
    first.serverClose(1006, "daemon exited");
    await world.clock.advance(600);
    const second = world.WebSocket.instances[1];
    second.open();
    second.receive({ t: "welcome" });

    first.serverClose(1006, "late notice");

    assert.equal(world.transport.status().state, "connected");
    await world.clock.advance(5000);
    assert.equal(world.WebSocket.instances.length, 2);
  });
});

describe("retry schedule", () => {
  /** Advance to just before `delay`, then over it, counting sockets either side. */
  async function expectRetryAfter(world, delay) {
    const before = world.WebSocket.instances.length;
    await world.clock.advance(delay - 1);
    assert.equal(world.WebSocket.instances.length, before, `retried before ${delay}ms`);
    await world.clock.advance(1);
    assert.equal(world.WebSocket.instances.length, before + 1, `no retry at ${delay}ms`);
  }

  it("backs off while the same daemon keeps failing the handshake", async () => {
    const world = makeWorld();
    await world.start();

    for (const delay of [500, 1000, 2000]) {
      world.WebSocket.instances.at(-1).serverClose(1006, "closed by the daemon");
      await expectRetryAfter(world, delay);
    }

    assert.equal(world.transport.status().attempt, 3);
  });

  it("polls instead of backing off when a daemon that was working goes away", async () => {
    const world = makeWorld();
    const ws = await world.start();
    ws.open();
    ws.receive({ t: "welcome" });

    ws.serverClose(1006, "daemon exited");
    await expectRetryAfter(world, 500);

    // The daemon leaving is not this end failing, so nothing is backing off yet.
    assert.equal(world.transport.status().attempt, 0);
  });

  it("starts the schedule over when a different daemon advertises itself", async () => {
    let advertised = { version: 1, port: 4711, token: "old" };
    const world = makeWorld({ pairing: () => advertised });
    await world.start();
    for (const delay of [500, 1000, 2000]) {
      world.WebSocket.instances.at(-1).serverClose(1006, "closed by the daemon");
      await expectRetryAfter(world, delay);
    }

    advertised = { version: 1, port: 4712, token: "new" };
    world.WebSocket.instances.at(-1).serverClose(1006, "closed by the daemon");
    await expectRetryAfter(world, 4000);

    assert.equal(world.WebSocket.instances.at(-1).url, "ws://127.0.0.1:4712/tbmcp");
    world.WebSocket.instances.at(-1).serverClose(1006, "closed by the daemon");
    await expectRetryAfter(world, 500);
  });
});

describe("visible state", () => {
  const BACKOFF = [500, 1000, 2000, 4000];

  /** Close the live socket and wait out the retry, once per call. */
  async function failCycle(world, index) {
    world.WebSocket.instances.at(-1).serverClose(1006, "closed by the daemon");
    await world.clock.advance(BACKOFF[index]);
  }

  const complaints = (world) =>
    world.errors().filter((text) => text.includes("restart Thunderbird"));

  it("names every close and complains once when none of them handshake", async () => {
    const world = makeWorld();
    await world.start();

    for (let index = 0; index < 3; index += 1) {
      await failCycle(world, index);
    }

    assert.deepEqual(
      world.log.records.filter((record) => record.level === "info").map((r) => r.text),
      new Array(3).fill("socket closed (code 1006: closed by the daemon)")
    );
    assert.deepEqual(complaints(world), [
      "the daemon on port 4711 accepted 3 connections but none completed the handshake — " +
        "restart Thunderbird if this persists; `tbmcp doctor` shows the daemon's view",
    ]);

    await failCycle(world, 3);
    assert.equal(complaints(world).length, 1, "complained more than once");
  });

  it("complains again only after a welcome has reset the count", async () => {
    const world = makeWorld();
    await world.start();
    for (let index = 0; index < 3; index += 1) {
      await failCycle(world, index);
    }
    assert.equal(complaints(world).length, 1);

    const recovered = world.WebSocket.instances.at(-1);
    recovered.open();
    recovered.receive({ t: "welcome" });

    recovered.serverClose(1006, "daemon exited");
    await world.clock.advance(500);
    for (let index = 0; index < 3; index += 1) {
      await failCycle(world, index);
    }

    assert.equal(complaints(world).length, 2);
  });
});

describe("status", () => {
  it("reports the whole picture before anything has happened", () => {
    const world = makeWorld();

    assert.deepEqual(plain(world.transport.status()), {
      state: "disconnected",
      attempt: 0,
      inFlight: 0,
      consecutiveFailures: 0,
      lastCloseCode: null,
      lastCloseAt: null,
      lastWelcomeAt: null,
      port: null,
    });
  });

  it("announces the bridge coming up and the connection failing", async () => {
    const world = makeWorld();
    const seen = [];
    const ws = await world.start(
      { app: null, capabilities: null },
      { onStateChange: (report) => seen.push(plain(report)) }
    );

    ws.open();
    ws.receive({ t: "welcome" });

    assert.equal(seen.length, 1);
    assert.equal(seen.at(-1).state, "connected");
    assert.equal(seen.at(-1).port, 4711);
    assert.match(seen.at(-1).lastWelcomeAt, /^\d{4}-\d\d-\d\dT/);

    ws.serverClose(1006, "daemon exited");

    assert.equal(seen.length, 2);
    assert.equal(seen.at(-1).state, "disconnected");
    assert.equal(seen.at(-1).lastCloseCode, 1006);
    assert.equal(seen.at(-1).consecutiveFailures, 1);
    assert.match(seen.at(-1).lastCloseAt, /^\d{4}-\d\d-\d\dT/);
  });

  it("does not report every failure in a burst", async () => {
    const world = makeWorld();
    const seen = [];
    await world.start(
      { app: null, capabilities: null },
      { onStateChange: (report) => seen.push(plain(report)) }
    );

    world.WebSocket.instances.at(-1).serverClose(1006, "closed by the daemon");
    await world.clock.advance(500);
    world.WebSocket.instances.at(-1).serverClose(1006, "closed by the daemon");
    await world.clock.advance(1000);

    assert.equal(seen.length, 1);
    assert.equal(seen[0].consecutiveFailures, 1);
  });
});

describe("startup wiring", () => {
  /** Run main.js as Thunderbird would, against a transport that only records. */
  async function bootMain() {
    const browser = fakeBrowser({ pairing: PAIRING });
    const starts = [];
    loadScript("background/main.js", {
      browser,
      tbxLog: fakeLog(),
      tbxRegistry: { methods: () => ["mail.list"] },
      tbxCapabilities: capabilityStub(),
      tbxEvents: { start() {} },
      tbxTransport: {
        start: (identity, options) => starts.push({ identity, options }),
        stop() {},
      },
      console: quietConsole(),
      Date,
    });
    await settle();
    return { browser, starts };
  }

  it("keeps the status file up to date as the transport changes state", async () => {
    const { browser, starts } = await bootMain();
    const before = plain(browser._written[0]);

    starts[0].options.onStateChange({ state: "connected", port: 4711 });
    await settle();

    assert.equal(browser._written.length, 2);
    const after = plain(browser._written[1]);
    assert.deepEqual(after.transport, { state: "connected", port: 4711 });
    for (const key of ["addonVersion", "app", "capabilities", "methodCount"]) {
      assert.deepEqual(after[key], before[key], `${key} was lost`);
    }
    assert.ok(after.writtenAt >= before.writtenAt, "writtenAt went backwards");
  });

  it("hands the transport the identity it fetched for the status file", async () => {
    const { browser, starts } = await bootMain();

    assert.equal(starts.length, 1);
    assert.deepEqual(plain(starts[0].identity), {
      app: { name: "Thunderbird", version: "async" },
      capabilities: { experiment: true, source: "async" },
    });
    assert.deepEqual(plain(browser._written[0].app), { name: "Thunderbird", version: "async" });
  });
});

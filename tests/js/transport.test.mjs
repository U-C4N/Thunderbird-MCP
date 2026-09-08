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
    bounded: async (promise, _ms, fallback = null) => {
      try {
        return await promise;
      } catch (ex) {
        return fallback;
      }
    },
    ...overrides,
  };
}

/** The real capabilities module, against the same browser and clock. */
function loadCapabilities({
  browser = fakeBrowser(),
  clock = fakeClock(),
  methods = ["mail.list"],
} = {}) {
  const context = loadScript("background/capabilities.js", {
    browser,
    tbxLog: fakeLog(),
    tbxRegistry: { methods: () => methods },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    console: quietConsole(),
  });
  return context.tbxCapabilities;
}

/** Load transport.js with every global it touches faked. */
function makeWorld({
  pairing = PAIRING,
  browser = fakeBrowser({ pairing }),
  clock = fakeClock(),
  capabilities = capabilityStub(),
} = {}) {
  const WebSocket = fakeWebSocketClass();
  const log = fakeLog();
  const context = loadScript("background/transport.js", {
    browser,
    WebSocket,
    tbxLog: log,
    tbxRegistry: { invoke: async () => ({}), methods: () => [] },
    tbxCapabilities: capabilities,
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
      capabilities: capabilityStub({
        appInfo: () => new Promise(() => {}),
        describe: () => new Promise(() => {}),
      }),
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

  it("keeps the identity it has when a refresh probe stops answering", async () => {
    // The real capabilities module, because the guarantee lives in its cache: the
    // probes never reject, so a refresh that "fails" still returns something.
    const clock = fakeClock();
    const browser = fakeBrowser({ pairing: PAIRING });
    let answering = true;
    const probe = (value) => async () => {
      if (!answering) {
        throw new Error("the privileged half is not answering");
      }
      return value;
    };
    browser.tbx.appInfo = probe({ name: "Thunderbird", version: "155.0" });
    browser.tbx.availableModules = probe(["prefs", "smtp"]);
    const capabilities = loadCapabilities({ browser, clock });
    // main.js probes once before starting the transport; that is what fills the cache.
    const identity = {
      app: await capabilities.appInfo(),
      capabilities: await capabilities.describe(),
    };
    answering = false;

    const world = makeWorld({ browser, clock, capabilities });
    const ws = await world.start(identity);
    ws.open();
    ws.receive({ t: "welcome" });
    await clock.advance(0); // the refresh runs, and both probes fail

    ws.serverClose(1006, "daemon exited");
    await clock.advance(600);
    const next = world.WebSocket.instances[1];
    next.open();

    assert.deepEqual(next.sent[0].app, { name: "Thunderbird", version: "155.0" });
    assert.deepEqual(next.sent[0].capabilities.privilegedModules, ["prefs", "smtp"]);
  });
});

describe("capability snapshots", () => {
  it("appInfoSync falls back to the manifest until a probe has answered", async () => {
    const capabilities = loadCapabilities();

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

  it("bounded answers with the fallback when a probe never does", async () => {
    const clock = fakeClock();
    const capabilities = loadCapabilities({ clock });
    let settled = "pending";

    capabilities.bounded(new Promise(() => {}), 5000, "gave up").then((value) => {
      settled = value;
    });

    await clock.advance(4999);
    assert.equal(settled, "pending");
    await clock.advance(1);
    assert.equal(settled, "gave up");
  });

  it("bounded passes an answer through, and a failure becomes the fallback", async () => {
    const capabilities = loadCapabilities();

    assert.equal(await capabilities.bounded(Promise.resolve("answer"), 5000, "no"), "answer");
    assert.equal(
      await capabilities.bounded(Promise.reject(new Error("wedged")), 5000, "no"),
      "no"
    );
    assert.equal(await capabilities.bounded(Promise.resolve(7), 5000), 7);
  });

  it("describeSync has describe()'s shape, without a probe of its own", async () => {
    const capabilities = loadCapabilities();

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

describe("pairing read", () => {
  it("gives up on a read that never answers, and drops its late answer", async () => {
    let release;
    const hung = new Promise((resolve) => (release = resolve));
    let reads = 0;
    const world = makeWorld({
      pairing: () => {
        reads += 1;
        return reads === 1 ? hung : PAIRING;
      },
    });

    world.transport.start({ app: null, capabilities: null });
    await world.clock.advance(0);
    assert.equal(world.transport.status().connecting, true);

    await world.clock.advance(5000);

    assert.match(world.errors().at(-1), /readBridgeFile did not answer within 5000ms/);
    assert.equal(world.transport.status().connecting, false);
    assert.deepEqual(world.WebSocket.instances, []);

    await world.clock.advance(500); // the first step of the failed schedule
    assert.equal(reads, 2);
    assert.equal(world.WebSocket.instances.length, 1);

    release(PAIRING); // the read that timed out finally answers
    await world.clock.advance(0);

    assert.equal(world.WebSocket.instances.length, 1);
  });
});

describe("supervisor", () => {
  it("leaves a pending retry alone", async () => {
    const world = makeWorld();
    await world.start();

    // Line the third failure up so the supervisor's 10s tick falls inside the 2s
    // backoff that follows it — the case that used to turn every longer wait into
    // a ten-second one.
    await world.clock.advance(8000);
    world.WebSocket.instances.at(-1).serverClose(1006, "closed by the daemon");
    await world.clock.advance(500);
    world.WebSocket.instances.at(-1).serverClose(1006, "closed by the daemon");
    await world.clock.advance(1000);
    world.WebSocket.instances.at(-1).serverClose(1006, "closed by the daemon");
    assert.equal(world.WebSocket.instances.length, 3);
    assert.equal(world.clock.now(), 9500);

    await world.clock.advance(1999); // the tick at 10000ms lands in here
    assert.equal(world.WebSocket.instances.length, 3, "the tick pre-empted the backoff");
    await world.clock.advance(1);
    assert.equal(world.WebSocket.instances.length, 4);
  });

  it("has nothing left to run after stop()", async () => {
    const world = makeWorld();
    const ws = await world.start();
    ws.open();
    ws.receive({ t: "welcome" });

    world.transport.stop();

    assert.deepEqual(ws.closeCalls, [{ code: 1000, reason: "shutting down" }]);
    await world.clock.advance(60000);
    assert.equal(world.WebSocket.instances.length, 1);
  });

  it("follows the daemon when it moves to another port", async () => {
    let advertised = { version: 1, port: 4711, token: "old" };
    const world = makeWorld({ pairing: () => advertised });
    const ws = await world.start();
    ws.open();
    ws.receive({ t: "welcome" });

    advertised = { version: 1, port: 4712, token: "new" };
    await world.clock.advance(10000);

    assert.deepEqual(ws.closeCalls, [{ code: 1000, reason: "following the pairing file" }]);
    await world.clock.advance(500);
    assert.equal(world.WebSocket.instances.at(-1).url, "ws://127.0.0.1:4712/tbmcp");
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

  it("does not blame a new daemon for the last one's failures", async () => {
    let advertised = { version: 1, port: 4711, token: "old" };
    const world = makeWorld({ pairing: () => advertised });
    await world.start();

    await failCycle(world, 0);
    world.WebSocket.instances.at(-1).serverClose(1006, "closed by the daemon");
    advertised = { version: 1, port: 4712, token: "new" };
    await world.clock.advance(1000);

    assert.equal(world.WebSocket.instances.at(-1).url, "ws://127.0.0.1:4712/tbmcp");
    world.WebSocket.instances.at(-1).serverClose(1006, "closed by the daemon");

    assert.deepEqual(complaints(world), []);
    assert.equal(world.transport.status().consecutiveFailures, 1);
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

    // The daemon going away does not count towards the next complaint...
    recovered.serverClose(1006, "daemon exited");
    await world.clock.advance(500);
    for (let index = 0; index < 2; index += 1) {
      await failCycle(world, index);
    }
    assert.equal(complaints(world).length, 1, "counted the daemon going away");

    // ...only three attempts that never handshake do.
    await failCycle(world, 2);

    assert.equal(complaints(world).length, 2);
  });
});

describe("status", () => {
  it("reports the whole picture before anything has happened", () => {
    const world = makeWorld();

    assert.deepEqual(plain(world.transport.status()), {
      state: "disconnected",
      connecting: false,
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
    assert.match(seen.at(-1).lastCloseAt, /^\d{4}-\d\d-\d\dT/);
    // The daemon going away after a working session is not an attempt that failed.
    assert.equal(seen.at(-1).consecutiveFailures, 0);

    await world.clock.advance(500);
    world.WebSocket.instances.at(-1).serverClose(1006, "closed by the daemon");
    await world.clock.advance(500);
    world.WebSocket.instances.at(-1).serverClose(1006, "closed by the daemon");

    assert.equal(world.transport.status().consecutiveFailures, 2);
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

  it("starts the transport even when the privileged half never answers", async () => {
    const clock = fakeClock();
    const browser = fakeBrowser({ pairing: PAIRING });
    const hang = () => new Promise(() => {});
    for (const call of ["keepAlive", "grantOptionalPermission", "appInfo", "availableModules"]) {
      browser.tbx[call] = hang;
    }
    browser.tbx.writeStatus = hang;
    const starts = [];
    loadScript("background/main.js", {
      browser,
      tbxLog: fakeLog(),
      tbxRegistry: { methods: () => ["mail.list"] },
      tbxCapabilities: loadCapabilities({ browser, clock }),
      tbxEvents: { start() {} },
      tbxTransport: {
        start: (identity, options) => starts.push({ identity, options }),
        stop() {},
      },
      console: quietConsole(),
      Date,
    });

    await settle(); // main.js gets as far as its first bounded call
    await clock.advance(30000);

    assert.equal(starts.length, 1, "the transport never started");
    assert.deepEqual(plain(starts[0].identity), { app: null, capabilities: null });
  });

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

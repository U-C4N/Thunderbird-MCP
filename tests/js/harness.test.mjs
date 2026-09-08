/* The harness testing itself.
 *
 * If loadScript ever stops exposing an add-on script's top-level `var`s, every
 * other test in this directory fails in a way that looks like a bug in the
 * add-on. This one test says otherwise.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { fakeBrowser, fakeClock, fakeLog, fakeWebSocketClass, loadScript } from "./harness.mjs";

describe("loadScript", () => {
  it("exposes a script's top-level var and runs it against the given globals", () => {
    const calls = [];
    const ctx = loadScript("background/log.js", {
      browser: fakeBrowser(),
      console: { info: (...args) => calls.push(args) },
    });

    ctx.tbxLog.info("x");

    assert.deepEqual(calls, [["[tbmcp]", "x"]]);
  });
});

describe("fakeLog", () => {
  it("records every level as flat text", () => {
    const log = fakeLog();

    log.warn("port", 4711, "is busy");

    assert.deepEqual(log.records, [{ level: "warn", text: "port 4711 is busy" }]);
  });
});

describe("fakeClock", () => {
  it("fires due timers in order and lets async handlers settle", async () => {
    const clock = fakeClock();
    const order = [];
    clock.setTimeout(async () => {
      order.push("late");
    }, 20);
    clock.setTimeout(() => order.push("early"), 10);

    await clock.advance(9);
    assert.deepEqual(order, []);

    await clock.advance(11);
    assert.deepEqual(order, ["early", "late"]);
    assert.equal(clock.now(), 20);
  });

  it("forgets a cleared timer and repeats an interval", async () => {
    const clock = fakeClock();
    const ticks = [];
    const cancelled = clock.setTimeout(() => ticks.push("never"), 5);
    clock.clearTimeout(cancelled);
    const interval = clock.setInterval(() => ticks.push("tick"), 10);

    await clock.advance(25);
    clock.clearInterval(interval);
    await clock.advance(25);

    assert.deepEqual(ticks, ["tick", "tick"]);
  });
});

describe("fakeWebSocketClass", () => {
  it("records what the add-on sends and replays what the daemon does", () => {
    const WebSocket = fakeWebSocketClass();
    const events = [];
    const ws = new WebSocket("ws://127.0.0.1:1/tbmcp");
    ws.onopen = () => events.push("open");
    ws.onmessage = (event) => events.push(JSON.parse(event.data).t);
    ws.onclose = (event) => events.push(`close ${event.code} ${event.reason}`);

    assert.equal(WebSocket.instances[0], ws);
    assert.equal(ws.readyState, WebSocket.CONNECTING);
    ws.open();
    assert.equal(ws.readyState, WebSocket.OPEN);
    ws.send(JSON.stringify({ t: "hello" }));
    ws.receive({ t: "welcome" });
    ws.serverClose(1006, "gone");

    assert.deepEqual(ws.sent, [{ t: "hello" }]);
    assert.deepEqual(events, ["open", "welcome", "close 1006 gone"]);
    assert.equal(ws.readyState, WebSocket.CLOSED);
  });

  it("records close() and reports it back through onclose", () => {
    const WebSocket = fakeWebSocketClass();
    const seen = [];
    const ws = new WebSocket("ws://127.0.0.1:1/tbmcp");
    ws.onclose = (event) => seen.push(event.code);

    ws.close(4000, "no welcome");

    assert.deepEqual(ws.closeCalls, [{ code: 4000, reason: "no welcome" }]);
    assert.deepEqual(seen, [4000]);
  });
});

describe("fakeBrowser", () => {
  it("answers the calls the add-on makes at startup", async () => {
    const browser = fakeBrowser({ pairing: { version: 1, port: 4711, token: "t" } });

    assert.equal(browser.runtime.getManifest().version, "9.9.9");
    assert.deepEqual(await browser.tbx.readBridgeFile(), { version: 1, port: 4711, token: "t" });
    await browser.tbx.writeStatus({ ok: true });
    assert.deepEqual(browser._written, [{ ok: true }]);
  });

  it("calls a pairing function so a test can control when the read finishes", async () => {
    let resolve;
    const browser = fakeBrowser({ pairing: () => new Promise((r) => (resolve = r)) });
    const pending = browser.tbx.readBridgeFile();
    resolve({ version: 1, port: 1, token: "t" });
    assert.equal((await pending).port, 1);
  });
});

/* The harness testing itself.
 *
 * If loadScript ever stops exposing an add-on script's top-level `var`s, every
 * other test in this directory fails in a way that looks like a bug in the
 * add-on. This one test says otherwise.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  fakeBrowser,
  fakeClock,
  fakeLog,
  fakeMessages,
  fakeWebSocketClass,
  loadScript,
} from "./harness.mjs";

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

describe("fakeMessages", () => {
  const FOLDER = "account1://Inbox";

  /** `count` headers, newest first, the way Thunderbird hands them over. */
  function sample(count, folderId = FOLDER) {
    return Array.from({ length: count }, (_, index) => ({
      id: 100 + index,
      headerMessageId: `<m${index}@example.invalid>`,
      subject: `Re: item ${index}`,
      author: "Ada <ada@example.invalid>",
      recipients: ["bob@example.invalid"],
      date: 1700000000000 - index * 60000,
      read: false,
      flagged: false,
      junk: false,
      tags: [],
      size: 1024,
      folder: { id: folderId, path: "/Inbox" },
    }));
  }

  it("pages a folder and leaves the id off the last page", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(12) }, pageSize: 10 });

    const first = await messages.list(FOLDER, { sortType: "date", sortOrder: "descending" });
    assert.equal(first.messages.length, 10);
    assert.ok(first.id, "more pages remain, so the list stays open");

    const last = await messages.continueList(first.id);
    assert.equal(last.messages.length, 2);
    assert.equal(last.id, undefined, "the last page carries no id");

    await assert.rejects(messages.continueList(first.id), /Unknown or expired list/);
  });

  it("answers a query with a bare list id when the query asks for one", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(12) }, queryPageSize: 10 });

    const id = await messages.query({ subject: "Re", returnMessageListId: true });
    assert.equal(typeof id, "string", "returnMessageListId replaces the page with its id");

    const first = await messages.continueList(id);
    assert.equal(first.messages.length, 10, "the id is still on its first page");
    assert.deepEqual(
      (await messages.query({ subject: "item 11" })).messages.map((m) => m.id),
      [111],
      "subject is a case-sensitive substring match"
    );
  });

  it("forgets an aborted list and records every call", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(12) }, pageSize: 10 });

    const first = await messages.list(FOLDER, {});
    await messages.abortList(first.id);

    await assert.rejects(messages.continueList(first.id), /Unknown or expired list/);
    assert.deepEqual(
      messages.calls.map((call) => call.method),
      ["list", "abortList", "continueList"]
    );
  });
});

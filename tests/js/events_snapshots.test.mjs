import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { fakeLog, loadScript, runInContext } from "./harness.mjs";

function events(pages = {}) {
  const listeners = {}, emitted = [];
  const messages = {};
  for (const name of ["onMoved", "onCopied", "onDeleted", "onUpdated", "onNewMailReceived"]) {
    messages[name] = { addListener(fn) { listeners[name] = fn; } };
  }
  messages.continueList = async (id) => {
    if (!pages[id]) throw new Error("list expired");
    return pages[id];
  };
  messages.abortList = async () => {};
  const ctx = loadScript("background/events.js", { browser: { messages }, tbxLog: fakeLog(), setTimeout, clearTimeout,
    tbxTransport: { emit(name, data) { emitted.push({ name, data }); } } });
  ctx.tbxEvents.start();
  return { ...ctx, listeners, emitted };
}
const plain = (v) => JSON.parse(JSON.stringify(v));
describe("paged Thunderbird events", () => {
  it("normalizes all moved pages for subscribers and keeps compact public data", async () => {
    const e = events({ before: { id: null, messages: [{ id: 2 }] }, after: { id: null, messages: [{ id: 12 }] } });
    let internal;
    e.tbxEvents.subscribe("messages.onMoved", (data) => { internal = data; });
    await e.listeners.onMoved({ id: "before", messages: [{ id: 1 }] }, { id: "after", messages: [{ id: 11 }] });
    assert.deepEqual(plain(internal.originals).map((m) => m.id), [1, 2]);
    assert.deepEqual(plain(internal.moved).map((m) => m.id), [11, 12]);
    assert.equal(internal.complete, true);
    assert.equal(e.emitted[0].data.count, 2);
    assert.equal(e.emitted[0].data.countExact, true);
  });
  it("counts deleted/new messages across pages and does not assume arrays", async () => {
    const e = events({ more: { id: null, messages: [{ id: 2 }] } });
    await e.listeners.onDeleted({ id: "more", messages: [{ id: 1 }] });
    await e.listeners.onNewMailReceived({ id: "inbox" }, { id: "more", messages: [{ id: 1 }] });
    assert.deepEqual(e.emitted.map((x) => x.data.count), [2, 2]);
  });
  it("reports incomplete normalization and supports unsubscribe", async () => {
    const e = events();
    let calls = 0;
    const off = e.tbxEvents.subscribe("messages.onDeleted", () => { calls++; });
    await e.listeners.onDeleted({ id: "missing", messages: [{ id: 1 }] });
    assert.equal(e.emitted[0].data.countExact, false);
    assert.match(e.emitted[0].data.partialReason, /expired/);
    off();
    await e.listeners.onDeleted({ id: null, messages: [] });
    assert.equal(calls, 1);
  });
});

function snapshots(now = { value: 1000000 }) {
  class ClockDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now.value])); }
    static now() { return now.value; }
  }
  const ctx = loadScript("background/registry.js", { Date: ClockDate, tbxRuntimeEpoch: "session" });
  runInContext(ctx, "background/snapshots.js");
  return ctx.tbxSnapshots;
}
describe("read snapshots", () => {
  it("freezes rows, supports repeatable cursors and prevents cross-tool use", () => {
    const store = snapshots(), rows = [{ id: 1 }, { id: 2 }, { id: 3 }];
    const first = store.begin("search", rows, { engine: "global_index" }, 1);
    rows[1].id = 999;
    const next = store.page("search", first.nextCursor, 1);
    assert.equal(next.items[0].id, 2);
    assert.deepEqual(plain(store.page("search", first.nextCursor, 1)), plain(next));
    assert.throws(() => store.page("followups", first.nextCursor), /expired/);
  });
  it("expires after five minutes and evicts the oldest of sixteen snapshots", () => {
    const now = { value: 1000000 }, store = snapshots(now);
    const first = store.begin("s", [1, 2], {}, 1);
    now.value += 300000;
    assert.throws(() => store.page("s", first.nextCursor), /expired/);
    const next = store.begin("s", [1, 2], {}, 1);
    for (let i = 0; i < 16; i++) store.begin("s", [1], {}, 1);
    assert.throws(() => store.page("s", next.nextCursor), /expired/);
  });
  it("rejects oversized snapshots", () => {
    const store = snapshots();
    assert.throws(() => store.begin("s", Array(1001).fill(1), {}, 1), /1000/);
    assert.throws(() => store.begin("s", ["x".repeat(17 * 1024 * 1024)], {}, 1), /32 MiB/);
  });
});

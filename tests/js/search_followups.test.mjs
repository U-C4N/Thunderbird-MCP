import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { loadScript, runInContext } from "./harness.mjs";
const plain = (v) => JSON.parse(JSON.stringify(v));
function search(raw) {
  const calls = [];
  const browser = { tbx: { async invoke(method, params) { calls.push({ method, params }); return raw; } } };
  const ctx = loadScript("background/registry.js", { browser, tbxRuntimeEpoch: "search" });
  runInContext(ctx, "background/snapshots.js");
  runInContext(ctx, "background/handlers/search.js");
  return { call: (method, params) => ctx.tbxRegistry.invoke(method, params), calls };
}
describe("search snapshot wrappers", () => {
  it("uses one bounded search then serves immutable continuation pages", async () => {
    const raw = { hits: [{ id: 1 }, { id: 2 }, { id: 3 }], matched: 3, retrieved: 3, indexEnabled: true };
    const s = search(raw);
    const first = await s.call("search.global", { query: "invoice", limit: 1 });
    raw.hits[1].id = 999;
    const next = await s.call("search.global", { cursor: first.nextCursor, limit: 2 });
    assert.deepEqual(plain(next.items).map((m) => m.id), [2, 3]);
    assert.equal(s.calls.length, 1);
    assert.equal(s.calls[0].params.limit, 1000);
    assert.equal(s.calls[0].params.snapshot, true);
    assert.equal(next.hasMore, false);
    assert.equal(next.coverage, "indexed_only");
  });
  it("suggests an explicit literal subject alternative without executing it", async () => {
    const s = search({ hits: [], matched: 0, retrieved: 0, indexEnabled: false, unmatchableTerms: ["2.0"] });
    const out = await s.call("search.global", { query: "2.0", folderId: "a://Inbox" });
    assert.equal(out.alternativeSearch.parameters.subject, "2.0");
    assert.equal(out.alternativeSearch.semanticsChanged, true);
    assert.deepEqual(plain(out.partialReasons), ["index_disabled", "unmatchable_terms"]);
    assert.equal(s.calls.length, 1);
  });
  it("does not claim a total for a cut-off global search", async () => {
    const s = search({ hits: [], matched: 0, retrieved: 1000, indexEnabled: true, truncated: true });
    const out = await s.call("search.global", { query: "invoice" });
    assert.equal("totalAvailable" in out, false);
    assert.deepEqual(plain(out.partialReasons), ["candidate_limit"]);
  });
  it("pages conversations and rejects query changes with a cursor", async () => {
    const s = search({ messages: [{ id: 1 }, { id: 2 }], total: 2, conversationId: 12 });
    const out = await s.call("search.conversation", { messageId: 1, limit: 1 });
    assert.equal((await s.call("search.conversation", { cursor: out.nextCursor })).items[0].id, 2);
    await assert.rejects(s.call("search.conversation", { cursor: out.nextCursor, messageId: 1 }), /only cursor/);
  });
});
const NOW = Date.UTC(2026, 9, 31, 12), DAY = 86400000;
function row(id, days, author = "Me <me@example.com>") {
  return { id, headerMessageId: `<m${id}@example.com>`, date: new Date(NOW - days * DAY).toISOString(),
    author, recipients: "Other <other@example.com>", subject: "Question", folderId: "account1://Sent" };
}
function followups({ rows = [row(1, 10)], headers = {}, stats = { enabled: true, available: true }, total, folderType = "sent" } = {}) {
  class ClockDate extends Date { constructor(...args) { super(...(args.length ? args : [NOW])); } static now() { return NOW; } }
  const calls = [];
  const browser = {
    folders: { async get(id) { return { id, type: folderType }; } },
    accounts: { async list() { return [{ id: "account1", identities: [{ email: "me@example.com" }, { email: "alias@example.com" }] }]; } },
    messages: {
      async query(q) { calls.push(q); return { id: null, messages: [rows[0]] }; },
      async abortList() {},
      async getHeaders(id) { if (headers[id] instanceof Error) throw headers[id]; return headers[id] || { to: ["other@example.com"] }; },
    },
    tbx: { async invoke(method) {
      if (method === "gloda.stats") return stats;
      return { conversationId: 15, messages: rows, total: total ?? rows.length, subject: "Question" };
    } },
  };
  const ctx = loadScript("background/registry.js", { browser, Date: ClockDate, setTimeout, clearTimeout, tbxRuntimeEpoch: "followups" });
  runInContext(ctx, "background/snapshots.js");
  runInContext(ctx, "background/handlers/followups.js");
  return { call: (params = {}) => ctx.tbxRegistry.invoke("messages.followups", params), calls };
}
describe("follow-up evidence", () => {
  it("returns the source message and explanation for an unanswered conversation", async () => {
    const out = await followups().call();
    assert.equal(out.items.length, 1);
    assert.equal(out.items[0].messageId, 1);
    assert.equal(out.items[0].waitingDays, 10);
    assert.deepEqual(plain(out.items[0].recipients), ["other@example.com"]);
    assert.equal(out.coverage, "indexed_only");
  });
  it("a linked human response closes the thread", async () => {
    const out = await followups({ rows: [row(1, 10), row(2, 8, "other@example.com")], headers: { 2: { "in-reply-to": ["<m1@example.com>"] } } }).call();
    assert.equal(out.items.length, 0);
    assert.equal(out.replied, 1);
  });
  it("an automatic response does not close the thread", async () => {
    const out = await followups({ rows: [row(1, 10), row(2, 8, "other@example.com")], headers: { 2: { "auto-submitted": ["auto-replied"], "in-reply-to": ["<m1@example.com>"] } } }).call();
    assert.equal(out.items.length, 1);
  });
  it("does not guess from an unlinked human response or a truncated thread", async () => {
    const out = await followups({ rows: [row(1, 10), row(2, 8, "other@example.com")] }).call();
    assert.equal(out.items.length, 0);
    assert.equal(out.unknown, 1);
    assert.ok(out.partialReasons.includes("unlinked_reply"));
    const cut = await followups({ total: 5000 }).call();
    assert.equal(cut.items.length, 0);
    assert.ok(cut.partialReasons.includes("incomplete_conversation"));
  });
  it("excludes mailing lists, self-mail and recently reopened conversations", async () => {
    assert.equal((await followups({ headers: { 1: { "list-id": ["list.example.com"] } } }).call()).items.length, 0);
    const self = row(1, 10); self.recipients = "alias@example.com";
    assert.equal((await followups({ rows: [self], headers: { 1: { to: ["alias@example.com"] } } }).call()).items.length, 0);
    assert.equal((await followups({ rows: [row(1, 10), row(3, 1, "alias@example.com")] }).call()).items.length, 0);
  });
  it("reports missing index and unavailable headers without false candidates", async () => {
    const absent = followups({ stats: { enabled: false, available: true } });
    const out = await absent.call();
    assert.equal(out.items.length, 0);
    assert.equal(absent.calls.length, 0);
    assert.ok(out.partialReasons.includes("index_unavailable"));
    const unavailable = await followups({ headers: { 1: new Error("offline") } }).call();
    assert.equal(unavailable.items.length, 0);
    assert.equal(unavailable.unknown, 1);
  });
  it("excludes old drafts, queued mail and templates while retaining archived sent mail", async () => {
    for (const folderType of ["drafts", "outbox", "templates"]) {
      const result = await followups({ folderType }).call();
      assert.equal(result.items.length, 0, folderType);
      assert.equal(result.excluded, 1);
    }
    const archived = await followups({ folderType: "archives" }).call();
    assert.equal(archived.items.length, 1);
    const movedDraft = await followups({ folderType: "archives", headers: { 1: { "x-mozilla-draft-info": ["internal/draft; vcard=0"] } } }).call();
    assert.equal(movedDraft.items.length, 0);
  });
});

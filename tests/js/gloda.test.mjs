/* Gloda — the global index, and the four ways a search of it came back empty.
 *
 * Every defect here was invisible in a unit test that built its own Date or its
 * own folder id, and obvious against a real profile: gloda hands back objects
 * from another realm, WebExtension folder ids look like URIs, and a term the
 * tokenizer throws away still reaches the SQL and zeroes the query.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import vm from "node:vm";

import { fakeGlodaSearcherClass, fakeSandbox, loadExperiment } from "./harness.mjs";

const ACCOUNTS = [
  { key: "account1", incomingServer: { rootFolder: { URI: "imap://me@example.com" } } },
];
const FOLDER_URI = "imap://me@example.com/INBOX";

/** How many rows `gloda.search` over-fetches for a default, unscoped query. */
const RETRIEVE = 75;

/** Values built inside the vm carry that realm's prototypes; strip them. */
function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

/** `count` indexed messages, newest first, as the searcher hands them over. */
function corpus(count, { folderUri = FOLDER_URI } = {}) {
  return Array.from({ length: count }, (_, index) => ({
    id: 1000 + index,
    headerMessageID: `<g${index}@example.invalid>`,
    subject: `Item ${index}`,
    date: new Date(1700000000000 - index * 60000),
    folderURI: folderUri,
    conversationID: 500,
    folderMessage: null,
    from: { value: `ada${index}@example.invalid`, contact: null },
    to: [],
    involves: [],
  }));
}

/**
 * The gloda module, loaded over a fixed index.
 *
 * `call` reaches a handler through the dispatch table rather than through
 * `invoke`, so a failure arrives as the error gloda raised instead of the wire
 * envelope core.js packs it into — that envelope is core.test.mjs's business.
 */
function experiment({ messages = [], accounts = ACCOUNTS, prefs = {}, modules = {} } = {}) {
  const globals = fakeSandbox({
    prefs: { "mailnews.database.global.indexer.enabled": true, ...prefs },
    modules: {
      "resource:///modules/MailServices.sys.mjs": { MailServices: { accounts: { accounts } } },
      "resource:///modules/gloda/GlodaMsgSearcher.sys.mjs": {
        GlodaMsgSearcher: fakeGlodaSearcherClass({ corpus: messages }),
      },
      ...modules,
    },
  });
  const ctx = loadExperiment(globals, { modules: ["gloda"] });
  return {
    gloda: ctx.TBX_TEST_HOOKS.gloda,
    call: (method, params) => ctx.TBX_TEST_HOOKS.core.handlers[method](params || {}),
  };
}

/** The gloda module's pure helpers, with `accounts` as MailServices reports them. */
function hooks(options = {}) {
  return experiment(options).gloda;
}

describe("isoDate", () => {
  it("dates a gloda hit whose Date was minted in another realm", () => {
    const { isoDate } = hooks();

    // `instanceof Date` is false for this one, which is why every hit was null.
    assert.equal(isoDate(vm.runInNewContext("new Date(0)")), "1970-01-01T00:00:00.000Z");
  });

  it("still reads a raw PRTime, and nothing out of nothing", () => {
    const { isoDate } = hooks();

    assert.equal(isoDate(1000000), "1970-01-01T00:00:01.000Z");
    assert.equal(isoDate(null), null);
    assert.equal(isoDate(undefined), null);
  });
});

describe("folderMatcher", () => {
  it("matches a WebExtension folder id against the hit's own id", () => {
    const matches = hooks().folderMatcher("account1://INBOX");

    assert.equal(matches({ folderId: "account1://INBOX" }), true);
    assert.equal(matches({ folderId: "account1://Sent" }), false);
  });

  it("matches a folder URI under an account's root against the hit's URI", () => {
    const matches = hooks().folderMatcher("imap://me@example.com/INBOX");

    assert.equal(matches({ folderUri: "imap://me@example.com/INBOX" }), true);
    assert.equal(matches({ folderUri: "imap://me@example.com/Sent" }), false);
  });

  it("says so when the reference belongs to no account here", () => {
    assert.throws(
      () => hooks().folderMatcher("accountZZ://nope"),
      (ex) => {
        assert.equal(ex.tbxKind, "usage");
        assert.match(ex.message, /accountZZ/);
        return true;
      }
    );
  });
});

describe("classifyTerms", () => {
  it("separates the terms the index can match from the ones it throws away", () => {
    const { classifyTerms } = hooks();

    assert.deepEqual(plain(classifyTerms(["payments", "2.0"])), {
      usable: ["payments"],
      unmatchable: ["2.0"],
    });
  });

  it("finds nothing usable in terms that tokenize to nothing", () => {
    const { classifyTerms } = hooks();

    assert.deepEqual(plain(classifyTerms(["2.0", "-"])).usable, []);
  });

  it("keeps CJK, which the tokenizer indexes one character at a time", () => {
    const { classifyTerms } = hooks();

    assert.deepEqual(plain(classifyTerms(["東京"])).usable, ["東京"]);
  });
});

describe("gloda.search over unmatchable terms", () => {
  it("runs the query and names the terms that cannot pull their weight", async () => {
    const { call } = experiment({ messages: corpus(3) });

    const result = plain(await call("gloda.search", { query: "payments 2.0" }));

    assert.equal(result.matched, 3, "the query still ran");
    assert.deepEqual(result.unmatchableTerms, ["2.0"]);
  });

  it("says nothing about terms when every one of them can match", async () => {
    const { call } = experiment({ messages: corpus(3) });

    const result = plain(await call("gloda.search", { query: "payments" }));

    assert.equal(result.unmatchableTerms, undefined);
  });

  it("explains an empty result the unmatchable term caused", async () => {
    const { call } = experiment();

    const result = plain(await call("gloda.search", { query: "payments 2.0" }));

    assert.equal(
      result.note,
      'The index cannot match "2.0" — it tokenizes into pieces shorter than three ' +
        "characters, and every term has to match. Drop those words and search again, " +
        "or use mail_search with a subject filter."
    );
  });

  it("lets a disabled index keep the last word", async () => {
    const { call } = experiment({
      prefs: { "mailnews.database.global.indexer.enabled": false },
    });

    const result = plain(await call("gloda.search", { query: "payments 2.0" }));

    assert.match(result.note, /global index is disabled/);
  });

  it("still refuses a query with no usable term at all", async () => {
    const { call } = experiment({ messages: corpus(3) });

    await assert.rejects(call("gloda.search", { query: "2.0" }), (ex) => {
      assert.equal(ex.tbxKind, "usage");
      assert.match(ex.message, /no searchable term/);
      return true;
    });
  });
});

describe("gloda.search truncation", () => {
  it("does not call a corpus that exactly fills the retrieval truncated", async () => {
    const { call } = experiment({ messages: corpus(RETRIEVE) });

    const result = plain(await call("gloda.search", { query: "payments" }));

    assert.equal(result.truncated, false);
    assert.equal(result.matched, RETRIEVE);
    assert.equal(result.retrieved, RETRIEVE);
  });

  it("reports a deeper corpus as truncated without counting the probe row", async () => {
    const { call } = experiment({ messages: corpus(RETRIEVE + 5) });

    const result = plain(await call("gloda.search", { query: "payments", limit: 25 }));

    assert.equal(result.truncated, true);
    assert.equal(result.retrieved, RETRIEVE, "the probe row is not a considered row");
    assert.equal(result.hits.length, 25);
  });
});

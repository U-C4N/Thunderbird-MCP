/* messages.query and messages.list, driven the way the daemon drives them.
 *
 * Both walk Thunderbird's MessageList, and both had a defect that only a real
 * mailbox showed: a query that answered with a list id instead of messages, and
 * a cursor that resumed on the page *after* the one it stopped in, silently
 * dropping everything it had not returned. The tests here are those two walks.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { fakeBrowser, fakeMessages, loadScript } from "./harness.mjs";

const FOLDER = "account1://Inbox";

/** `count` headers, newest first, shaped the way Thunderbird hands them over. */
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

/**
 * Load the handler module and hand back what it defined.
 *
 * The real registry.js supplies tbxError and tbxUtil so failures carry the same
 * shape the daemon sees; tbxRegistry is faked only to catch the definitions.
 */
function loadHandlers(messages) {
  const registry = loadScript("background/registry.js");
  const browser = fakeBrowser();
  const handlers = new Map();
  browser.messages = messages;
  loadScript("background/handlers/messages.js", {
    browser,
    tbxError: registry.tbxError,
    tbxUtil: registry.tbxUtil,
    tbxRegistry: {
      define(method, fn) {
        handlers.set(method, fn);
      },
    },
  });
  return handlers;
}

/** Values built inside the vm carry that realm's prototypes; strip them. */
function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

/** The message ids of one page. */
function ids(result) {
  return plain(result.messages).map((message) => message.id);
}

/** Errors cross the vm realm boundary, so match on the wire fields, not instanceof. */
function usageError(pattern) {
  return (ex) => {
    assert.equal(ex.tbxKind, "usage", `wrong error kind: ${ex.message}`);
    assert.match(ex.message, pattern);
    return true;
  };
}

describe("messages.query", () => {
  it("answers with the matching headers, not with a list id", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, queryPageSize: 10 });
    const query = loadHandlers(messages).get("messages.query");

    const result = await query({ query: { subject: "Re" }, limit: 5 });

    assert.equal(result.messages.length, 5);
    assert.deepEqual(ids(result), [100, 101, 102, 103, 104]);
    assert.equal(result.messages[0].subject, "Re: item 0");
    assert.ok(result.cursor, "25 matches are left, so there has to be a cursor");
  });

  it("asks for a page of its own size rather than a bare list id", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, queryPageSize: 10 });
    const query = loadHandlers(messages).get("messages.query");

    await query({ query: { subject: "Re" }, limit: 5 });

    const queryInfo = messages.calls[0].args[0];
    assert.equal(queryInfo.returnMessageListId, undefined);
    assert.equal(queryInfo.messagesPerPage, 5);
  });

  it("resolves a bare list id, for a build that insists on answering with one", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, queryPageSize: 10 });
    // Same fake, forced into the shape returnMessageListId produces.
    const stubborn = {
      ...messages,
      query: (queryInfo) => messages.query({ ...queryInfo, returnMessageListId: true }),
    };
    const query = loadHandlers(stubborn).get("messages.query");

    const result = await query({ query: { subject: "Re" }, limit: 5 });

    assert.deepEqual(ids(result), [100, 101, 102, 103, 104]);
  });

  it("echoes the scope the query named, defaulting to sub-folders included", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, queryPageSize: 10 });
    const query = loadHandlers(messages).get("messages.query");

    const result = await query({ query: { subject: "Re", folderId: FOLDER }, limit: 5 });

    assert.ok(result.scope, "a first page says what it searched");
    assert.deepEqual(plain(result.scope), {
      folderIds: [FOLDER],
      accountIds: null,
      includeSubFolders: true,
    });
  });

  it("keeps the account scope and an explicit includeSubFolders", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, queryPageSize: 10 });
    const query = loadHandlers(messages).get("messages.query");

    const result = await query({
      query: { subject: "Re", accountId: "account1", includeSubFolders: false },
      limit: 5,
    });

    assert.equal(result.messages.length, 5);
    assert.ok(result.scope, "a first page says what it searched");
    assert.deepEqual(plain(result.scope), {
      folderIds: null,
      accountIds: ["account1"],
      includeSubFolders: false,
    });
  });

  it("reports a query that named no folder as unscoped", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, queryPageSize: 10 });
    const query = loadHandlers(messages).get("messages.query");

    const result = await query({ query: { subject: "Re" }, limit: 5 });

    assert.ok(result.scope, "a first page says what it searched");
    assert.deepEqual(plain(result.scope), {
      folderIds: null,
      accountIds: null,
      includeSubFolders: false,
    });
  });

  it("leaves the scope off a continuation page", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, queryPageSize: 10 });
    const query = loadHandlers(messages).get("messages.query");
    const params = { query: { subject: "Re", folderId: FOLDER }, limit: 5 };

    const first = await query(params);
    const next = await query({ ...params, cursor: first.cursor });

    assert.equal(next.messages.length, 5, "the walk carried on");
    assert.equal(next.scope, undefined, "the scope belongs to the first page only");
  });
});

describe("paging", () => {
  /** Walk a handler with its own cursors until it says there is nothing left. */
  async function walk(handler, params) {
    const seen = [];
    const pages = [];
    const cursors = [];
    for (let guard = 0, cursor = null; guard < 200; guard += 1) {
      const result = await handler({ ...params, cursor });
      pages.push(result.messages.length);
      seen.push(...ids(result));
      cursors.push(result.cursor);
      cursor = result.cursor;
      if (!cursor) {
        return { seen, pages, cursors };
      }
    }
    throw new Error("the walk never ran out of cursors");
  }

  it("walks a folder without gaps or duplicates, whatever the limit", async () => {
    for (const limit of [1, 3, 7, 25]) {
      const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, pageSize: 10 });
      const list = loadHandlers(messages).get("messages.list");
      const whole = await list({ folderId: FOLDER, limit: 30 });

      const walked = await walk(list, { folderId: FOLDER, limit });

      assert.equal(walked.seen.length, 30, `limit ${limit} lost messages`);
      assert.deepEqual(walked.seen, ids(whole), `limit ${limit} walked out of order`);
    }
  });

  it("walks a query without gaps or duplicates, whatever the limit", async () => {
    for (const limit of [1, 3, 7, 25]) {
      const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, queryPageSize: 10 });
      const query = loadHandlers(messages).get("messages.query");
      const params = { query: { subject: "Re: item" } };
      const whole = await query({ ...params, limit: 30 });

      const walked = await walk(query, { ...params, limit });

      assert.equal(walked.seen.length, 30, `limit ${limit} lost messages`);
      assert.deepEqual(walked.seen, ids(whole), `limit ${limit} walked out of order`);
    }
  });

  it("returns the short last page and then stops", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(12) }, pageSize: 10 });
    const list = loadHandlers(messages).get("messages.list");

    const walked = await walk(list, { folderId: FOLDER, limit: 5 });

    assert.deepEqual(walked.pages, [5, 5, 2]);
    assert.equal(walked.cursors.at(-1), null);
    assert.match(walked.cursors[0], /^tbx:\d+$/, "a part-read page needs a cursor of ours");
    assert.match(
      walked.cursors[1],
      /^list-/,
      "a page that ended on the limit leaves nothing over, so the list id will do"
    );
  });

  it("evicts the oldest part-read page and aborts the list behind it", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, pageSize: 10 });
    const list = loadHandlers(messages).get("messages.list");
    const cursors = [];
    for (let walks = 0; walks < 33; walks += 1) {
      // Each one stops 3 messages into a 10-message page and is then abandoned.
      cursors.push((await list({ folderId: FOLDER, limit: 3 })).cursor);
    }

    // The fake mints list ids in order, so the first walk's list is "list-1".
    assert.deepEqual(
      messages.calls.filter((call) => call.method === "abortList").map((call) => call.args[0]),
      ["list-1"]
    );
    await assert.rejects(
      () => list({ folderId: FOLDER, limit: 3, cursor: cursors[0] }),
      usageError(/cursor/)
    );
    const newest = await list({ folderId: FOLDER, limit: 3, cursor: cursors.at(-1) });
    assert.equal(newest.messages.length, 3, "the newest walk survived the eviction");
  });

  it("says a raw Thunderbird cursor has expired", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, pageSize: 10 });
    const list = loadHandlers(messages).get("messages.list");

    await assert.rejects(
      () => list({ folderId: FOLDER, limit: 3, cursor: "list-404" }),
      usageError(/expired/)
    );
  });
});

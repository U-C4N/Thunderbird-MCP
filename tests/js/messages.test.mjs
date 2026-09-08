/* messages.query and messages.list, driven the way the daemon drives them.
 *
 * Both walk Thunderbird's MessageList, and both had a defect that only a real
 * mailbox showed: a query that answered with a list id instead of messages, and
 * a cursor that resumed on the page *after* the one it stopped in, silently
 * dropping everything it had not returned. The tests here are those two walks.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { fakeBrowser, fakeLog, fakeMessages, loadScript } from "./harness.mjs";

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
function loadHandlers(messages, browser = fakeBrowser()) {
  const registry = loadScript("background/registry.js");
  const handlers = new Map();
  browser.messages = messages;
  loadScript("background/handlers/messages.js", {
    browser,
    tbxLog: fakeLog(),
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

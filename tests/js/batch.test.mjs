import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { describe, it } from "node:test";

import { loadScript, runInContext } from "./harness.mjs";

const plain = (value) => JSON.parse(JSON.stringify(value));

function fixture(options = {}) {
  const source = { id: "a://Inbox", accountId: "a", path: "/Inbox" };
  const target = { id: "a://Filed", accountId: "a", path: "/Filed" };
  const foreign = { id: "b://Inbox", accountId: "b", path: "/Inbox" };
  const folders = new Map([source, target, foreign].map((folder) => [folder.id, folder]));
  const messages = new Map();
  const listeners = new Map();
  const updates = [];
  const moves = [];
  const cursors = new Map();
  let nextId = 100;
  let nextCursor = 0;
  const add = (id, overrides = {}) => {
    const message = { id, headerMessageId: `<mail${id}@example.invalid>`,
      date: 1700000000000, subject: "Private subject", author: "private@example.invalid",
      recipients: ["recipient@example.invalid"], size: 1234, read: false,
      flagged: false, tags: ["untouched"], folder: source, ...overrides };
    messages.set(id, message);
    return message;
  };
  add(1);
  const events = {
    subscribe(name, listener) {
      const active = listeners.get(name) || new Set();
      active.add(listener);
      listeners.set(name, active);
      return () => active.delete(listener);
    },
    emit(name, data) {
      for (const listener of listeners.get(name) || []) {
        listener(data);
      }
    },
  };
  const browser = {
    tbx: { async invoke() {} },
    folders: {
      async get(id) {
        if (!folders.has(id)) throw new Error("missing folder");
        return plain(folders.get(id));
      },
      async getFolderCapabilities(id) {
        return { canAddMessages: true, canDeleteMessages: options.readOnlySource ? id !== source.id : true };
      },
    },
    messages: {
      async get(id) {
        if (!messages.has(id)) throw new Error("missing message");
        return plain(messages.get(id));
      },
      async query(query) {
        const results = [...messages.values()].filter((message) =>
          message.folder.id === query.folderId && message.headerMessageId === query.headerMessageId);
        const chunk = options.pageSize || 100;
        if (results.length <= chunk) return { id: null, messages: plain(results) };
        const id = `list-${++nextCursor}`;
        cursors.set(id, results.slice(chunk));
        return { id, messages: plain(results.slice(0, chunk)) };
      },
      async continueList(id) {
        const remaining = cursors.get(id);
        const chunk = options.pageSize || 100;
        const page = remaining.splice(0, chunk);
        if (!remaining.length) cursors.delete(id);
        return { id: remaining.length ? id : null, messages: plain(page) };
      },
      async abortList(id) { cursors.delete(id); },
      async listTags() { return [{ key: "todo" }, { key: "untouched" }, { key: "later" }]; },
      async update(id, patch) {
        updates.push({ id, patch: plain(patch) });
        Object.assign(messages.get(id), plain(patch));
        if (options.failUpdate) throw new Error("response lost");
      },
      async move(ids, destination) {
        moves.push({ ids: plain(ids), destination });
        const originals = ids.map((id) => plain(messages.get(id)));
        const moved = originals.map((message) => ({ ...message, id: ++nextId, folder: folders.get(destination) }));
        if (!options.copyInstead) ids.forEach((id) => messages.delete(id));
        moved.forEach((message) => messages.set(message.id, message));
        if (options.duplicateDestination) {
          const duplicate = { ...moved[0], id: ++nextId };
          messages.set(duplicate.id, duplicate);
        }
        if (options.failMove) throw new Error("response lost");
        if (options.suppressEvent) return;
        events.emit(options.copyInstead ? "messages.onCopied" : "messages.onMoved", {
          originals, ...(options.copyInstead ? { copied: plain(moved) } : { moved: plain(moved) }),
          complete: !options.incompleteEvent,
        });
      },
    },
  };
  const context = loadScript("background/registry.js", { browser, crypto: webcrypto,
    TextEncoder, setTimeout, clearTimeout, tbxRuntimeEpoch: "epoch-a", tbxEvents: events });
  context.tbxRegistry.define("x.batch.move", async (params) => {
    if (options.pendingNativeHeader) {
      throw context.tbxError.blocked("Pending IMAP header", [], { code: "REFERENCE_PENDING" });
    }
    if (params.checkOnly) return { supported: true };
    await browser.messages.move([params.messageId], params.destination.id);
    if (options.nativeProof) return { completed: true, nativeMoveObserved: {
      messageId: params.messageId, sourceFolderId: params.reference.folderId,
      destinationFolderId: params.destination.id,
    } };
  });
  runInContext(context, "background/references.js");
  runInContext(context, "background/handlers/batch.js");
  const invoke = (method, params) => context.tbxRegistry.invoke(method, params);
  const preview = (params = {}) => invoke("messages.batchPreview", { messageIds: [1], action: "mark",
    changes: { read: true }, ...params });
  const step = (item) => invoke("messages.batchStep", { operationId: "op-1", itemId: "item-1", item });
  const undo = (item, previousResult) => invoke("messages.batchUndo", {
    operationId: "undo-1", itemId: "item-1", item, previousResult });
  return { context, browser, messages, folders, source, target, foreign, add, preview,
    invoke, step, undo, updates, moves, listeners, cursors };
}

describe("persistent references", () => {
  it("stores digests rather than subject/address data and resolves across a new epoch", async () => {
    const f = fixture();
    const reference = await f.context.tbxReferences.capture(1);
    assert.ok(!JSON.stringify(reference).includes("Private subject"));
    assert.ok(!JSON.stringify(reference).includes("private@example.invalid"));
    const message = f.messages.get(1);
    f.messages.delete(1);
    f.messages.set(77, { ...message, id: 77 });
    f.add(1, { subject: "An unrelated reused id" });
    f.context.tbxRuntimeEpoch = "epoch-b";
    const resolved = await f.context.tbxReferences.resolve(reference);
    assert.equal(resolved.id, 77);
  });

  it("refuses missing RFC ids and indistinguishable duplicate messages", async () => {
    const f = fixture();
    f.messages.get(1).headerMessageId = "";
    await assert.rejects(f.context.tbxReferences.capture(1), (error) => error.code === "REFERENCE_NO_MESSAGE_ID");
    f.messages.get(1).headerMessageId = "<duplicate@example.invalid>";
    f.messages.set(2, { ...f.messages.get(1), id: 2 });
    await assert.rejects(f.context.tbxReferences.capture(1), (error) => error.code === "REFERENCE_AMBIGUOUS");
  });

  it("walks repeated list ids through all pages instead of treating them as cycles", async () => {
    const f = fixture({ pageSize: 1 });
    f.add(2, { headerMessageId: f.messages.get(1).headerMessageId, subject: "different" });
    f.add(3, { headerMessageId: f.messages.get(1).headerMessageId, subject: "different again" });
    const reference = await f.context.tbxReferences.capture(1);
    assert.equal((await f.context.tbxReferences.resolve(reference)).id, 1);
    assert.equal(f.cursors.size, 0);
  });

  it("refuses a renamed folder or a changed message", async () => {
    const f = fixture();
    const reference = await f.context.tbxReferences.capture(1);
    f.messages.get(1).subject = "Edited";
    await assert.rejects(f.context.tbxReferences.resolve(reference), (error) => error.code === "REFERENCE_NOT_FOUND");
    f.folders.get(f.source.id).path = "/Renamed";
    await assert.rejects(f.context.tbxReferences.resolve(reference), (error) => error.code === "REFERENCE_FOLDER_CHANGED");
  });
});

describe("frozen batch preview", () => {
  it("keeps live samples outside persisted items and makes no mutation", async () => {
    const f = fixture();
    const result = await f.preview({ messageIds: [1, 1] });
    assert.deepEqual(plain(result.counts), { requested: 1, eligible: 1, invalid: 0 });
    assert.equal(result.preview[0].subject, "Private subject");
    assert.ok(!JSON.stringify(result.items).includes("Private subject"));
    assert.ok(!JSON.stringify(result.items).includes("private@example.invalid"));
    assert.deepEqual(f.updates, []);
  });

  it("reports missing messages and rejects mixed actions and oversized input", async () => {
    const f = fixture();
    assert.equal((await f.preview({ messageIds: [1, 9] })).invalid.length, 1);
    await assert.rejects(f.preview({ action: "move", destinationFolderId: f.target.id }), /cannot also change/);
    await assert.rejects(f.preview({ messageIds: Array.from({ length: 501 }, (_, i) => i) }), /at most 500/);
    await assert.rejects(f.preview({ changes: { addTags: ["todo"], removeTags: ["todo"] } }), /both added/);
    await assert.rejects(f.preview({ changes: { addTags: ["unknown"] } }), /does not exist/);
  });

  it("accepts the 500-message boundary and limits live samples to ten", async () => {
    const f = fixture();
    for (let id = 2; id <= 500; id += 1) f.add(id);
    const result = await f.preview({ messageIds: Array.from({ length: 500 }, (_, index) => index + 1) });
    assert.equal(result.items.length, 500);
    assert.equal(result.preview.length, 10);
    assert.equal(result.invalid.length, 0);
    assert.equal(f.updates.length, 0);
  });

  it("refuses cross-account destinations and sources that would copy instead", async () => {
    const f = fixture();
    const foreign = await f.preview({ action: "move", changes: null, destinationFolderId: f.foreign.id });
    assert.equal(foreign.invalid[0].code, "CROSS_ACCOUNT_MOVE");
    const denied = fixture({ readOnlySource: true });
    const result = await denied.preview({ action: "move", changes: null, destinationFolderId: denied.target.id });
    assert.equal(result.invalid[0].code, "FOLDER_CAPABILITY");
  });
});

describe("guarded mark and undo", () => {
  it("restores touched fields while preserving unrelated tags added later", async () => {
    const f = fixture();
    const { items: [item] } = await f.preview({ changes: { read: true, addTags: ["todo"] } });
    const result = await f.step(item);
    assert.equal(result.status, "succeeded");
    f.messages.get(1).tags.push("later");
    const undone = await f.undo(item, result);
    assert.equal(undone.status, "succeeded");
    assert.equal(f.messages.get(1).read, false);
    assert.deepEqual(f.messages.get(1).tags, ["untouched", "later"]);
  });

  it("does not overwrite a touched-field change since preview or before undo", async () => {
    const f = fixture();
    const { items: [item] } = await f.preview();
    f.messages.get(1).read = true;
    assert.equal((await f.step(item)).status, "conflict");
    assert.equal(f.updates.length, 0);
    f.messages.get(1).read = false;
    const result = await f.step(item);
    f.messages.get(1).read = false;
    assert.equal((await f.undo(item, result)).status, "conflict");
    assert.equal(f.updates.length, 1);
  });

  it("classifies a lost mutation response as unknown and never guesses undo", async () => {
    const f = fixture({ failUpdate: true });
    const { items: [item] } = await f.preview();
    const result = await f.step(item);
    assert.equal(result.status, "unknown");
    assert.equal(f.messages.get(1).read, true);
    assert.equal((await f.undo(item, result)).code, "NOT_UNDOABLE");
    assert.equal(f.updates.length, 1);
  });

  it("records no-op items and does not undo a later user change", async () => {
    const f = fixture();
    const { items: [item] } = await f.preview({ changes: { read: false } });
    const result = await f.step(item);
    assert.equal(result.changed, false);
    f.messages.get(1).read = true;
    assert.equal((await f.undo(item, result)).changed, false);
    assert.equal(f.messages.get(1).read, true);
    assert.equal(f.updates.length, 0);
  });
});

describe("guarded moves", () => {
  it("uses prebound native correlation when TB128 omits the WebExtension move event", async () => {
    const f = fixture({ nativeProof: true, suppressEvent: true });
    const { items: [item] } = await f.preview({ action: "move", changes: null, destinationFolderId: f.target.id });
    const result = await f.step(item);
    assert.equal(result.status, "succeeded");
    assert.equal(result.reference.folderId, f.target.id);
    assert.equal((await f.undo(item, result)).status, "succeeded");
    assert.equal(f.messages.size, 1);
    assert.ok([...f.listeners.values()].every((listeners) => listeners.size === 0));
  });

  it("keeps an ambiguous post-dispatch native mapping unknown rather than guessing a duplicate", async () => {
    const f = fixture({ nativeProof: true, suppressEvent: true, duplicateDestination: true });
    const { items: [item] } = await f.preview({ action: "move", changes: null, destinationFolderId: f.target.id });
    const result = await f.step(item);
    assert.equal(result.status, "unknown");
    assert.equal(f.moves.length, 1);
    assert.equal(f.messages.size, 2);
  });

  it("rejects missing native support before creating a move plan", async () => {
    const f = fixture();
    delete f.browser.tbx;
    await assert.rejects(f.preview({ action: "move", changes: null, destinationFolderId: f.target.id }),
      (error) => error.code === "UNSUPPORTED_CAPABILITY");
    assert.equal(f.moves.length, 0);
  });

  it("refuses a native pseudo header both at preview and immediately before dispatch", async () => {
    const options = {};
    const f = fixture(options);
    const { items: [item] } = await f.preview({ action: "move", changes: null, destinationFolderId: f.target.id });
    options.pendingNativeHeader = true;
    const result = await f.step(item);
    assert.equal(result.status, "conflict");
    assert.equal(result.code, "REFERENCE_PENDING");
    const preview = await f.preview({ action: "move", changes: null, destinationFolderId: f.target.id });
    assert.equal(preview.invalid[0].code, "REFERENCE_PENDING");
    assert.equal(f.moves.length, 0);
  });

  it("tracks changed ids and moves the confirmed message back on undo", async () => {
    const f = fixture();
    const { items: [item] } = await f.preview({ action: "move", changes: null, destinationFolderId: f.target.id });
    const result = await f.step(item);
    assert.equal(result.status, "succeeded");
    assert.notEqual(result.reference.messageId, 1);
    assert.equal(result.reference.folderId, f.target.id);
    const undone = await f.undo(item, result);
    assert.equal(undone.status, "succeeded");
    assert.equal(undone.reference.folderId, f.source.id);
    assert.equal(f.messages.size, 1);
    assert.ok([...f.listeners.values()].every((listeners) => listeners.size === 0));
  });

  it("does not call a copy a move and leaves it for explicit recovery", async () => {
    const f = fixture({ copyInstead: true });
    const { items: [item] } = await f.preview({ action: "move", changes: null, destinationFolderId: f.target.id });
    const result = await f.step(item);
    assert.equal(result.status, "unknown");
    assert.equal(result.code, "UNEXPECTED_COPY");
    assert.equal(f.messages.size, 2);
    assert.equal((await f.undo(item, result)).code, "NOT_UNDOABLE");
    assert.equal(f.moves.length, 1);
  });

  it("reports incomplete events and transport exceptions as unknown", async () => {
    for (const options of [{ incompleteEvent: true }, { failMove: true }]) {
      const f = fixture(options);
      const { items: [item] } = await f.preview({ action: "move", changes: null, destinationFolderId: f.target.id });
      assert.equal((await f.step(item)).status, "unknown");
      assert.equal(f.moves.length, 1);
    }
  });

  it("refuses undo when the user moved the message elsewhere", async () => {
    const f = fixture();
    const { items: [item] } = await f.preview({ action: "move", changes: null, destinationFolderId: f.target.id });
    const result = await f.step(item);
    f.messages.get(result.reference.messageId).folder = f.foreign;
    assert.equal((await f.undo(item, result)).status, "conflict");
    assert.equal(f.moves.length, 1);
  });
});

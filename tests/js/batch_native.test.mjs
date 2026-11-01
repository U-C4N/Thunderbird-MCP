import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { describe, it } from "node:test";
import { loadScript } from "./harness.mjs";

function fixture(options = {}) {
  const source = { URI: "imap://fixture/INBOX", isServer: false, flags: 0,
    canDeleteMessages: true, server: { type: "imap" }, accountId: "a", path: "/INBOX" };
  const destination = { URI: "imap://fixture/Filed", isServer: false, flags: 0,
    canFileMessages: true, server: source.server, accountId: options.foreign ? "b" : "a", path: "/Filed" };
  const converted = { author: "fixture@example.invalid", bccList: [], ccList: [], date: 1700000000000,
    headerMessageId: "message@example.invalid", recipients: ["reader@example.invalid"], size: 100, subject: "İstanbul — Türkçe özet" };
  const fingerprint = createHash("sha256").update(JSON.stringify(converted)).digest("hex");
  const header = { folder: source, messageKey: 7, messageId: converted.headerMessageId, flags: 0,
    getUint32Property(name) { return name === "pseudoHdr" && options.pseudo ? 1 : 0; } };
  source.msgDatabase = { containsKey: () => !options.stale,
    getMsgHdrForKey: () => header };
  const calls = [];
  const listeners = new Set();
  let refreshes = 0;
  destination.QueryInterface = () => ({ updateFolderWithListener(_window, listener) {
    refreshes++;
    if (options.refreshInProgress) {
      for (const observer of listeners) observer.msgAdded({ ...header, folder: destination, messageKey: 23 });
    } else listener.OnStopRunningUrl(null, options.refreshFailure ? 1 : 0);
  } });
  let native;
  native = loadScript("experiment/modules/batch.js", {
    TBX_MODULE_NAMES: [], TBX_MODULES: {},
    Cu: { importGlobalProperties(names) {
      assert.deepEqual(Array.from(names), ["TextEncoder"]);
      native.TextEncoder = TextEncoder;
    } },
    Services: { io: { offline: Boolean(options.offline) } },
    H: {
      need(params, key) { if (params[key] == null) throw new Error("required"); return params[key]; },
      blocked(message, _needs, extra = {}) { return Object.assign(new Error(message), extra); },
      unsupported: (message) => new Error(message), usage: (message) => new Error(message),
      withTimeout: (promise) => promise,
    },
    needMod(name) {
      assert.equal(name, "MailServices");
      return {
        mfn: { msgsMoveCopyCompleted: 1, msgAdded: 2, addListener(listener) { listeners.add(listener); },
          removeListener(listener) { listeners.delete(listener); } },
        copy: { copyMessages(...args) {
          assert.equal(listeners.size, 1, "native listener must be bound before dispatch");
          calls.push(args);
          const original = options.deletedOriginal ? { ...header, messageId: "" } : header;
          for (const listener of listeners) listener.msgsMoveCopyCompleted(!options.copyInstead, [original], destination, []);
        } },
      };
    },
    ChromeUtils: { importESModule() {
      return { getFolder(id) { return { folder: id === "a://INBOX" ? source : destination }; } };
    } },
    Ci: { nsMsgFolderFlags: { Virtual: 1 }, nsMsgMessageFlags: { IMAPDeleted: 2, Expunged: 4 }, nsICryptoHash: {} },
    Cc: { "@mozilla.org/security/hash;1": { createInstance() {
      const hash = createHash("sha256");
      return { SHA256: 1, init() {}, update(bytes) { hash.update(bytes); }, finish() { return hash.digest("latin1"); } };
    } } },
    Components: { isSuccessCode: (status) => status === 0 },
  });
  const extension = {
    messageManager: { get: () => header, convert: () => ({ ...converted, ...(options.changed ? { subject: "Changed" } : {}) }) },
    folderManager: { convert: (folder) => ({ accountId: folder.accountId, path: folder.path }) },
  };
  const params = { messageId: 1,
    reference: { folderId: "a://INBOX", accountId: "a", folderPath: "/INBOX", headerMessageId: converted.headerMessageId, fingerprint },
    destination: { id: "a://Filed", accountId: "a", path: "/Filed" } };
  return { calls, source, destination, listeners, get refreshes() { return refreshes; },
    run: (extra = {}) => native.TBX_MODULES["batch.move"]({ ...params, ...extra }, extension) };
}

describe("native batch moves", () => {
  it("checks capabilities and identity without dispatch during preview", async () => {
    const f = fixture();
    assert.equal((await f.run({ checkOnly: true })).supported, true);
    assert.equal(f.calls.length, 0);
  });

  it("disables optimistic native undo and waits for actual copy-service completion", async () => {
    const f = fixture();
    let finished = false;
    const result = f.run().then((value) => { finished = true; return value; });
    await Promise.resolve();
    assert.equal(f.calls.length, 1);
    assert.equal(f.calls[0][0], f.source);
    assert.equal(f.calls[0][2], f.destination);
    assert.equal(f.calls[0][3], true);
    assert.equal(f.calls[0][6], false, "allowUndo must not create pseudo-offline headers");
    assert.equal(finished, false);
    f.calls[0][4].onStopCopy(0);
    const proof = await result;
    assert.equal(proof.completed, true);
    assert.equal(proof.nativeMoveObserved.messageId, 1);
    assert.equal(f.refreshes, 1, "authoritative destination headers must be loaded");
    assert.equal(f.listeners.size, 0);
  });

  for (const [option, code] of [["pseudo", "REFERENCE_PENDING"], ["stale", "REFERENCE_NOT_FOUND"],
    ["changed", "REFERENCE_CHANGED"], ["foreign", "REFERENCE_FOLDER_CHANGED"], ["offline", "REFERENCE_OFFLINE"]]) {
    it(`rejects ${option} native state before dispatch`, async () => {
      const f = fixture({ [option]: true });
      await assert.rejects(f.run(), (error) => error.code === code);
      assert.equal(f.calls.length, 0);
    });
  }

  it("does not turn a native failure into success", async () => {
    const f = fixture();
    const result = f.run();
    f.calls[0][4].onStopCopy(0x80004005);
    await assert.rejects(result, (error) => error.code === "NATIVE_MOVE_FAILED");
    assert.equal(f.listeners.size, 0);
  });

  it("accepts a newly loaded destination header when Thunderbird owns the current refresh", async () => {
    const f = fixture({ refreshInProgress: true, deletedOriginal: true });
    const result = f.run();
    f.calls[0][4].onStopCopy(0);
    assert.equal((await result).completed, true);
    assert.equal(f.refreshes, 1);
    assert.equal(f.listeners.size, 0);
  });

  for (const [option, code] of [["copyInstead", "NATIVE_UNEXPECTED_COPY"], ["refreshFailure", "NATIVE_REFRESH_FAILED"]]) {
    it(`does not acknowledge ${option} as a confirmed move`, async () => {
      const f = fixture({ [option]: true });
      const result = f.run();
      f.calls[0][4].onStopCopy(0);
      await assert.rejects(result, (error) => error.code === code);
      assert.equal(f.listeners.size, 0);
    });
  }
});

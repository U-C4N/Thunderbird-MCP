import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { describe, it } from "node:test";
import { fakeLog, loadScript, runInContext, fakeSandbox, loadExperiment } from "./harness.mjs";

const plain = (value) => JSON.parse(JSON.stringify(value));
const failure = (code) => (ex) => ex.code === code;

function fixture(overrides = {}) {
  const state = {
    body: "hello", subject: "Draft", id: 7, rawStatus: "0001", extraRaw: "",
    open: false, protected: false, cid: false, identity: "id1", saveCalls: 0, sendCalls: 0,
    finished: [], patches: [], removed: [], attachments: [], cc: ["cc@example.invalid"], ...overrides,
  };
  const nativeCalls = [];
  const message = () => ({
    id: state.id, subject: state.subject, author: "me@example.invalid", recipients: ["you@example.invalid"],
    ccList: state.cc, bccList: [], size: state.messageSize || 1000,
    folder: { id: "account1://Drafts" },
  });
  const browser = {
    tbx: { async invoke(method, params) {
      nativeCalls.push([method, params]);
      if (method === "drafts.inspect") return { openInComposer: state.open,
        editorSupported: !state.unpatched, editorRestriction: state.unpatched ? "Update Thunderbird." : null };
      if (method === "drafts.open") {
        state.open = true;
        if (state.changedOnOpen) state.extraRaw = "changed";
        return { tabId: 20 };
      }
      if (method === "drafts.finish") {
        state.finished.push(params);
        if (params.close) state.open = false;
        return {};
      }
      if (method === "drafts.state") return { canSendNow: true, canSendLater: true };
      throw new Error(method);
    } },
    messages: {
      async get() { return message(); },
      async getFull(_id, options) {
        // TB128 accepts decrypt, but rejects the decoding switches added in133.
        assert.deepEqual(Object.keys(options), ["decrypt"]);
        assert.equal(options.decrypt, false);
        return {
          headers: { "x-identity-key": [state.identity] }, contentType: state.protected ? "multipart/encrypted" : "message/rfc822",
          parts: [{ contentType: state.html ? "text/html" : "text/plain", body: state.body },
            ...(state.cid ? [{ contentType: "image/png", partName: "1.2", headers: { "content-id": ["<image1>"] } }] : [])],
        };
      },
      async getRaw(_id, options) {
        assert.equal(options.decrypt, false);
        return "X-Mozilla-Status: " + state.rawStatus + "\r\nSubject: " + state.subject +
          "\r\nX-Identity-Key: " + state.identity + "\r\n\r\n" + state.body + state.extraRaw;
      },
      async listAttachments() { return state.attachments; },
      async getAttachmentFile() { return new Blob(["attachment"]); },
    },
    compose: {
      async getComposeDetails() {
        return { type: "draft", identityId: state.liveIdentity || state.identity,
          subject: state.subject, to: state.nativeTo || message().recipients, cc: state.cc, bcc: [], plainTextBody: state.body,
          isPlainText: !state.html, selectedEncryptionTechnology: state.protection };
      },
      async getComposeState() { return state.emptyComposeState ? {} : { canSendNow: true, canSendLater: true }; },
      async setComposeDetails(_id, patch) {
        state.patches.push(patch);
        if ("subject" in patch) state.subject = patch.subject;
        if ("cc" in patch) state.cc = patch.cc;
        if ("plainTextBody" in patch && !state.preventBodyEdit) state.body = patch.plainTextBody;
      },
      async listAttachments() { return state.liveAttachments || []; },
      async getAttachmentFile() { return new Blob(["attachment"]); },
      async removeAttachment(_id, id) { state.removed.push(id); },
      async addAttachment() { if (state.addFails) throw new Error("attachment import failed"); },
      async saveMessage() {
        state.saveCalls++;
        if (state.saveFails) throw new Error("connection disappeared");
        state.id++;
        return { messages: [message()], mode: "draft" };
      },
      async sendMessage() {
        state.sendCalls++;
        if (state.sendFails) throw new Error("Sent copy failed");
        return { messages: [message()], mode: "sendNow", headerMessageId: "sent@example.invalid" };
      },
    },
  };
  const ctx = loadScript("background/registry.js", {
    browser, crypto: webcrypto, tbxLog: fakeLog(),
    tbxReferences: {
      async capture(id) {
        if (state.captureFails) throw new Error("reference unavailable");
        return { messageId: id, fingerprint: "fp", folderId: "account1://Drafts" };
      },
      async resolve() { return message(); },
    },
    tbxCompose: {
      async fileFromPath(path) { return { name: path, file: new Blob(["new"]) }; },
      outcome(result, mode) { return { messageId: result.messages[0]?.id, mode }; },
    },
  });
  runInContext(ctx, "background/handlers/drafts.js");
  const call = (method, params = {}) => ctx.tbxRegistry.invoke("compose." + method, params);
  return { state, nativeCalls, call, browser };
}

async function mutationArgs(f) {
  const snapshot = await f.call("draftGet", { messageId: 7 });
  return { reference: snapshot.reference, expectedRevision: snapshot.revision, operationId: "op1", requestKey: "key1" };
}

describe("saved draft snapshots", () => {
  it("keeps saved content readable but refuses updates and sends on affected builds", async () => {
    const f = fixture({ unpatched: true });
    const result = await f.call("draftGet", { messageId: 7 });
    assert.equal(result.body, "hello");
    assert.ok(result.reference && result.revision);
    assert.equal(result.reason, "unsupported_draft_editor");
    assert.equal(result.editable, false);
    assert.equal(result.sendable, false);
    const args = await mutationArgs(f);
    await assert.rejects(f.call("draftUpdate", { ...args, changes: { subject: "no" } }), failure("UNSUPPORTED_CAPABILITY"));
    await assert.rejects(f.call("draftSend", { ...args, mode: "send" }), failure("UNSUPPORTED_CAPABILITY"));
    assert.ok(f.nativeCalls.every(([method]) => method === "drafts.inspect"));
    assert.equal(f.state.saveCalls + f.state.sendCalls, 0);
  });
  it("reads saved MIME without opening a composer", async () => {
    const f = fixture();
    const result = await f.call("draftGet", { messageId: 7 });
    assert.equal(result.body, "hello");
    assert.equal(result.editable, true);
    assert.equal(result.sendable, true);
    assert.match(result.revision, /^sha256:[a-f0-9]{64}$/);
    assert.deepEqual(f.nativeCalls.map(([method]) => method), ["drafts.inspect"]);
  });
  it("ignores read-state header changes but hashes attachment/body bytes", async () => {
    const f = fixture();
    const first = await f.call("draftGet", { messageId: 7 });
    f.state.rawStatus = "0003";
    const second = await f.call("draftGet", { messageId: 7 });
    assert.equal(second.revision, first.revision);
    f.state.extraRaw = "changed attachment bytes";
    const third = await f.call("draftGet", { messageId: 7 });
    assert.notEqual(third.revision, first.revision);
  });
  it("returns protected metadata without body, revision or decryption", async () => {
    const f = fixture({ protected: true });
    const result = await f.call("draftGet", { messageId: 7 });
    assert.equal(result.editable, false);
    assert.equal(result.reason, "protected_draft");
    assert.equal(result.body, undefined);
    assert.equal(result.revision, null);
  });
  it("never exposes a truncated body as editable", async () => {
    const f = fixture({ body: "a".repeat(200001) });
    const result = await f.call("draftGet", { messageId: 7 });
    assert.equal(result.body, undefined);
    assert.equal(result.bodyPreview.length, 4000);
    assert.equal(result.bodyTruncated, true);
    assert.equal(result.sendable, false);
    assert.equal(result.revision, null);
  });
  it("open user editors are reported without touching the window", async () => {
    const f = fixture({ open: true });
    const result = await f.call("draftGet", { messageId: 7 });
    assert.equal(result.openInComposer, true);
    assert.equal(result.editable, false);
    assert.equal(f.state.finished.length, 0);
  });
  it("missing stable reference leaves the readable draft uneditable", async () => {
    const f = fixture({ captureFails: true });
    const result = await f.call("draftGet", { messageId: 7 });
    assert.equal(result.body, "hello");
    assert.equal(result.editable, false);
    assert.equal(result.reason, "unresolvable_reference");
  });
});

describe("guarded draft mutations", () => {
  it("patches only supplied fields, preserves an empty cc list, and refreshes the saved id", async () => {
    const f = fixture();
    const args = await mutationArgs(f);
    const result = await f.call("draftUpdate", { ...args, changes: { subject: "Revised", cc: [] } });
    assert.deepEqual(plain(f.state.patches), [{ subject: "Revised", cc: [] }]);
    assert.equal(result.messageId, 8);
    assert.equal(result.reference.messageId, 8);
    assert.notEqual(result.revision, args.expectedRevision);
    assert.equal(result.body, undefined);
    assert.equal(f.state.finished[0].close, true);
  });
  it("refuses a stale revision without opening or saving", async () => {
    const f = fixture();
    const args = await mutationArgs(f);
    f.state.body = "user changed this";
    await assert.rejects(f.call("draftUpdate", { ...args, changes: { subject: "Revised" } }), failure("DRAFT_CONFLICT"));
    assert.equal(f.state.saveCalls, 0);
    assert.equal(f.nativeCalls.some(([name]) => name === "drafts.open"), false);
  });
  it("leaves a user-owned editor untouched", async () => {
    const f = fixture();
    const args = await mutationArgs(f);
    f.state.open = true;
    await assert.rejects(f.call("draftUpdate", { ...args, changes: { subject: "Revised" } }), failure("DRAFT_BUSY"));
    assert.equal(f.state.finished.length, 0);
  });
  it("detects external edits during native editor startup", async () => {
    const f = fixture({ changedOnOpen: true });
    const args = await mutationArgs(f);
    await assert.rejects(f.call("draftUpdate", { ...args, changes: { subject: "Revised" } }), failure("DRAFT_CONFLICT"));
    assert.equal(f.state.patches.length, 0);
    assert.equal(f.state.finished[0].close, false);
  });
  it("refuses a changed native sender identity", async () => {
    const f = fixture({ liveIdentity: "other" });
    const args = await mutationArgs(f);
    await assert.rejects(f.call("draftSend", { ...args, mode: "send" }), failure("DRAFT_CONFLICT"));
    assert.equal(f.state.sendCalls, 0);
  });
  it("refuses recipients changed while opening the native editor", async () => {
    const f = fixture({ nativeTo: ["other@example.invalid"] });
    await assert.rejects(f.call("draftSend", { ...await mutationArgs(f), mode: "send" }), failure("DRAFT_CONFLICT"));
    assert.equal(f.state.sendCalls, 0);
  });
  it("refuses a silently prevented plain text edit before saving", async () => {
    const f = fixture({ preventBodyEdit: true });
    await assert.rejects(f.call("draftUpdate", { ...await mutationArgs(f), changes: { body: "replacement" } }), failure("DRAFT_CONFLICT"));
    assert.equal(f.state.saveCalls, 0);
    assert.equal(f.state.finished[0].close, false);
  });
  it("returns recovery metadata after a partially applied editor change", async () => {
    const f = fixture({ addFails: true });
    await assert.rejects(f.call("draftUpdate", { ...await mutationArgs(f), changes: { subject: "changed", addAttachments: [{ path: "a.txt" }] } }), (ex) => ex.recoveryWindowOpen && ex.tabId === 20);
    assert.equal(f.state.subject, "changed");
    assert.equal(f.state.saveCalls, 0);
    assert.equal(f.state.finished[0].close, false);
  });
  it("uses the owned native command state if the official API returns an empty object", async () => {
    const f = fixture({ emptyComposeState: true });
    const result = await f.call("draftSend", { ...await mutationArgs(f), mode: "send" });
    assert.equal(result.sent, true);
    assert.equal(f.nativeCalls.some(([method]) => method === "drafts.state"), true);
  });
  it("does not disable signing or encryption selected in the native draft", async () => {
    const f = fixture({ protection: { signMessage: true } });
    const args = await mutationArgs(f);
    await assert.rejects(f.call("draftSend", { ...args, mode: "send" }), failure("PROTECTED_DRAFT_UNSUPPORTED"));
    assert.equal(f.state.sendCalls, 0);
    assert.equal(f.state.patches.length, 0);
  });
  it("refuses body edits to inline CID content before opening", async () => {
    const f = fixture({ cid: true, html: true });
    const args = await mutationArgs(f);
    await assert.rejects(f.call("draftUpdate", { ...args, changes: { body: "new" } }), failure("DRAFT_UNSUPPORTED_MIME"));
    assert.equal(f.state.open, false);
  });
  it("sends native CID content without reconstructing or patching the body", async () => {
    const f = fixture({ cid: true, html: true });
    const args = await mutationArgs(f);
    const result = await f.call("draftSend", { ...args, mode: "send" });
    assert.equal(result.sent, true);
    assert.equal(f.state.patches.length, 0);
    assert.equal(f.state.finished[0].close, true);
  });
  it("preserves an ambiguous send without saving a rescue or closing its editor", async () => {
    const f = fixture({ sendFails: true });
    const args = await mutationArgs(f);
    await assert.rejects(f.call("draftSend", { ...args, mode: "send" }), failure("OUTCOME_UNKNOWN"));
    assert.equal(f.state.sendCalls, 1);
    assert.equal(f.state.saveCalls, 0);
    assert.equal(f.state.finished[0].close, false);
  });
  it("does not close a composer after a failed save", async () => {
    const f = fixture({ saveFails: true });
    const args = await mutationArgs(f);
    await assert.rejects(f.call("draftUpdate", { ...args, changes: { subject: "Revised" } }), failure("OUTCOME_UNKNOWN"));
    assert.equal(f.state.finished[0].close, false);
  });
  it("refuses ambiguous duplicate attachments before changing the draft", async () => {
    const f = fixture({ attachments: [{ name: "a.txt", size: 10, partName: "1.2" }],
      liveAttachments: [{ id: 1, name: "a.txt", size: 10 }, { id: 2, name: "a.txt", size: 10 }] });
    const args = await mutationArgs(f);
    await assert.rejects(f.call("draftUpdate", { ...args, changes: { removeAttachmentRefs: ["part:1.2"] } }), failure("DRAFT_ATTACHMENT_CONFLICT"));
    assert.equal(f.state.removed.length, 0);
    assert.equal(f.state.saveCalls, 0);
  });
});

describe("native draft helper", () => {
  function nativeFixture(userWindow = null, version = "155.0") {
    const windows = userWindow ? [userWindow] : [];
    let observer;
    let openType;
    const header = { flags: 0, folder: { flags: 1, getUriForMsg: () => "mailbox://ACCOUNT/Drafts#7" } };
    const listeners = new Map();
    const win = {
      gComposeType: 9, gMsgCompose: { compFields: { draftId: "mailbox://account/Drafts#7" } },
      closed: false, addEventListener(name, fn) { listeners.set(name, fn); },
      removeEventListener(name) { listeners.delete(name); },
      gAutoSaveInterval: 300000, gAutoSaveTimeout: 1,
      clearTimeout() {}, setTimeout() { this.autoSaveScheduled = true; }, AutoSave() {},
      defaultController: { isCommandEnabled: () => true },
      SetContentAndBodyAsUnmodified() { this.unmodified = true; },
      close() { this.closed = true; },
    };
    const globals = fakeSandbox();
    globals.Services.appinfo.version = version;
    globals.Services.vc = { compare(value, minimum) {
      // This fixture only exercises the documented release boundaries.
      return value.localeCompare(minimum, "en", { numeric: true });
    } };
    globals.Services.io = { newURI(value) {
      return { spec: value.replace(/^(\w+:\/\/)([^/]+)/, (_all, scheme, authority) => scheme.toLowerCase() + authority.toLowerCase()) };
    } };
    globals.Services.wm = { getEnumerator: () => windows.values() };
    globals.Services.ww = {
      registerNotification(value) { observer = value; }, unregisterNotification() { observer = null; },
    };
    globals.Ci.nsMsgFolderFlags = { Drafts: 1 };
    globals.Ci.nsMsgMessageFlags = { Queued: 2, Template: 4 };
    globals.Ci.nsIMsgCompType = { Draft: 9, EditAsNew: 10 };
    globals.Ci.nsIMsgCompFormat = { Default: 0 };
    const originalImport = globals.ChromeUtils.importESModule;
    globals.ChromeUtils.importESModule = (url) => {
      if (url.endsWith("MailServices.sys.mjs")) return { MailServices: { accounts: { allIdentities: [{ key: "id1" }] }, compose: {
        OpenComposeWindow(_url, _header, _uri, type) {
          openType = type; windows.push(win); observer.observe(win, "domwindowopened");
          listeners.get("compose-editor-ready")();
        },
      } } };
      return originalImport(url);
    };
    const ctx = loadExperiment(globals, { modules: ["drafts"] });
    const extension = { messageManager: { get: () => header }, tabManager: { convert: () => ({ id: 20 }) } };
    const api = new ctx.tbx().getAPI({ extension }).tbx;
    return { api, win, listeners, type: () => openType };
  }
  it("rejects the affected native editor before opening any window", async () => {
    for (const version of ["128.0", "128.4.2", "132.0", "133.0b2"]) {
      const f = nativeFixture(null, version);
      assert.equal((await f.api.invoke("drafts.inspect", { messageId: 7 })).editorSupported, false, version);
      await assert.rejects(f.api.invoke("drafts.open", { messageId: 7 }), /UNSUPPORTED_CAPABILITY/);
      assert.equal(f.type(), undefined);
    }
    for (const version of ["128.4.3", "128.14.0", "133.0b3", "155.0"]) {
      const f = nativeFixture(null, version);
      assert.equal((await f.api.invoke("drafts.inspect", { messageId: 7 })).editorSupported, true, version);
    }
  });
  it("opens the real Draft compose type and only closes the owned window", async () => {
    const f = nativeFixture();
    await f.api.invoke("drafts.open", { messageId: 7, identityId: "id1", operationId: "op" });
    assert.equal(f.type(), 9);
    await f.api.invoke("drafts.finish", { tabId: 20, operationId: "op", close: true });
    assert.equal(f.win.closed, true);
  });
  it("refuses to adopt an already open user's window", async () => {
    const user = { closed: false, gMsgCompose: { compFields: { draftId: "mailbox://account/Drafts#7" } } };
    const f = nativeFixture(user);
    await assert.rejects(f.api.invoke("drafts.open", { messageId: 7, identityId: "id1", operationId: "op" }), (ex) => String(ex.message).includes("DRAFT_BUSY"));
    assert.equal(f.type(), undefined);
    assert.equal(user.closed, false);
  });
  it("cannot close a window owned by another operation", async () => {
    const f = nativeFixture();
    await f.api.invoke("drafts.open", { messageId: 7, identityId: "id1", operationId: "op" });
    await assert.rejects(f.api.invoke("drafts.finish", { tabId: 20, operationId: "other", close: true }), (ex) => String(ex.message).includes("DRAFT_NOT_OWNED"));
    assert.equal(f.win.closed, false);
  });
  it("does not block native API body-edit events, and disables auto-save on recovery", async () => {
    const f = nativeFixture();
    await f.api.invoke("drafts.open", { messageId: 7, identityId: "id1", operationId: "op" });
    assert.equal(f.listeners.has("beforeinput"), false);
    assert.equal(f.listeners.has("keydown"), true);
    await f.api.invoke("drafts.finish", { tabId: 20, operationId: "op", close: false });
    assert.equal(f.listeners.has("keydown"), false);
    assert.equal(f.win.gAutoSaveInterval, 0);
    assert.equal(f.win.autoSaveScheduled, undefined);
    assert.equal(f.win.closed, false);
  });
});

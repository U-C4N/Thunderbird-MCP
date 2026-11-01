import assert from "node:assert/strict";
import { createHash, webcrypto } from "node:crypto";
import path from "node:path";
import { describe, it } from "node:test";
import { fakeSandbox, loadExperiment, loadScript, runInContext } from "./harness.mjs";
const plain = (v) => JSON.parse(JSON.stringify(v));
const hash = (v) => createHash("sha256").update(v).digest("hex");
function files() {
  const disk = new Map(), directories = new Set(["/out"]);
  let serial = 0;
  const state = { failWrite: false, race: null };
  const io = {
    async exists(name) { return disk.has(name) || directories.has(name); },
    async stat(name) {
      if (directories.has(name)) return { type: "directory", size: 0 };
      if (!disk.has(name)) throw new Error("not found");
      return { type: "regular", size: disk.get(name).length };
    },
    async createUniqueFile(directory, prefix) { const name = `${directory}/${prefix}${++serial}`; disk.set(name, Buffer.alloc(0)); return name; },
    async write(name, bytes) {
      disk.set(name, Buffer.from(bytes));
      if (state.failWrite) throw new Error("disk full");
    },
    async move(from, to, options) {
      if (state.race) { disk.set(to, Buffer.from(state.race)); state.race = null; }
      if (options.noOverwrite && disk.has(to)) throw new Error("already exists");
      disk.set(to, disk.get(from)); disk.delete(from);
    },
    async remove(name) { disk.delete(name); },
    async read(name) { return Uint8Array.from(disk.get(name)); },
    async computeHexDigest(name) { return hash(disk.get(name)); },
  };
  const globals = fakeSandbox();
  globals.IOUtils = io;
  globals.PathUtils = { ...path.posix, filename: path.posix.basename, parent: path.posix.dirname };
  globals.ChromeUtils.base64URLDecode = (text) => Uint8Array.from(Buffer.from(text, "base64url")).buffer;
  globals.ChromeUtils.base64URLEncode = (bytes) => Buffer.from(bytes).toString("base64url");
  const ctx = loadExperiment(globals, { modules: ["files"] });
  const handlers = ctx.TBX_TEST_HOOKS.core.handlers;
  return { disk, state, call: (method, params) => handlers[method](params), hooks: ctx.TBX_TEST_HOOKS.files };
}
describe("atomic local-file writes", () => {
  it("uses unique owned temporaries and verifies the attachment hash", async () => {
    const f = files(), bytes = Buffer.from("invoice");
    const out = await f.call("files.write", { directory: "/out", filename: "fatura.pdf", base64: bytes.toString("base64"), hash: true, expectedSha256: hash(bytes) });
    assert.equal(out.sha256, hash(bytes));
    assert.equal(f.disk.size, 1);
    assert.equal(f.disk.get("/out/fatura.pdf").toString(), "invoice");
  });
  it("never clobbers a destination appearing after the existence check", async () => {
    const f = files(); f.state.race = "someone else's file";
    await assert.rejects(f.call("files.write", { directory: "/out", filename: "a.txt", base64: "YQ==" }), /already exists/);
    assert.equal(f.disk.get("/out/a.txt").toString(), "someone else's file");
    assert.equal(f.disk.size, 1);
  });
  it("cleans only its own temporary after a partial failed write", async () => {
    const f = files(); f.disk.set("/out/keep.tmp", Buffer.from("keep")); f.state.failWrite = true;
    await assert.rejects(f.call("files.write", { directory: "/out", filename: "a.txt", base64: "YQ==" }), /disk full/);
    assert.deepEqual([...f.disk.keys()], ["/out/keep.tmp"]);
  });
  it("allocates distinct safe names including Unicode and Windows reserved names", async () => {
    const f = files(); f.disk.set("/out/fatura.pdf", Buffer.from("existing"));
    const out = await f.call("files.prepareNames", { directory: "/out", names: ["fatura.pdf", "fatura.pdf", "CON.txt", "../a.pdf", "İstanbul.pdf"] });
    assert.deepEqual(plain(out.files).map((v) => v.name), ["fatura (2).pdf", "fatura (3).pdf", "_CON.txt", ".._a.pdf", "İstanbul.pdf"]);
    assert.equal(f.disk.size, 1, "planning writes no file");
  });
});
function attachments({ attachments: entries, bytes = "invoice" } = {}) {
  const f = files(), content = Buffer.from(bytes);
  const list = entries || [{ partName: "1.2", name: "invoice.pdf", contentType: "application/pdf", size: content.length }];
  const state = { content, list, unavailable: false, reads: 0 };
  const message = { id: 1, subject: "Invoice" };
  const browser = { tbx: { invoke: f.call }, messages: {
    async get() { return message; }, async listAttachments() { return state.list; },
    async getAttachmentFile() { state.reads++; return { size: state.content.length, async arrayBuffer() { return Uint8Array.from(state.content).buffer; } }; },
  } };
  const references = { async captureHeader() { return { messageId: 1, fingerprint: "ref" }; },
    async resolve() { if (state.unavailable) throw new Error("moved"); return message; } };
  const ctx = loadScript("background/registry.js", { browser, crypto: webcrypto, tbxReferences: references, btoa,
    tbxBase64: { fromArrayBuffer: (v) => Buffer.from(v).toString("base64") } });
  runInContext(ctx, "background/handlers/attachments.js");
  return { ...f, state, call: (method, params) => ctx.tbxRegistry.invoke(method, params) };
}
describe("frozen attachment export", () => {
  it("plans metadata without downloading bytes, then prepares and writes once", async () => {
    const a = attachments();
    const plan = await a.call("messages.attachmentsPlan", { messageIds: [1], directory: "/out" });
    assert.equal(plan.items.length, 1); assert.equal(a.state.reads, 0);
    const prepared = await a.call("messages.attachmentsPrepare", { item: plan.items[0] });
    assert.equal(prepared.item.expectedSha256, hash(a.state.content));
    const first = await a.call("messages.attachmentsSave", { item: prepared.item });
    assert.equal(first.status, "succeeded");
    const reads = a.state.reads;
    const again = await a.call("messages.attachmentsReconcile", { item: prepared.item });
    assert.equal(again.status, "succeeded"); assert.equal(a.state.reads, reads);
    assert.equal(a.disk.size, 1);
  });
  it("a matching file appearing after preview is still a conflict for an initial save", async () => {
    const a = attachments();
    const plan = await a.call("messages.attachmentsPlan", { messageIds: [1], directory: "/out" });
    const { item } = await a.call("messages.attachmentsPrepare", { item: plan.items[0] });
    a.disk.set(item.path, Buffer.from(a.state.content));
    const reads = a.state.reads;
    const result = await a.call("messages.attachmentsSave", { item });
    assert.equal(result.status, "conflict");
    assert.equal(result.code, "DESTINATION_EXISTS");
    assert.equal(a.state.reads, reads);
  });
  it("freezes names and refuses changed source bytes or destination", async () => {
    const a = attachments();
    const plan = await a.call("messages.attachmentsPlan", { messageIds: [1], directory: "/out" });
    const prepared = await a.call("messages.attachmentsPrepare", { item: plan.items[0] });
    a.state.content = Buffer.from("changed");
    assert.equal((await a.call("messages.attachmentsSave", { item: prepared.item })).status, "conflict");
    a.disk.set(prepared.item.path, Buffer.from("keep"));
    assert.equal((await a.call("messages.attachmentsSave", { item: prepared.item })).status, "conflict");
    assert.equal(a.disk.get(prepared.item.path).toString(), "keep");
  });
  it("reconciles only a matching hash and leaves absence unknown", async () => {
    const a = attachments();
    const plan = await a.call("messages.attachmentsPlan", { messageIds: [1], directory: "/out" });
    const { item } = await a.call("messages.attachmentsPrepare", { item: plan.items[0] });
    assert.equal((await a.call("messages.attachmentsReconcile", { item })).status, "unknown");
    a.disk.set(item.path, Buffer.from("invoice"));
    assert.equal((await a.call("messages.attachmentsReconcile", { item })).status, "succeeded");
  });
  it("enforces filters and counts the 201st attachment as invalid", async () => {
    const entries = Array.from({ length: 201 }, (_, i) => ({ partName: String(i), name: `invoice${i}.pdf`, contentType: "application/pdf", size: 1 }));
    const a = attachments({ attachments: entries });
    const plan = await a.call("messages.attachmentsPlan", { messageIds: [1], directory: "/out" });
    assert.equal(plan.items.length, 200); assert.equal(plan.invalid[0].code, "EXPORT_LIMIT");
    const filtered = await a.call("messages.attachmentsPlan", { messageIds: [1], directory: "/out", filenameContains: "invoice200", contentTypes: ["application/pdf"] });
    assert.equal(filtered.items.length, 1); assert.equal(filtered.invalid.length, 0);
  });
  it("rejects oversized metadata before allocating attachment bytes", async () => {
    const a = attachments({ attachments: [{ partName: "1", name: "large.zip", size: 26 * 1024 * 1024, contentType: "application/zip" }] });
    const plan = await a.call("messages.attachmentsPlan", { messageIds: [1], directory: "/out" });
    assert.equal(plan.items.length, 0); assert.equal(plan.invalid[0].code, "ATTACHMENT_SIZE"); assert.equal(a.state.reads, 0);
  });
});

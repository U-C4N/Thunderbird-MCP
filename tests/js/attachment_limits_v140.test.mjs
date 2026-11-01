import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { loadScript, runInContext } from "./harness.mjs";

const MIB = 1024 * 1024;

function fixture(sizes) {
  const state = { buffered: 0, downloaded: 0, offline: false, writes: 0, fileSize: sizes[0] };
  const attachments = sizes.map((size, index) => ({ size, partName: String(index),
    name: `part${index}.pdf`, contentType: "application/pdf" }));
  const message = { id: 1, subject: "Synthetic" };
  const browser = {
    messages: {
      async get() { return message; },
      async listAttachments() { return attachments; },
      async getAttachmentFile() {
        state.downloaded++;
        if (state.offline) throw new Error("source offline");
        return { size: state.fileSize, async arrayBuffer() { state.buffered++; throw new Error("unexpected allocation"); } };
      },
    },
    tbx: { async invoke(method, params) {
      if (method === "files.prepareNames") return { directory: "/out", files: params.names.map((name) => ({ name, path: `/out/${name}` })) };
      if (method === "files.write") state.writes++;
      throw new Error(`unexpected ${method}`);
    } },
  };
  const ctx = loadScript("background/registry.js", { browser, tbxReferences: {
    async captureHeader() { return { messageId: 1, fingerprint: "fixture" }; },
    async resolve() { return message; },
  } });
  runInContext(ctx, "background/handlers/attachments.js");
  const invoke = (method, args) => ctx.tbxRegistry.invoke(method, args);
  return { state, plan: () => invoke("messages.attachmentsPlan", { messageIds: [1], directory: "/out" }),
    prepare: (item) => invoke("messages.attachmentsPrepare", { item }) };
}

describe("attachment export size and availability boundaries", () => {
  it("admits exactly 250 MiB and invalidates one byte beyond it without reading file content", async () => {
    const sizes = Array(10).fill(25 * MIB);
    const atLimit = fixture(sizes);
    const accepted = await atLimit.plan();
    assert.equal(accepted.counts.bytes, 250 * MIB);
    assert.deepEqual(Array.from(accepted.invalid), []);
    const overLimit = fixture([...sizes, 1]);
    const rejected = await overLimit.plan();
    assert.equal(rejected.invalid[0].code, "EXPORT_LIMIT");
    assert.equal(atLimit.state.downloaded + overLimit.state.downloaded, 0);
  });

  it("checks actual Thunderbird file size before allocating a changed oversized file", async () => {
    const f = fixture([4]);
    const plan = await f.plan();
    f.state.fileSize = 25 * MIB + 1;
    await assert.rejects(f.prepare(plan.items[0]), (error) => error.code === "ATTACHMENT_SIZE");
    assert.equal(f.state.downloaded, 1);
    assert.equal(f.state.buffered, 0);
    assert.equal(f.state.writes, 0);
  });

  it("does not start a file write when the reviewed attachment becomes unavailable", async () => {
    const f = fixture([4]);
    const plan = await f.plan();
    f.state.offline = true;
    await assert.rejects(f.prepare(plan.items[0]), /offline/);
    assert.equal(f.state.buffered, 0);
    assert.equal(f.state.writes, 0);
  });
});

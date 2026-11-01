import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { describe, it } from "node:test";
import { fakeLog, loadScript, runInContext } from "./harness.mjs";

function fixture({ changeDuringMime = false, staleInitialHeaders = false } = {}) {
  const state = { body: "reviewed body", recipient: "first@example.com", gets: 0, reads: 0 };
  const browser = {
    tbx: { async invoke(method) { assert.equal(method, "drafts.inspect"); return { openInComposer: false }; } },
    messages: {
      async get() {
        state.gets++;
        return { id: 1, size: 100, subject: "Draft", author: "me@example.com", folder: { id: "account1://Drafts" },
          recipients: [staleInitialHeaders && state.gets === 1 ? "old@example.com" : state.recipient] };
      },
      async getRaw() {
        state.reads++;
        return `Subject: Draft\r\nTo: ${state.recipient}\r\nX-Identity-Key: id1\r\n\r\n${state.body}`;
      },
      async getFull() {
        const body = state.body;
        if (changeDuringMime) { state.body = "unreviewed replacement"; state.recipient = "second@example.com"; }
        return { contentType: "message/rfc822", headers: { "x-identity-key": ["id1"] },
          parts: [{ contentType: "text/plain", body }] };
      },
      async listAttachments() { return []; },
    },
  };
  const ctx = loadScript("background/registry.js", { browser, crypto: webcrypto, tbxLog: fakeLog(),
    tbxReferences: { async capture() { return { messageId: 1, fingerprint: "ref" }; } } });
  runInContext(ctx, "background/handlers/drafts.js");
  return { state, read: () => ctx.tbxRegistry.invoke("compose.draftGet", { messageId: 1 }) };
}

describe("consistent draft review snapshots", () => {
  it("refuses an old body paired with a newer raw-message revision", async () => {
    const f = fixture({ changeDuringMime: true });
    await assert.rejects(f.read(), (ex) => ex.code === "DRAFT_CONFLICT");
    assert.equal(f.state.reads, 2);
  });
  it("refreshes message metadata inside the raw revision bracket", async () => {
    const f = fixture({ staleInitialHeaders: true });
    const result = await f.read();
    assert.equal(result.to[0], "first@example.com");
    assert.equal(result.body, "reviewed body");
    assert.equal(result.sendable, true);
    assert.equal(f.state.reads, 2);
  });
});

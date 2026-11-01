import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { describe, it } from "node:test";
import { loadScript, runInContext } from "./harness.mjs";

function world() {
  const context = loadScript("background/registry.js", { crypto: webcrypto, TextEncoder });
  runInContext(context, "background/operations.js");
  runInContext(context, "background/handlers/operations.js");
  return context;
}

describe("runtime operation receipts", () => {
  it("coalesces concurrent duplicate delivery and reports pending", async () => {
    const w = world();
    let calls = 0, finish;
    w.tbxRegistry.define("test.write", async () => {
      calls++;
      await new Promise(resolve => { finish = resolve; });
      return { status: "succeeded", messageId: 9 };
    });
    const params = { operationId: "one", itemId: 2, body: "in memory only" };
    const first = w.tbxRegistry.invoke("test.write", params);
    while (!finish) await new Promise(resolve => setTimeout(resolve, 1));
    const second = w.tbxRegistry.invoke("test.write", params);
    const pending = await w.tbxRegistry.invoke("operations.receipt", { ...params, method: "test.write" });
    assert.equal(pending.pending, true);
    finish();
    assert.deepEqual(await first, await second);
    assert.equal(calls, 1);
    const receipt = await w.tbxRegistry.invoke("operations.receipt", { ...params, method: "test.write" });
    assert.equal(receipt.result.messageId, 9);
    assert.equal(receipt.pending, false);
    assert.equal(receipt.runtimeEpoch, w.tbxRuntimeEpoch);
  });

  it("refuses a reused key with changed arguments", async () => {
    const w = world();
    let calls = 0;
    w.tbxRegistry.define("test.write", async () => { calls++; return {}; });
    await w.tbxRegistry.invoke("test.write", { operationId: "one", value: 1 });
    await assert.rejects(w.tbxRegistry.invoke("test.write", { operationId: "one", value: 2 }), /different content/);
    assert.equal(calls, 1);
  });

  it("retains a failed receipt without replay and resets across runtime loads", async () => {
    const w = world();
    let calls = 0;
    w.tbxRegistry.define("test.write", async () => { calls++; throw w.tbxError.blocked("conflict", [], { code: "CONFLICT" }); });
    const params = { operationId: "one" };
    await assert.rejects(w.tbxRegistry.invoke("test.write", params), /conflict/);
    await assert.rejects(w.tbxRegistry.invoke("test.write", params), /conflict/);
    assert.equal(calls, 1);
    const receipt = w.tbxOperations.receipt({ ...params, method: "test.write" });
    assert.equal(receipt.error.code, "CONFLICT");
    assert.notEqual(world().tbxRuntimeEpoch, w.tbxRuntimeEpoch);
  });
});

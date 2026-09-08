/* The background page's side of the privileged hop.
 *
 * Everything the experiment throws arrives here as one string, because that is
 * all that survives `normalizeError`. These tests are the other half of that
 * contract: the tagged envelope is unpacked back into the taxonomy, and an
 * untagged message still gets the guesswork it always got.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { fakeBrowser, fakeLog, loadScript, runInContext } from "./harness.mjs";

/** registry.js and privileged.js in one page scope, as Thunderbird loads them. */
function loadPrivileged(invoke, { methods = ["gloda.search"] } = {}) {
  const browser = fakeBrowser();
  browser.tbx.availableModules = async () => ({ methods });
  browser.tbx.invoke = invoke;
  const ctx = loadScript("background/registry.js", { browser, tbxLog: fakeLog() });
  return runInContext(ctx, "background/handlers/privileged.js");
}

/** The error a call failed with; realms differ, so tests read its fields. */
async function failure(promise) {
  try {
    await promise;
  } catch (ex) {
    return ex;
  }
  return assert.fail("the call was expected to fail");
}

/** What the experiment throws once wireError has packed it. */
function tagged(payload) {
  return async () => {
    throw new Error(`tbxerr:${JSON.stringify(payload)}`);
  };
}

describe("forwarding a privileged failure", () => {
  it("unpacks a tagged usage error back into the taxonomy", async () => {
    const ctx = loadPrivileged(tagged({ kind: "usage", message: "subject is required" }));

    const ex = await failure(ctx.tbxRegistry.invoke("x.gloda.search", {}));

    assert.equal(ex.tbxKind, "usage");
    assert.equal(ex.message, "subject is required");
  });

  it("keeps what a blocked failure needs", async () => {
    const ctx = loadPrivileged(
      tagged({ kind: "blocked", message: "exists", needs: ["overwrite=true"] })
    );

    const ex = await failure(ctx.tbxRegistry.invoke("x.gloda.search", {}));

    assert.equal(ex.tbxKind, "blocked");
    assert.deepEqual([...ex.needs], ["overwrite=true"]);
  });

  it("carries the code the experiment reported", async () => {
    const ctx = loadPrivileged(
      tagged({ kind: "thunderbird", message: "t", code: "NS_ERROR_FAILURE" })
    );

    const ex = await failure(ctx.tbxRegistry.invoke("x.gloda.search", {}));

    assert.equal(ex.code, "NS_ERROR_FAILURE");
  });

  it("treats a kind it does not know as Thunderbird's problem", async () => {
    const ctx = loadPrivileged(tagged({ kind: "martian", message: "m" }));

    const ex = await failure(ctx.tbxRegistry.invoke("x.gloda.search", {}));

    assert.equal(ex.tbxKind, "thunderbird");
    assert.equal(ex.message, "m");
  });

  it("still guesses at an untagged message, since older builds send those", async () => {
    const required = loadPrivileged(async () => {
      throw new Error("x is required");
    });
    const weird = loadPrivileged(async () => {
      throw new Error("weird");
    });

    assert.equal(
      (await failure(required.tbxRegistry.invoke("x.gloda.search", {}))).tbxKind,
      "usage"
    );
    assert.equal(
      (await failure(weird.tbxRegistry.invoke("x.gloda.search", {}))).tbxKind,
      "thunderbird"
    );
  });
});

describe("tbxError.serialize", () => {
  it("does not report a bare Error name as a code", () => {
    const { tbxError } = loadScript("background/registry.js");

    // "[Error]" told the model nothing and read like a second failure.
    assert.equal(tbxError.serialize(new Error("boom")).code, null);
  });

  it("keeps a name that identifies the failure", () => {
    const { tbxError } = loadScript("background/registry.js");

    assert.equal(tbxError.serialize(new TypeError("t")).code, "TypeError");
    assert.equal(tbxError.serialize(Object.assign(new Error("x"), { code: "E1" })).code, "E1");
  });
});

describe("tbxError.fromWire", () => {
  it("unpacks an envelope for any caller of the privileged half, not just forward()", () => {
    const { tbxError } = loadScript("background/registry.js");

    const ex = tbxError.fromWire(new Error('tbxerr:{"kind":"usage","message":"no such file"}'));

    assert.equal(ex.tbxKind, "usage");
    assert.equal(ex.message, "no such file");
  });

  it("does not claim a message that is not one of ours", () => {
    const { tbxError } = loadScript("background/registry.js");

    assert.equal(tbxError.fromWire(new Error("plain")), null);
    assert.equal(tbxError.fromWire(new Error("tbxerr:not json")), null);
  });
});

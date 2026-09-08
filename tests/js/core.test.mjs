/* The privileged half's own plumbing: deadlines and the error envelope.
 *
 * Both defects these cover were invisible from the outside. The sandbox has no
 * DOM globals, so every `H.withTimeout` deadline threw a ReferenceError before
 * the work it guarded even started; and an Error thrown from a system-principal
 * sandbox loses its message on the way to the background page, which is why the
 * envelope below is a string the other side can parse rather than an object.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { fakeSandbox, loadExperiment } from "./harness.mjs";

const TIMER_URL = "resource://gre/modules/Timer.sys.mjs";

describe("H.withTimeout", () => {
  it("rejects with its own deadline, in a sandbox that has no DOM timers", async () => {
    const ctx = loadExperiment(fakeSandbox());
    const { H } = ctx.TBX_TEST_HOOKS.core;

    await assert.rejects(H.withTimeout(new Promise(() => {}), 20, "gloda search"), (ex) => {
      assert.match(String(ex.message), /gloda search timed out after 20ms/);
      return true;
    });
  });

  it("clears the timer when the guarded work wins the race", async () => {
    const globals = fakeSandbox();
    const timer = globals.ChromeUtils.importESModule(TIMER_URL);
    const ctx = loadExperiment(globals);
    const { H } = ctx.TBX_TEST_HOOKS.core;

    assert.equal(await H.withTimeout(Promise.resolve(7), 20, "a fast one"), 7);
    assert.equal(timer.cleared.length, 1, "an armed timer outlives the call that armed it");
  });
});

/** The whole privileged API, plus the lexical helpers a test needs to drive it. */
function experiment(globals = fakeSandbox(), extension = {}) {
  const ctx = loadExperiment(globals);
  const api = new ctx.tbx().getAPI({ extension }).tbx;
  return {
    api,
    hooks: ctx.TBX_TEST_HOOKS.core,
    invoke: (method, params) => api.invoke(method, params),
  };
}

/** The error a call failed with, whatever realm minted it. */
async function rejection(promise) {
  try {
    await promise;
  } catch (ex) {
    return ex;
  }
  return assert.fail("the call was expected to fail");
}

/** The payload the background page gets to parse out of a failed call. */
async function envelope(promise) {
  const message = String((await rejection(promise)).message);
  assert.ok(message.startsWith("tbxerr:"), `not a tagged error: ${message}`);
  return JSON.parse(message.slice("tbxerr:".length));
}

describe("invoke", () => {
  it("packs a usage failure into an envelope, because a bare Error loses its message", async () => {
    const { hooks, invoke } = experiment();
    hooks.handlers["test.thing"] = async () => {
      throw hooks.H.usage("subject is required");
    };

    assert.deepEqual(await envelope(invoke("test.thing", {})), {
      kind: "usage",
      message: "subject is required",
      code: null,
    });
  });

  it("carries what a blocked failure needs to go through", async () => {
    const { hooks, invoke } = experiment();
    hooks.handlers["test.thing"] = async () => {
      throw hooks.H.blocked("exists", "overwrite=true");
    };

    assert.deepEqual(await envelope(invoke("test.thing", {})), {
      kind: "blocked",
      message: "exists",
      code: null,
      needs: ["overwrite=true"],
    });
  });

  it("reports an untyped failure as Thunderbird's, with no code worth quoting", async () => {
    const { hooks, invoke } = experiment();
    hooks.handlers["test.thing"] = async () => {
      throw new Error("boom");
    };

    assert.deepEqual(await envelope(invoke("test.thing", {})), {
      kind: "thunderbird",
      message: "boom",
      code: null,
    });
  });

  it("keeps a subclass name as the code", async () => {
    const { hooks, invoke } = experiment();
    hooks.handlers["test.thing"] = async () => {
      throw new TypeError("t");
    };

    assert.equal((await envelope(invoke("test.thing", {}))).code, "TypeError");
  });

  it("names the method it does not know", async () => {
    const { invoke } = experiment();

    const payload = await envelope(invoke("nope.thing", {}));
    assert.equal(payload.kind, "usage");
    assert.match(payload.message, /nope\.thing/);
  });

  it("still reports a message when ExtensionError is not there to carry it", async () => {
    const { hooks, invoke } = experiment(
      fakeSandbox({ modules: { "resource://gre/modules/ExtensionUtils.sys.mjs": {} } })
    );
    hooks.handlers["test.thing"] = async () => {
      throw hooks.H.usage("subject is required");
    };

    // A plain object is the only other shape normalizeError keeps a message for.
    assert.equal((await envelope(invoke("test.thing", {}))).message, "subject is required");
  });
});

describe("the rest of the API surface", () => {
  it("packs a blocked write into the envelope, though it never goes through invoke", async () => {
    const globals = fakeSandbox();
    globals.IOUtils = { ...globals.IOUtils, exists: async () => true };
    const { api } = experiment(globals);

    const failure = await rejection(
      api.writeFile({ directory: "/out", filename: "invoice.pdf", base64: "", overwrite: false })
    );

    assert.equal(failure.constructor.name, "ExtensionError");
    assert.ok(String(failure.message).startsWith("tbxerr:"));
    const payload = JSON.parse(String(failure.message).slice("tbxerr:".length));
    assert.equal(payload.kind, "blocked");
    assert.match(payload.message, /already exists/);
    assert.match(payload.needs.join(" "), /overwrite=true/);
  });

  it("packs a usage failure from a method that is not invoke", async () => {
    const extension = { hasPermission: () => false, manifest: { optional_permissions: [] } };
    const { api } = experiment(fakeSandbox(), extension);

    const payload = await envelope(api.grantOptionalPermission("messages.send"));

    assert.equal(payload.kind, "usage");
    assert.match(payload.message, /optional_permissions/);
  });

  it("leaves a method that succeeds alone", async () => {
    const { api } = experiment();

    assert.equal((await api.appInfo()).name, "Thunderbird");
  });
});

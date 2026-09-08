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

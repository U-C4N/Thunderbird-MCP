import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { fakeLog, loadScript, runInContext } from "./harness.mjs";

function fixture(headless = false) {
  const calls = { sends: 0, saves: 0, closed: 0 };
  const browser = {
    messages: {},
    compose: {
      async beginNew() { return { id: 20 }; },
      async sendMessage() { calls.sends++; throw new Error("SMTP accepted, Sent copy failed"); },
      async saveMessage() { calls.saves++; throw new Error("save unavailable"); },
      async setComposeDetails() {},
    },
    tabs: { async remove() { calls.closed++; } },
  };
  if (headless) browser.messages.sendMessage = browser.compose.sendMessage;
  const context = loadScript("background/registry.js", { browser, tbxLog: fakeLog() });
  runInContext(context, "background/handlers/compose.js");
  return { calls, browser, run: (mode, details = {}) => context.tbxRegistry.invoke("compose.send", { mode, to: ["to@example.invalid"], body: "test", ...details }) };
}

describe("legacy send ambiguity", () => {
  for (const headless of [false, true]) {
    it(`${headless ? "headless" : "native window"} delivery never retries or creates a rescue draft after send uncertainty`, async () => {
      const f = fixture(headless);
      await assert.rejects(f.run("send"), (error) => error.code === "OUTCOME_UNKNOWN");
      assert.deepEqual(f.calls, { sends: 1, saves: 0, closed: 0 });
    });
  }
  it("keeps the compose window after an unsuccessful save", async () => {
    const f = fixture();
    await assert.rejects(f.run("draft"), /left open/);
    assert.deepEqual(f.calls, { sends: 0, saves: 1, closed: 0 });
  });
  it("preserves HTML saves through the native editor even when headless save exists", async () => {
    const f = fixture();
    f.browser.messages.saveMessage = () => { throw new Error("unsafe HTML headless path"); };
    await assert.rejects(f.run("draft", { isHtml: true, body: "<p>HTML</p>" }), /left open/);
    assert.equal(f.calls.saves, 1);
  });
});

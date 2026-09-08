/* The error console, as read from inside Thunderbird.
 *
 * `tb_console` exists to answer "what did the bridge complain about?", and it
 * could not: an extension page's `console.*` output goes to the ConsoleAPI
 * storage, not to `Services.console`, so every `[tbmcp]` line the add-on writes
 * was invisible to the one tool meant to surface it. Reading both stores is the
 * fix, and reading *only our own* events out of the second one is the constraint
 * — that store holds every other add-on's output too.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { fakeSandbox, loadExperiment } from "./harness.mjs";

const ADDON_ID = "bridge@thunderbird-mcp";
const BASE_URL = "moz-extension://11111111-2222-3333-4444-555555555555/";
const STORAGE = "@mozilla.org/consoleAPI-storage;1";

/** Values built inside the vm carry that realm's prototypes; strip them. */
function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

/** An `nsIScriptError` as `Services.console.getMessageArray()` hands it over. */
function systemEntry(message, at) {
  return {
    message,
    QueryInterface: () => ({
      flags: 0,
      category: "chrome javascript",
      sourceName: "chrome://tbmcp/content/x.js",
      lineNumber: 12,
      timeStamp: at,
    }),
  };
}

/** A ConsoleAPI event, as an extension page's `console.*` leaves it. */
function consoleEvent({ level = "log", args, at, addonId, innerID }) {
  return { level, arguments: args, timeStamp: at, addonId, innerID };
}

/**
 * The admin module over both console stores.
 *
 * `getAPI` is called because that is where the add-on learns its own id — the
 * privileged half has no other way to know which events are its own.
 */
function experiment({ system = [], events = [], storageFails = false, identify = true } = {}) {
  const globals = fakeSandbox();
  globals.Ci = {
    nsIScriptError: { warningFlag: 1 },
    nsIConsoleAPIStorage: "nsIConsoleAPIStorage",
  };
  globals.Services = { ...globals.Services, console: { getMessageArray: () => system } };
  globals.Cc = {
    [STORAGE]: {
      getService() {
        if (storageFails) {
          throw new Error("no such service");
        }
        return { getEvents: () => events };
      },
    },
  };
  const ctx = loadExperiment(globals, { modules: ["admin"] });
  if (identify) {
    new ctx.tbx().getAPI({ extension: { id: ADDON_ID, baseURL: BASE_URL } });
  }
  return (params) => ctx.TBX_TEST_HOOKS.core.handlers["admin.consoleMessages"](params || {});
}

const AT = Date.parse("2026-09-08T15:00:00.000Z");

describe("admin.consoleMessages", () => {
  it("includes the add-on's own console output, which Services.console never holds", async () => {
    const call = experiment({
      events: [
        consoleEvent({ level: "warn", args: ["[tbmcp]", "no welcome"], at: AT, addonId: ADDON_ID }),
      ],
    });

    const result = await call({});

    assert.deepEqual(plain(result.messages), [
      {
        at: new Date(AT).toISOString(),
        severity: "warn",
        message: "[tbmcp] no welcome",
        source: "console",
      },
    ]);
  });

  it("leaves another add-on's console output where it found it", async () => {
    const call = experiment({
      events: [
        consoleEvent({ args: ["ours"], at: AT, addonId: ADDON_ID }),
        consoleEvent({ args: ["theirs"], at: AT + 1, addonId: "someone@else" }),
        consoleEvent({ args: ["nobody's"], at: AT + 2, innerID: "moz-extension://other/x.js" }),
      ],
    });

    const result = await call({});

    assert.deepEqual(
      plain(result.messages.map((row) => row.message)),
      ["ours"]
    );
  });

  it("recognises an event by its innerID when it carries no addonId", async () => {
    const call = experiment({
      events: [consoleEvent({ args: ["from our page"], at: AT, innerID: `${BASE_URL}background.js` })],
    });

    const result = await call({});

    assert.deepEqual(
      plain(result.messages.map((row) => row.message)),
      ["from our page"]
    );
  });

  it("merges the two stores in time order, newest last", async () => {
    const call = experiment({
      system: [systemEntry("first, from XPCOM", AT), systemEntry("third, from XPCOM", AT + 2000)],
      events: [consoleEvent({ args: ["second, from us"], at: AT + 1000, addonId: ADDON_ID })],
    });

    const result = await call({});

    assert.deepEqual(
      plain(result.messages.map((row) => row.message)),
      ["first, from XPCOM", "second, from us", "third, from XPCOM"]
    );
    assert.equal(result.buffered, 3);
  });

  it("redacts our own lines too — a token in a log is still a token", async () => {
    const call = experiment({
      events: [
        consoleEvent({ args: ["[tbmcp] token: abcd1234efgh"], at: AT, addonId: ADDON_ID }),
      ],
    });

    const result = await call({});

    assert.match(result.messages[0].message, /<redacted>/);
    assert.doesNotMatch(result.messages[0].message, /abcd1234efgh/);
  });

  it("filters across both stores", async () => {
    const call = experiment({
      system: [systemEntry("unrelated warning", AT)],
      events: [consoleEvent({ args: ["[tbmcp] connecting"], at: AT + 1, addonId: ADDON_ID })],
    });

    const result = await call({ filter: "tbmcp" });

    assert.deepEqual(
      plain(result.messages.map((row) => row.message)),
      ["[tbmcp] connecting"]
    );
    assert.equal(result.matched, 1);
    assert.equal(result.buffered, 2);
  });

  it("still answers when this build has no ConsoleAPI storage", async () => {
    const call = experiment({ system: [systemEntry("from XPCOM", AT)], storageFails: true });

    const result = await call({});

    assert.deepEqual(
      plain(result.messages.map((row) => row.message)),
      ["from XPCOM"]
    );
  });

  it("claims no events at all until it knows which add-on it is", async () => {
    const call = experiment({
      system: [systemEntry("from XPCOM", AT)],
      events: [consoleEvent({ args: ["ours"], at: AT + 1, addonId: ADDON_ID })],
      identify: false,
    });

    const result = await call({});

    assert.deepEqual(
      plain(result.messages.map((row) => row.message)),
      ["from XPCOM"]
    );
  });
});

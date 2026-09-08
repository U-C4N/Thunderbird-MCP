/* Gloda — the global index, and the four ways a search of it came back empty.
 *
 * Every defect here was invisible in a unit test that built its own Date or its
 * own folder id, and obvious against a real profile: gloda hands back objects
 * from another realm, WebExtension folder ids look like URIs, and a term the
 * tokenizer throws away still reaches the SQL and zeroes the query.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import vm from "node:vm";

import { fakeSandbox, loadExperiment } from "./harness.mjs";

const ACCOUNTS = [
  { key: "account1", incomingServer: { rootFolder: { URI: "imap://me@example.com" } } },
];

/** The gloda module's pure helpers, with `accounts` as MailServices reports them. */
function hooks({ accounts = ACCOUNTS, ...options } = {}) {
  const globals = fakeSandbox({
    ...options,
    modules: {
      "resource:///modules/MailServices.sys.mjs": { MailServices: { accounts: { accounts } } },
      ...(options.modules || {}),
    },
  });
  return loadExperiment(globals, { modules: ["gloda"] }).TBX_TEST_HOOKS.gloda;
}

describe("isoDate", () => {
  it("dates a gloda hit whose Date was minted in another realm", () => {
    const { isoDate } = hooks();

    // `instanceof Date` is false for this one, which is why every hit was null.
    assert.equal(isoDate(vm.runInNewContext("new Date(0)")), "1970-01-01T00:00:00.000Z");
  });

  it("still reads a raw PRTime, and nothing out of nothing", () => {
    const { isoDate } = hooks();

    assert.equal(isoDate(1000000), "1970-01-01T00:00:01.000Z");
    assert.equal(isoDate(null), null);
    assert.equal(isoDate(undefined), null);
  });
});

describe("folderMatcher", () => {
  it("matches a WebExtension folder id against the hit's own id", () => {
    const matches = hooks().folderMatcher("account1://INBOX");

    assert.equal(matches({ folderId: "account1://INBOX" }), true);
    assert.equal(matches({ folderId: "account1://Sent" }), false);
  });

  it("matches a folder URI under an account's root against the hit's URI", () => {
    const matches = hooks().folderMatcher("imap://me@example.com/INBOX");

    assert.equal(matches({ folderUri: "imap://me@example.com/INBOX" }), true);
    assert.equal(matches({ folderUri: "imap://me@example.com/Sent" }), false);
  });

  it("says so when the reference belongs to no account here", () => {
    assert.throws(
      () => hooks().folderMatcher("accountZZ://nope"),
      (ex) => {
        assert.equal(ex.tbxKind, "usage");
        assert.match(ex.message, /accountZZ/);
        return true;
      }
    );
  });
});

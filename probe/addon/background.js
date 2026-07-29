/* Capability probe, non-privileged half.
 *
 * Verifies which WebExtension namespaces exist, whether the experiment API
 * loaded, and which outbound transports a background page can actually use to
 * reach a Python process on loopback. Everything is written to
 * <profile>/tbmcp-probe-report.json so the result survives a failed transport.
 */

const HTTP_PORT = 8271;
const WS_PORT = 8271;
const REPORT_FILE = "tbmcp-probe-report.json";

/** Namespaces we care about for the MCP server. */
const NAMESPACES = [
  "accounts", "addressBooks", "contacts", "mailingLists", "compose",
  "composeScripts", "folders", "identities", "mailTabs", "menus",
  "messageDisplay", "messages", "messengerSettings", "messengerUtilities",
  "scripting", "sessions", "spaces", "spacesToolbar", "tabs", "theme",
  "windows", "cloudFile", "commands", "storage", "runtime", "downloads",
  "notifications", "alarms", "idle", "management", "privacy", "proxy",
  "browserSettings", "pkcs11", "tbx",
];

function timeout(ms, promise) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), ms)),
  ]);
}

/** Enumerate which browser.* namespaces are actually exposed to us. */
function namespaceSupport() {
  const present = {};
  for (const ns of NAMESPACES) {
    const value = browser[ns];
    present[ns] = value
      ? Object.keys(value).filter(k => typeof value[k] === "function").length || true
      : false;
  }
  return present;
}

/** Can a background page open a WebSocket to loopback? */
function testWebSocket() {
  return new Promise(resolve => {
    let socket;
    const done = result => {
      try {
        socket && socket.close();
      } catch (ex) {
        /* ignore */
      }
      resolve(result);
    };
    const timer = setTimeout(() => done({ ok: false, error: "timeout (no server?)" }), 4000);
    try {
      socket = new WebSocket(`ws://127.0.0.1:${WS_PORT}/probe`);
      socket.onopen = () => socket.send(JSON.stringify({ hello: "tbmcp-probe" }));
      socket.onmessage = event => {
        clearTimeout(timer);
        done({ ok: true, echo: String(event.data).slice(0, 200) });
      };
      socket.onerror = () => {
        clearTimeout(timer);
        done({ ok: false, error: "onerror (blocked, or no server listening)" });
      };
    } catch (ex) {
      clearTimeout(timer);
      done({ ok: false, error: String(ex.message || ex) });
    }
  });
}

/** Can a background page fetch()/POST to loopback (long-poll fallback)? */
async function testFetch() {
  try {
    const response = await timeout(
      4000,
      fetch(`http://127.0.0.1:${HTTP_PORT}/probe-http`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hello: "tbmcp-probe" }),
      })
    );
    return { ok: true, status: response.status, body: (await response.text()).slice(0, 200) };
  } catch (ex) {
    return { ok: false, error: String(ex.message || ex) };
  }
}

/** A couple of read-only calls to confirm the official API really works here. */
async function testOfficialApi() {
  const result = {};
  try {
    const accounts = await browser.accounts.list(false);
    result.accounts = accounts.map(a => ({ id: a.id, type: a.type, identities: a.identities.length }));
  } catch (ex) {
    result.accountsError = String(ex.message || ex);
  }
  try {
    const tags = await browser.messages.tags.list();
    result.tagCount = tags.length;
  } catch (ex) {
    result.tagsError = String(ex.message || ex);
  }
  try {
    // fullText is the Gloda-backed search path we want to expose as a tool.
    const page = await browser.messages.query({ fullText: "invoice", messagesPerPage: 5 });
    result.queryFullText = { messages: page.messages.length, hasListId: !!page.id };
  } catch (ex) {
    result.queryFullTextError = String(ex.message || ex);
  }
  try {
    const folders = await browser.folders.query({ isVirtual: true });
    result.virtualFolderCount = folders.length;
  } catch (ex) {
    result.virtualFolderError = String(ex.message || ex);
  }
  return result;
}

async function main() {
  const report = {
    generatedAt: new Date().toISOString(),
    manifestVersion: browser.runtime.getManifest().manifest_version,
    namespaces: namespaceSupport(),
  };

  report.privileged = browser.tbx
    ? await browser.tbx.probe().catch(ex => ({ error: String(ex.message || ex) }))
    : { error: "experiment API not available — unsigned experiment load FAILED" };

  report.officialApi = await testOfficialApi();
  report.transports = {
    webSocket: await testWebSocket(),
    fetch: await testFetch(),
  };

  const text = JSON.stringify(report, null, 2);
  console.log("[tbmcp-probe] report ready", text);

  if (browser.tbx) {
    try {
      const path = await browser.tbx.writeReport(REPORT_FILE, text);
      console.log("[tbmcp-probe] written to", path);
    } catch (ex) {
      console.error("[tbmcp-probe] write failed", ex);
    }
  }
  await browser.storage.local.set({ report });
}

main().catch(ex => console.error("[tbmcp-probe] fatal", ex));

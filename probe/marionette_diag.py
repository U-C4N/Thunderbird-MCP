"""Ask a running Thunderbird (started with -marionette -remote-allow-system-access)
why an XPI was rejected: parse its manifest directly and dump console errors."""

from __future__ import annotations

import json
import sys

from marionette_probe import Marionette

SCRIPT = r"""
const xpiPath = arguments[0];
const done = arguments[arguments.length - 1];
(async () => {
  const out = {};
  // 1) Recent console messages mentioning the add-on or XPI machinery.
  try {
    const messages = Services.console.getMessageArray() || [];
    out.console = messages
      .map(m => String(m.message || m))
      .filter(t => /xpi|addon|extension|manifest|probe|thunderbird-mcp/i.test(t))
      .slice(-40);
  } catch (e) { out.consoleError = String(e); }

  // 2) Parse the manifest straight out of the zip via ExtensionData.
  try {
    const { ExtensionData } = ChromeUtils.importESModule("resource://gre/modules/Extension.sys.mjs");
    const fileUri = Services.io.newFileURI(new (ChromeUtils.importESModule(
      "resource://gre/modules/FileUtils.sys.mjs").FileUtils.File)(xpiPath));
    const rootURI = Services.io.newURI("jar:" + fileUri.spec + "!/");
    const data = new ExtensionData(rootURI, /* isPrivileged */ false);
    try {
      await data.loadManifest();
      out.manifest = {
        ok: true, id: data.id, type: data.type,
        manifestVersion: data.manifest && data.manifest.manifest_version,
        hasExperimentApis: !!(data.manifest && data.manifest.experiment_apis),
        canUseAPIExperiment: data.canUseAPIExperiment(),
        errors: data.errors, warnings: data.warnings,
      };
    } catch (e) {
      out.manifest = { ok: false, thrown: String(e && e.message || e),
                       errors: data.errors, warnings: data.warnings };
    }
  } catch (e) { out.manifestSetupError = String(e && e.stack || e); }

  // 3) Can we even read the zip?
  try {
    const zr = Cc["@mozilla.org/libjar/zip-reader;1"].createInstance(Ci.nsIZipReader);
    const { FileUtils } = ChromeUtils.importESModule("resource://gre/modules/FileUtils.sys.mjs");
    zr.open(new FileUtils.File(xpiPath));
    const entries = [];
    const it = zr.findEntries("*");
    while (it.hasMore()) { entries.push(it.getNext()); }
    zr.close();
    out.zip = { ok: true, entries };
  } catch (e) { out.zip = { ok: false, error: String(e && e.message || e) }; }

  done(out);
})().catch(e => done({ fatalError: String(e && e.stack || e) }));
"""


def main() -> int:
    xpi = sys.argv[1]
    client = Marionette()
    client.send("WebDriver:NewSession", {"capabilities": {}})
    client.send("Marionette:SetContext", {"value": "chrome"})
    result = client.send(
        "WebDriver:ExecuteAsyncScript",
        {"script": SCRIPT, "args": [xpi], "newSandbox": False, "scriptTimeout": 60000},
    )
    print(json.dumps(result, indent=2))
    client.sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

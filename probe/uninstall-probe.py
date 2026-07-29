"""Remove the capability probe add-on from a Marionette-enabled Thunderbird."""

from __future__ import annotations

import json
import sys

from marionette_probe import Marionette

SCRIPT = r"""
const done = arguments[arguments.length - 1];
(async () => {
  const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
  const addon = await AddonManager.getAddonByID(arguments[0]);
  if (!addon) { done({ removed: false, reason: "not installed" }); return; }
  await addon.uninstall();
  done({ removed: true, version: addon.version });
})().catch(e => done({ fatalError: String((e && e.stack) || e) }));
"""


def main() -> int:
    client = Marionette()
    client.send("WebDriver:NewSession", {"capabilities": {}})
    client.send("Marionette:SetContext", {"value": "chrome"})
    result = client.send(
        "WebDriver:ExecuteAsyncScript",
        {"script": SCRIPT, "args": ["probe@thunderbird-mcp.local"], "newSandbox": False},
    )
    print(json.dumps(result, indent=2))
    client.sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

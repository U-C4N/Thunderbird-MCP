"""Dump console messages and extension state for the probe add-on."""

from __future__ import annotations

import json
import sys

from marionette_probe import Marionette

SCRIPT = r"""
const done = arguments[arguments.length - 1];
(async () => {
  const out = {};
  const ID = "probe@thunderbird-mcp.local";
  try {
    const policy = WebExtensionPolicy.getByID(ID);
    out.policy = policy
      ? { active: policy.active, mv: policy.manifestVersion, baseURL: policy.getURL("") }
      : null;
  } catch (e) { out.policyError = String(e); }

  try {
    const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
    const addon = await AddonManager.getAddonByID(ID);
    out.addon = addon ? {
      version: addon.version, isActive: addon.isActive, appDisabled: addon.appDisabled,
      userDisabled: addon.userDisabled, signedState: addon.signedState,
      isPrivileged: addon.isPrivileged, installLocation: addon.installLocation?.name,
    } : null;
  } catch (e) { out.addonError = String(e); }

  try {
    const messages = Services.console.getMessageArray() || [];
    out.console = messages.map(m => String(m.message || m))
      .filter(t => /probe|tbmcp|thunderbird-mcp|tbx|experiment/i.test(t))
      .slice(-40);
  } catch (e) { out.consoleError = String(e); }
  done(out);
})().catch(e => done({ fatalError: String(e && e.stack || e) }));
"""


def main() -> int:
    client = Marionette()
    client.send("WebDriver:NewSession", {"capabilities": {}})
    client.send("Marionette:SetContext", {"value": "chrome"})
    print(
        json.dumps(
            client.send(
                "WebDriver:ExecuteAsyncScript",
                {"script": SCRIPT, "args": [], "newSandbox": False, "scriptTimeout": 30000},
            ),
            indent=2,
        )
    )
    client.sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

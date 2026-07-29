#!/usr/bin/env python3
"""Development helper: read Thunderbird's error console over Marionette.

    python tools/tb_console.py [pattern]

Restarts Thunderbird with automation enabled, dumps matching console messages and
the bridge add-on's state, then restarts it normally. Marionette is a wide-open
local automation channel, so it is never left listening.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from tbmcp import marionette
from tbmcp.addon_build import addon_id
from tbmcp.addon_install import _launch, _stop, find_thunderbird

SCRIPT = r"""
const pattern = arguments[0];
const addonId = arguments[1];
const done = arguments[arguments.length - 1];
(async () => {
  const out = {};
  const rx = pattern ? new RegExp(pattern, "i") : /tbmcp|thunderbird-mcp/i;
  try {
    out.messages = (Services.console.getMessageArray() || [])
      .map(m => String(m.message || m))
      .filter(t => rx.test(t))
      .slice(-60);
  } catch (e) { out.messagesError = String(e); }

  try {
    const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
    const addon = await AddonManager.getAddonByID(addonId);
    out.addon = addon ? {
      version: addon.version, isActive: addon.isActive, appDisabled: addon.appDisabled,
      userDisabled: addon.userDisabled, signedState: addon.signedState,
    } : null;
    const policy = WebExtensionPolicy.getByID(addonId);
    out.policy = policy ? { active: policy.active, baseURL: policy.getURL("") } : null;
  } catch (e) { out.addonError = String(e); }

  // Is the pairing file where the add-on expects it, and readable?
  try {
    const path = PathUtils.join(
      Services.dirsvc.get("ProfD", Ci.nsIFile).path, "tbmcp-bridge.json");
    out.pairing = (await IOUtils.exists(path))
      ? { exists: true, port: (await IOUtils.readJSON(path)).port }
      : { exists: false };
  } catch (e) { out.pairing = { error: String(e) }; }

  done(out);
})().catch(e => done({ fatalError: String((e && e.stack) || e) }));
"""


def main() -> int:
    pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    exe = find_thunderbird()
    if exe is None:
        print("could not find thunderbird")
        return 2

    _stop()
    _launch(exe, ["-marionette", "-remote-allow-system-access"], None)
    if not marionette.wait_for_port(timeout=90.0):
        print("marionette never opened")
        return 1
    # The background page needs a moment to run and fail.
    time.sleep(8)

    client = marionette.connect(timeout=60.0)
    try:
        client.start_chrome_session()
        report = client.execute(SCRIPT, [pattern, addon_id()], timeout_ms=60_000)
    finally:
        client.quit_application()

    print(json.dumps(report, indent=2, ensure_ascii=False))
    _stop(timeout=30.0)
    _launch(exe, [], None)
    print("\n(Thunderbird restarted without automation)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

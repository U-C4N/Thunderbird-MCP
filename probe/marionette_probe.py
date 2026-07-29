"""Minimal Marionette client used to (a) prove Thunderbird ships a privileged
automation channel and (b) install the unsigned experiment add-on without any UI.

Marionette framing is `<byte-length>:<json>`; commands are
`[0, msgId, name, params]` and responses `[1, msgId, error, result]`.
"""

from __future__ import annotations

import json
import socket
import sys
import time

HOST = "127.0.0.1"
PORT = 2828


class Marionette:
    def __init__(self, host: str = HOST, port: int = PORT, timeout: float = 60.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""
        self.msg_id = 0
        self.handshake = self._recv()

    # --- framing ---------------------------------------------------------
    def _recv_raw(self) -> bytes:
        while b":" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("marionette closed the connection")
            self.buf += chunk
        length_str, _, rest = self.buf.partition(b":")
        length = int(length_str)
        self.buf = rest
        while len(self.buf) < length:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("marionette closed mid-frame")
            self.buf += chunk
        payload, self.buf = self.buf[:length], self.buf[length:]
        return payload

    def _recv(self):
        return json.loads(self._recv_raw())

    def send(self, name: str, params: dict | None = None):
        self.msg_id += 1
        body = json.dumps([0, self.msg_id, name, params or {}]).encode()
        self.sock.sendall(str(len(body)).encode() + b":" + body)
        while True:
            message = self._recv()
            if isinstance(message, list) and message[0] == 1 and message[1] == self.msg_id:
                _, _, error, result = message
                if error:
                    raise RuntimeError(f"{name} failed: {json.dumps(error)[:800]}")
                return result

    def close(self) -> None:
        try:
            self.send("Marionette:Quit", {"flags": ["eForceQuit"]})
        except Exception:
            pass
        self.sock.close()


CAPABILITY_SCRIPT = r"""
const done = arguments[arguments.length - 1];
(async () => {
  const out = {};
  const { AppConstants } = ChromeUtils.importESModule("resource://gre/modules/AppConstants.sys.mjs");
  const { AddonSettings } = ChromeUtils.importESModule("resource://gre/modules/addons/AddonSettings.sys.mjs");
  const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
  out.app = {
    name: Services.appinfo.name,
    version: Services.appinfo.version,
    requireSigning: AppConstants.MOZ_REQUIRE_SIGNING,
    nightly: AppConstants.NIGHTLY_BUILD,
    channel: AppConstants.MOZ_UPDATE_CHANNEL,
  };
  out.addonSettings = {
    REQUIRE_SIGNING: AddonSettings.REQUIRE_SIGNING,
    EXPERIMENTS_ENABLED: AddonSettings.EXPERIMENTS_ENABLED,
    SCOPES_SIDELOAD: AddonSettings.SCOPES_SIDELOAD,
  };
  out.privileged = {
    canReadPrefs: Services.prefs.getIntPref("mail.pane_config.dynamic", -1),
    profileDir: Services.dirsvc.get("ProfD", Ci.nsIFile).path,
  };
  const { MailServices } = ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
  out.privileged.accountCount = MailServices.accounts.accounts.length;
  out.addons = (await AddonManager.getAllAddons()).map(a => ({
    id: a.id, type: a.type, active: a.isActive, signedState: a.signedState,
  }));
  done(out);
})().catch(e => done({ fatalError: String(e && e.stack || e) }));
"""

INSTALL_SCRIPT = r"""
const xpiPath = arguments[0];
const done = arguments[arguments.length - 1];
(async () => {
  const out = { xpiPath };
  const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
  const { FileUtils } = ChromeUtils.importESModule("resource://gre/modules/FileUtils.sys.mjs");
  const file = new FileUtils.File(xpiPath);
  out.fileExists = file.exists();
  const install = await AddonManager.getInstallForFile(file);
  if (!install) { done({ ...out, error: "getInstallForFile returned null" }); return; }
  out.initialState = install.state;
  out.installError = install.error;
  const finished = await new Promise(resolve => {
    const listener = {
      onInstallEnded(inst, addon) {
        resolve({ ok: true, addonId: addon.id, signedState: addon.signedState,
                  appDisabled: addon.appDisabled, userDisabled: addon.userDisabled,
                  isActive: addon.isActive, isPrivileged: addon.isPrivileged });
      },
      onInstallFailed(inst) { resolve({ ok: false, state: inst.state, error: inst.error }); },
      onInstallCancelled(inst) { resolve({ ok: false, cancelled: true, error: inst.error }); },
      onDownloadFailed(inst) { resolve({ ok: false, download: true, error: inst.error }); },
    };
    install.addListener(listener);
    setTimeout(() => resolve({ ok: false, timedOut: true, state: install.state, error: install.error }), 30000);
    install.install();
  });
  out.result = finished;
  done(out);
})().catch(e => done({ fatalError: String(e && e.stack || e) }));
"""


def main() -> int:
    xpi = sys.argv[1] if len(sys.argv) > 1 else None
    deadline = time.time() + 45
    client = None
    while time.time() < deadline:
        try:
            client = Marionette()
            break
        except OSError:
            time.sleep(1.0)
    if client is None:
        print("could not connect to marionette on 127.0.0.1:2828", flush=True)
        return 1

    print("handshake:", json.dumps(client.handshake), flush=True)
    session = client.send("WebDriver:NewSession", {"capabilities": {}})
    print("session:", json.dumps(session)[:300], flush=True)
    client.send("Marionette:SetContext", {"value": "chrome"})

    caps = client.send(
        "WebDriver:ExecuteAsyncScript",
        {"script": CAPABILITY_SCRIPT, "args": [], "newSandbox": False},
    )
    print("\n=== CAPABILITIES ===")
    print(json.dumps(caps, indent=2))

    if xpi:
        result = client.send(
            "WebDriver:ExecuteAsyncScript",
            {"script": INSTALL_SCRIPT, "args": [xpi], "newSandbox": False, "scriptTimeout": 60000},
        )
        print("\n=== INSTALL ===")
        print(json.dumps(result, indent=2))

    print("\n(leaving Thunderbird running)")
    client.sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

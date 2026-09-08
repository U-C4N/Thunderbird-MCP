/* Entry point. Loaded last, after every handler module has registered itself. */

(async () => {
  /* Nothing on the way to `tbxTransport.start()` may be able to hang. Every call
   * below crosses into the privileged half, which is the part that wedges, and a
   * bridge that never starts cannot even report that it did not. */
  const PROBE_TIMEOUT_MS = 5000;

  await tbxLog.init();
  const manifest = browser.runtime.getManifest();
  tbxLog.info(
    `bridge ${manifest.version} starting — ${tbxRegistry.methods().length} methods, ` +
      `privileged half ${browser.tbx ? "loaded" : "MISSING"}`
  );

  if (!browser.tbx) {
    // Everything under `x.*` is unavailable in this state. Say so once, loudly,
    // rather than letting each call fail on its own.
    tbxLog.error(
      "the experiment API did not load: preferences, account settings, filters and " +
        "calendar will be unavailable. Reinstall with `tbmcp install-addon`."
    );
  }

  // What the transport announces in its `hello`. Fetched here because the status
  // file needs the same two values, and because the handshake must never wait on a
  // probe: whatever we learn now is what the first hello carries.
  let identity = { app: null, capabilities: null };
  // The startup report, kept so the transport's state can be added to it later
  // without probing anything again.
  let report = null;

  // Leave a record on disk before doing anything that could fail. When the bridge
  // itself is broken there is no channel left to report through, so `tbmcp doctor`
  // reads this file instead — and its absence on an installed, active add-on is
  // itself the diagnosis: the privileged half never loaded.
  if (browser.tbx) {
    // First, and most important: stop Thunderbird suspending this page. It ships
    // extensions.eventPages.enabled=true, which makes MV2's "persistent": true a
    // no-op — so without this the page is suspended after 30s of quiet, taking its
    // timers and the bridge socket with it, and the connection only returns when some
    // unrelated mail event happens to wake us.
    try {
      const alive = await tbxCapabilities.bounded(
        browser.tbx.keepAlive(true),
        PROBE_TIMEOUT_MS
      );
      if (!alive) {
        throw new Error(`no answer within ${PROBE_TIMEOUT_MS}ms`);
      }
      tbxLog.info(
        alive.enabled
          ? `keep-alive on (every ${alive.intervalMs}ms; idle timeout ${alive.idleTimeoutMs}ms)`
          : `keep-alive not needed: ${alive.reason || "unknown"}`
      );
    } catch (ex) {
      tbxLog.error(
        "could not stop the background page being suspended, so the bridge will drop " +
          "out after about 30s of inactivity:",
        ex.message || ex
      );
    }

    // Unlocks browser.messages.sendMessage, which sends without opening a compose
    // window. It is an OptionalOnlyPermission, so it cannot be asked for in the
    // manifest and needs a click we do not have. See grantOptionalPermission.
    try {
      const grant = await tbxCapabilities.bounded(
        browser.tbx.grantOptionalPermission("messages.send"),
        PROBE_TIMEOUT_MS
      );
      if (!grant) {
        throw new Error(`no answer within ${PROBE_TIMEOUT_MS}ms`);
      }
      if (!grant.alreadyHad) {
        tbxLog.info(`granted messages.send: ${grant.granted}`);
      }
    } catch (ex) {
      tbxLog.warn(
        "could not grant messages.send, so sending will open a compose window:",
        ex.message || ex
      );
    }

    try {
      // Whatever these two do not answer in time stays null, and `hello` falls back
      // to the capability snapshots instead.
      const [capabilities, app] = await Promise.all([
        tbxCapabilities.bounded(tbxCapabilities.describe(), PROBE_TIMEOUT_MS),
        tbxCapabilities.bounded(tbxCapabilities.appInfo(), PROBE_TIMEOUT_MS),
      ]);
      identity = { app, capabilities };
      report = {
        writtenAt: new Date().toISOString(),
        addonVersion: manifest.version,
        app,
        capabilities,
        methodCount: tbxRegistry.methods().length,
      };
      const written = await tbxCapabilities.bounded(
        browser.tbx.writeStatus(report),
        PROBE_TIMEOUT_MS
      );
      if (!written) {
        throw new Error(`no answer within ${PROBE_TIMEOUT_MS}ms`);
      }
    } catch (ex) {
      tbxLog.warn("could not write the status file:", ex.message || ex);
    }
  }

  tbxEvents.start();
  tbxTransport.start(identity, {
    /* A connection that never works leaves nothing else to look at: the daemon
     * cannot report what it never heard from, and the console is gone by the time
     * anyone runs `tbmcp doctor`. So the same file the startup report goes into
     * gains the transport's own account of what has been happening. */
    onStateChange(transport) {
      if (!report) {
        return;
      }
      browser.tbx
        .writeStatus({ ...report, transport, writtenAt: new Date().toISOString() })
        .catch((ex) => {
          tbxLog.warn("could not update the status file:", ex.message || ex);
        });
    },
  });

  browser.runtime.onSuspend.addListener(() => {
    tbxLog.info("suspending — closing the bridge");
    tbxTransport.stop();
  });
})().catch((ex) => {
  console.error("[tbmcp] fatal during startup", ex);
});

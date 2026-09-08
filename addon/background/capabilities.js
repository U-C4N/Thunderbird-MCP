/* What this Thunderbird can actually do.
 *
 * Sent in the `hello` frame so the daemon can answer "why did that fail?" without
 * a round trip, and so `tbmcp doctor` can explain a partial install: if the
 * privileged half did not load, every `x.*` method is reported missing up front
 * rather than failing one at a time.
 *
 * Both probes cache what they learn, because `hello` cannot wait for them: the
 * privileged half answers over a message port that a wedged Thunderbird can leave
 * hanging indefinitely, and a handshake that never starts is far worse than one
 * carrying a slightly stale description.
 */

var tbxCapabilities = (() => {
  const WATCHED_NAMESPACES = [
    "accounts",
    "addressBooks",
    "compose",
    "folders",
    "identities",
    "mailTabs",
    "messageDisplay",
    "messages",
    "messengerSettings",
    "messengerUtilities",
    "sessions",
    "tabs",
    "windows",
  ];

  let lastAppInfo = null;
  let lastPrivilegedModules = [];

  /** What we know without asking anyone: the manifest, and our own version. */
  function manifestAppInfo() {
    const manifest = browser.runtime.getManifest();
    return { name: "Thunderbird", version: "unknown", addon: manifest.version };
  }

  /** Everything except the privileged module list, which needs a round trip. */
  function snapshot(privilegedModules) {
    return {
      experiment: Boolean(browser.tbx),
      namespaces: WATCHED_NAMESPACES.filter((name) => Boolean(browser[name])),
      privilegedModules,
      methods: tbxRegistry.methods(),
      // Surfaced so the server can adapt instead of guessing from a version number.
      features: {
        headlessSend: Boolean(browser.messages && browser.messages.sendMessage),
        messageImport: Boolean(browser.messages && browser.messages.import),
        tagsNamespace: Boolean(browser.messages && browser.messages.tags),
        folderQuery: Boolean(browser.folders && browser.folders.query),
        unifiedFolders: Boolean(browser.folders && browser.folders.getUnifiedFolder),
      },
    };
  }

  return {
    /**
     * `promise`, but it always settles, and never with a rejection.
     *
     * Anything that crosses into the privileged half can hang — that is the failure
     * this add-on keeps meeting — and a startup step that hangs takes the bridge
     * with it, because nothing after it ever runs. Callers get `fallback` instead
     * and carry on with what they already know.
     *
     * @param {Promise} promise
     * @param {number} ms  how long the caller is prepared to wait.
     * @param {*} [fallback]  what to resolve with when it does not answer.
     */
    bounded(promise, ms, fallback = null) {
      return new Promise((resolve) => {
        const timer = setTimeout(() => resolve(fallback), ms);
        const settle = (value) => {
          clearTimeout(timer);
          resolve(value);
        };
        promise.then(settle, (ex) => {
          tbxLog.debug("a privileged call failed:", ex.message || ex);
          settle(fallback);
        });
      });
    },

    async appInfo() {
      if (browser.tbx) {
        try {
          lastAppInfo = await browser.tbx.appInfo();
          return lastAppInfo;
        } catch (ex) {
          tbxLog.debug("appInfo failed:", tbxError.readable(ex));
        }
      }
      // The cache is only written by an answer. A probe that failed must not be
      // able to downgrade what we already know — the transport re-probes after
      // every welcome, and a wedged privileged half would otherwise turn a good
      // `hello` into one claiming this is Thunderbird "unknown".
      return lastAppInfo || manifestAppInfo();
    },

    /** The last answer `appInfo()` produced, or the manifest before there was one. */
    appInfoSync() {
      return lastAppInfo || manifestAppInfo();
    },

    async describe() {
      if (browser.tbx) {
        try {
          lastPrivilegedModules = await browser.tbx.availableModules();
        } catch (ex) {
          // Same as above: keep the last list we were actually given.
          tbxLog.warn(
            "the privileged half loaded but is not answering:",
            tbxError.readable(ex)
          );
        }
      }
      return snapshot(lastPrivilegedModules);
    },

    /** `describe()` minus the round trip: the module list is whatever it last saw. */
    describeSync() {
      return snapshot(lastPrivilegedModules);
    },
  };
})();

/* What this Thunderbird can actually do.
 *
 * Sent in the `hello` frame so the daemon can answer "why did that fail?" without
 * a round trip, and so `tbmcp doctor` can explain a partial install: if the
 * privileged half did not load, every `x.*` method is reported missing up front
 * rather than failing one at a time.
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

  return {
    async appInfo() {
      if (browser.tbx) {
        try {
          return await browser.tbx.appInfo();
        } catch (ex) {
          tbxLog.debug("appInfo failed:", ex.message || ex);
        }
      }
      const manifest = browser.runtime.getManifest();
      return { name: "Thunderbird", version: "unknown", addon: manifest.version };
    },

    async describe() {
      const namespaces = WATCHED_NAMESPACES.filter((name) => Boolean(browser[name]));
      const privileged = Boolean(browser.tbx);
      let privilegedModules = [];
      if (privileged) {
        try {
          privilegedModules = await browser.tbx.availableModules();
        } catch (ex) {
          tbxLog.warn("the privileged half loaded but is not answering:", ex.message || ex);
        }
      }
      return {
        experiment: privileged,
        namespaces,
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
    },
  };
})();

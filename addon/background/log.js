/* Logging that stays useful in Thunderbird's error console.
 *
 * Everything is prefixed so a user can filter the console down to this add-on,
 * and levels are gated on a stored flag so a normal install is quiet.
 */

var tbxLog = (() => {
  const PREFIX = "[tbmcp]";
  let verbose = false;

  return {
    async init() {
      try {
        const stored = await browser.storage.local.get("verbose");
        verbose = Boolean(stored && stored.verbose);
      } catch (ex) {
        // storage is unavailable during very early startup; stay quiet.
      }
    },
    setVerbose(value) {
      verbose = Boolean(value);
      browser.storage.local.set({ verbose }).catch(() => {});
    },
    get isVerbose() {
      return verbose;
    },
    debug(...args) {
      if (verbose) {
        console.debug(PREFIX, ...args);
      }
    },
    info(...args) {
      console.info(PREFIX, ...args);
    },
    warn(...args) {
      console.warn(PREFIX, ...args);
    },
    error(...args) {
      console.error(PREFIX, ...args);
    },
  };
})();

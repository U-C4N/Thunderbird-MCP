/* Bridge every `x.*` method onto the privileged half.
 *
 * There is no per-method plumbing here on purpose: the background page cannot do
 * anything useful with a preference or a filter anyway, so it forwards verbatim and
 * lets the privileged module own validation. Adding a privileged capability means
 * adding one file under experiment/modules/ and one Python tool — nothing here.
 */

{
  /** Method names the privileged half exposes, discovered at connect time. */
  let known = null;

  async function privilegedMethods() {
    if (known) {
      return known;
    }
    if (!browser.tbx) {
      known = [];
      return known;
    }
    try {
      const report = await browser.tbx.availableModules();
      known = report.methods || [];
    } catch (ex) {
      tbxLog.warn("could not enumerate privileged methods:", ex.message || ex);
      known = [];
    }
    return known;
  }

  /**
   * A single catch-all handler. `tbxRegistry.invoke` only reaches us for methods
   * that were registered, so we register the prefix itself and let the transport's
   * unknown-method path handle typos — but the daemon addresses us by full method
   * name, so we register a proxy under each name the privileged half reports.
   *
   * Because the privileged half is queried lazily, registration happens on first
   * use rather than at load: we register a wildcard by overriding invoke below.
   */
  const forward = (method) => async (params) => {
    if (!browser.tbx) {
      throw tbxError.unsupported(
        `${method} needs the privileged half of the add-on, which did not load. ` +
          "Reinstall with `tbmcp install-addon`, then check Thunderbird's error console."
      );
    }
    const bare = method.startsWith("x.") ? method.slice(2) : method;
    const available = await privilegedMethods();
    if (available.length && !available.includes(bare)) {
      throw tbxError.usage(
        `${method} is not available in this add-on build (known: ${available
          .map((m) => `x.${m}`)
          .join(", ")})`
      );
    }
    try {
      return await browser.tbx.invoke(bare, params);
    } catch (ex) {
      // The privileged half packs `tbx:` + a JSON payload into the message,
      // because ExtensionCommon.normalizeError rebuilds the Error on the way
      // across and drops every property but the message. Unpack it when present:
      // `needs` has to arrive as data, since PROTOCOL.md has clients read it to
      // learn what would unblock the call.
      const raw = String(ex.message || ex);
      if (raw.startsWith("tbx:")) {
        let payload = null;
        try {
          payload = JSON.parse(raw.slice(4));
        } catch (parseError) {
          payload = null;
        }
        if (payload && typeof payload.message === "string") {
          if (payload.kind === "blocked") {
            throw tbxError.blocked(payload.message, payload.needs);
          }
          const make = tbxError[payload.kind] || tbxError.thunderbird;
          throw make(payload.message);
        }
      }
      // Untagged: something outside invoke() threw. Guess from the text, as before.
      const message = raw;
      if (/ is required|must be|unknown privileged method|not an? /.test(message)) {
        throw tbxError.usage(message);
      }
      if (/does not expose|unavailable/.test(message)) {
        throw tbxError.unsupported(message);
      }
      if (/not writable|will not be written|locked by/.test(message)) {
        throw tbxError.blocked(message);
      }
      throw tbxError.thunderbird(message);
    }
  };

  /* The set of privileged methods is fixed at build time by which module files
   * were spliced in, so enumerate them eagerly at startup and register a proxy for
   * each. Until that resolves, `x.*` calls fall through to a lazy proxy. */
  const PREREGISTERED = [
    "accounts.copiesAndFolders", "accounts.identities", "accounts.junkSettings",
    "accounts.list", "accounts.serverSettings", "accounts.setCopiesAndFolders",
    "accounts.setJunkSetting", "accounts.setServerSetting", "accounts.setSyncSetting",
    "accounts.syncSettings",
    "admin.addons", "admin.appInfo", "admin.compactFolders", "admin.consoleMessages",
    "admin.diagnostics", "admin.downloadForOffline", "admin.restart", "admin.setAddonEnabled",
    "calendar.createCalendar", "calendar.createEvent", "calendar.createTask",
    "calendar.deleteCalendar", "calendar.deleteItem", "calendar.getItem",
    "calendar.listCalendars", "calendar.listItems", "calendar.updateCalendar",
    "calendar.updateEvent", "calendar.updateTask",
    "files.read", "files.stat", "files.write",
    "filters.create", "filters.delete", "filters.list", "filters.reorder", "filters.run",
    "filters.setEnabled", "filters.update",
    "gloda.conversation", "gloda.search", "gloda.stats",
    "identities.get", "identities.set", "identities.setEncryption", "identities.setSignature",
    "junk.classify", "junk.getSettings", "junk.resetTraining", "junk.setSettings",
    "junk.train",
    "openpgp.exportKey", "openpgp.importKey", "openpgp.keyDetails", "openpgp.listKeys",
    "prefs.get", "prefs.getMany", "prefs.list", "prefs.reset", "prefs.set", "prefs.userSet",
    "smtp.create", "smtp.delete", "smtp.list", "smtp.setDefault", "smtp.update",
    "vfolders.create", "vfolders.delete", "vfolders.list", "vfolders.update",
  ];

  for (const bare of PREREGISTERED) {
    const method = `x.${bare}`;
    tbxRegistry.define(method, forward(method));
  }
}

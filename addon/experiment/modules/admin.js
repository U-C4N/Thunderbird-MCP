/* Application state, add-ons, folder maintenance and the error console.
 *
 * This is where "why did that fail?" ends up, so nothing here is allowed to fail
 * all at once: `admin.diagnostics` probes each area independently and returns an
 * `error` field for the parts that did not answer. A report that throws because
 * one getter is broken is the report you cannot get when you need it.
 */

TBX_MODULE_NAMES.push("admin");

{
  const OUR_ADDON_ID = "bridge@thunderbird-mcp";

  /** AddonManager.SIGNEDSTATE_* as a word. A bare 0 reads like a failure, when for
   *  our deliberately unsigned XPI it is the expected value. */
  const SIGNED_STATES = {
    "-2": "broken",
    "-1": "unknown",
    0: "missing",
    1: "preliminary",
    2: "signed",
    3: "system",
    4: "privileged",
  };

  /** Console lines routinely carry auth headers and our own bridge token. A
   *  debugging aid must not become an exfiltration channel, so anything shaped like
   *  a credential is blanked before it leaves Thunderbird. */
  const SECRETISH = [
    /((?:pass(?:word|wd)?|secret|token|api[-_]?key|auth[-_]?key)["'\s]*[:=]["'\s]*)([^\s"',;}]{3,})/gi,
    /((?:authorization|proxy-authorization)\s*:\s*)(.+)/gi,
  ];

  function redact(text) {
    let out = String(text);
    for (const pattern of SECRETISH) {
      out = out.replace(pattern, "$1<redacted>");
    }
    return out;
  }

  /** Toolkit modules no other capability needs, so they are absent from core.js's
   *  MODULE_URLS. Imported defensively: a rename must cost one field of a report,
   *  not the report. */
  function importOrNull(url) {
    try {
      return ChromeUtils.importESModule(url);
    } catch (ex) {
      return null;
    }
  }

  function dirPath(key) {
    try {
      return Services.dirsvc.get(key, Ci.nsIFile).path;
    } catch (ex) {
      return null;
    }
  }

  /** The build's stance on unsigned and experiment add-ons.
   *
   *  These two decide whether our XPI can install at all and whether its
   *  `experiment_apis` are honoured, which between them explain every failed
   *  install we have seen. AddonSettings is a thin cache over the same two prefs,
   *  so reading the prefs is a faithful fallback when the module moves. */
  function buildFlags() {
    const out = {};
    const constants = importOrNull("resource://gre/modules/AppConstants.sys.mjs");
    if (constants && constants.AppConstants) {
      out.updateChannel = constants.AppConstants.MOZ_UPDATE_CHANNEL;
      out.requireSigningBuild = constants.AppConstants.MOZ_REQUIRE_SIGNING;
      out.nightly = constants.AppConstants.NIGHTLY_BUILD;
    } else {
      out.updateChannel = Services.prefs.getCharPref("app.update.channel", null);
    }
    const settings = importOrNull("resource://gre/modules/addons/AddonSettings.sys.mjs");
    if (settings && settings.AddonSettings) {
      out.signaturesRequired = settings.AddonSettings.REQUIRE_SIGNING;
      out.experimentsEnabled = settings.AddonSettings.EXPERIMENTS_ENABLED;
    } else {
      out.signaturesRequired = Services.prefs.getBoolPref("xpinstall.signatures.required", true);
      out.experimentsEnabled = Services.prefs.getBoolPref("extensions.experiments.enabled", false);
    }
    out.unsignedAddonsAllowed = out.signaturesRequired === false;
    out.experimentApisAllowed = out.experimentsEnabled === true;
    return out;
  }

  /** core.js's tbx.appInfo() answers the handshake with the bare minimum; this adds
   *  the build flags and is what diagnostics embeds. */
  function appInfo() {
    return {
      name: Services.appinfo.name,
      version: Services.appinfo.version,
      buildID: Services.appinfo.appBuildID,
      platformVersion: Services.appinfo.platformVersion,
      os: Services.appinfo.OS,
      abi: H.safeGet(Services.appinfo, "XPCOMABI"),
      locale: Services.locale.appLocaleAsBCP47,
      profileDir: dirPath("ProfD"),
      installDir: dirPath("GreD"),
      build: buildFlags(),
    };
  }

  TBX_MODULES["admin.appInfo"] = async () => appInfo();

  /* --------------------------------------------------------------- add-ons */

  function describeAddon(addon) {
    return {
      id: addon.id,
      name: addon.name,
      version: addon.version,
      type: addon.type,
      isActive: addon.isActive,
      appDisabled: addon.appDisabled,
      userDisabled: addon.userDisabled,
      signedState: addon.signedState,
      signedStateName: SIGNED_STATES[String(addon.signedState)] || null,
      isBridge: addon.id === OUR_ADDON_ID,
    };
  }

  TBX_MODULES["admin.addons"] = async (params) => {
    const AddonManager = needMod("AddonManager");
    const wanted = params.type ? String(params.type) : null;
    const all = (await AddonManager.getAllAddons()) || [];
    const addons = all
      .filter((addon) => !wanted || addon.type === wanted)
      .map(describeAddon)
      .sort((a, b) => String(a.name).localeCompare(String(b.name)));
    return { addons, count: addons.length, installed: all.length };
  };

  TBX_MODULES["admin.setAddonEnabled"] = async (params) => {
    const id = H.need(params, "id");
    if (typeof params.enabled !== "boolean") {
      throw H.usage("enabled must be true or false");
    }
    if (id === OUR_ADDON_ID && !params.enabled) {
      throw H.blocked(
        "refusing to disable the bridge add-on: it is the connection this call arrived " +
          "on, so disabling it would cut the call off mid-flight and leave no way to " +
          "switch it back on from here",
        "turn it off in Thunderbird's Add-ons Manager if that is really what you want"
      );
    }
    const AddonManager = needMod("AddonManager");
    const addon = await AddonManager.getAddonByID(id);
    if (!addon) {
      throw H.usage(`no add-on with id ${id} — x.admin.addons lists the installed ids`);
    }
    const previous = describeAddon(addon);
    if (params.enabled) {
      await addon.enable();
    } else {
      await addon.disable();
    }
    const current = describeAddon(addon);
    return {
      id,
      previous,
      current,
      // enable() only clears the user's own disable; a compatibility or signature
      // block set by Thunderbird itself stays put and the add-on stays inert.
      note: current.appDisabled
        ? "Thunderbird still blocks this add-on (appDisabled), so it will not run."
        : undefined,
    };
  };

  /* ---------------------------------------------------------------- restart */

  /** Held here because a pending nsITimer that gets collected never fires. */
  let restartTimer = null;

  TBX_MODULES["admin.restart"] = async (params) => {
    const delayMs = Number.isInteger(params.delayMs)
      ? Math.min(Math.max(params.delayMs, 100), 10000)
      : 750;
    const flags = Ci.nsIAppStartup.eRestart | Ci.nsIAppStartup.eAttemptQuit;
    restartTimer = Cc["@mozilla.org/timer;1"].createInstance(Ci.nsITimer);
    // Quit from a timer callback rather than inline, so this reply is on the wire
    // before the process goes away — otherwise the caller sees a dropped socket and
    // cannot tell a restart from a crash. nsITimer is the one timer facility a
    // privileged sub-script is guaranteed to have.
    restartTimer.initWithCallback(
      () => {
        try {
          Services.startup.quit(flags);
        } catch (ex) {
          console.error("[tbmcp] restart failed:", ex.message || ex);
        }
      },
      delayMs,
      Ci.nsITimer.TYPE_ONE_SHOT
    );
    return {
      restarting: true,
      inMs: delayMs,
      note:
        "Thunderbird has not restarted yet — it will in a moment. This connection " +
        "drops, anything in flight fails, and the add-on reconnects a few seconds " +
        "after Thunderbird comes back.",
    };
  };

  /* ------------------------------------------------------ folder maintenance */

  /** Resolve a folder id from the official API ("<accountKey>://<path>") or a raw
   *  nsIMsgFolder URI. The privileged half knows nothing about WebExtension ids, and
   *  the Python layer passes on whatever folder_list produced. */
  function folderFor(id) {
    const text = String(id);
    const utils = mod("MailUtils");
    if (utils && typeof utils.getExistingFolder === "function") {
      try {
        const direct = utils.getExistingFolder(text);
        if (direct) {
          return direct;
        }
      } catch (ex) {
        // Not a folder URI; fall through to the WebExtension id form.
      }
    }
    const split = text.indexOf("://");
    if (split < 1) {
      throw H.usage(`${text} is not a folder id — pass one from folder_list`);
    }
    let folder = H.account(text.slice(0, split)).incomingServer.rootFolder;
    for (const segment of text.slice(split + 3).split("/")) {
      if (!segment) {
        continue;
      }
      const name = decodeURIComponent(segment);
      let child = null;
      try {
        child = folder.getChildNamed(name);
      } catch (ex) {
        child = null;
      }
      if (!child) {
        throw H.usage(`${text}: ${folder.name} has no subfolder named ${name}`);
      }
      folder = child;
    }
    return folder;
  }

  function folderBytes(folder) {
    const value = H.safeGet(folder, "sizeOnDisk");
    return typeof value === "number" ? value : null;
  }

  /** Not a property: getTotalMessages(deep) throws on a folder whose database has
   *  not been opened yet, which is common right after a download. */
  function folderMessageCount(folder) {
    try {
      return folder.getTotalMessages(false);
    } catch (ex) {
      return null;
    }
  }

  /** Wrap a callback-style folder operation as a promise.
   *
   *  `nsIUrlListener` is the only completion signal these APIs give; returning
   *  before OnStopRunningUrl would report success while Thunderbird was still
   *  rewriting the store. No local deadline on purpose — the request already
   *  carries the caller's, and compacting a large mbox legitimately takes minutes. */
  function runWithUrlListener(what, start) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const listener = {
        QueryInterface: ChromeUtils.generateQI(["nsIUrlListener"]),
        OnStartRunningUrl() {},
        OnStopRunningUrl(url, status) {
          if (settled) {
            return;
          }
          settled = true;
          if (!status || status === Cr.NS_OK) {
            resolve();
          } else {
            reject(new Error(`${what} failed with status ${status}`));
          }
        },
      };
      try {
        start(listener);
      } catch (ex) {
        if (!settled) {
          settled = true;
          reject(ex);
        }
      }
    });
  }

  /** Callers arrive in two shapes: the admin toolset passes `folderIds` and an
   *  `accountKey`, the folders toolset one `folderId` and an `accountId`. Accepting
   *  both is cheaper than deciding which of them is wrong. */
  function folderIdsFrom(params) {
    if (Array.isArray(params.folderIds)) {
      return params.folderIds;
    }
    return params.folderId ? [params.folderId] : [];
  }

  function accountKeyFrom(params) {
    return params.accountKey || params.accountId || null;
  }

  /** Itself plus every descendant. `descendants` is a plain array on 128+; older
   *  builds expose nothing iterable, and one folder is a fair fallback. */
  function withDescendants(folder) {
    const out = [folder];
    try {
      for (const child of folder.descendants) {
        out.push(child);
      }
    } catch (ex) {
      // Nothing to add.
    }
    return out;
  }

  /** Resolve every id up front: a bad id is the caller's mistake and should fail the
   *  whole call, while a folder that refuses to compact is a per-folder failure. */
  function resolveAll(params) {
    const ids = folderIdsFrom(params);
    if (ids.length === 0) {
      throw H.usage("pass folderIds (an array of folder ids from folder_list) or folderId");
    }
    return ids.map((id) => ({ folderId: String(id), folder: folderFor(id) }));
  }

  TBX_MODULES["admin.compactFolders"] = async (params) => {
    const accountKey = accountKeyFrom(params);
    if (accountKey) {
      const root = H.account(String(accountKey)).incomingServer.rootFolder;
      if (typeof root.compactAll !== "function") {
        throw H.unsupported("this Thunderbird build has no nsIMsgFolder.compactAll");
      }
      const before = folderBytes(root);
      await runWithUrlListener(`compacting account ${accountKey}`, (listener) =>
        root.compactAll(listener, null)
      );
      return {
        scope: "account",
        accountKey: String(accountKey),
        compacted: [{ name: root.name, bytesBefore: before, bytesAfter: folderBytes(root) }],
        failures: [],
      };
    }

    const targets = resolveAll(params);
    if (typeof targets[0].folder.compact !== "function") {
      throw H.unsupported("this Thunderbird build has no nsIMsgFolder.compact");
    }
    const compacted = [];
    const failures = [];
    for (const target of targets) {
      const bytesBefore = folderBytes(target.folder);
      try {
        // One at a time: concurrent compaction of the same store is how a mailbox
        // gets corrupted.
        await runWithUrlListener(`compacting ${target.folder.name}`, (listener) =>
          target.folder.compact(listener, null)
        );
        compacted.push({
          folderId: target.folderId,
          name: target.folder.name,
          bytesBefore,
          bytesAfter: folderBytes(target.folder),
        });
      } catch (ex) {
        failures.push({ folderId: target.folderId, error: String(ex.message || ex) });
      }
    }
    return { scope: "folders", compacted, failures, count: compacted.length };
  };

  TBX_MODULES["admin.downloadForOffline"] = async (params) => {
    let targets = resolveAll(params);
    if (params.includeSubFolders) {
      targets = targets.flatMap((target) =>
        withDescendants(target.folder).map((folder, index) => ({
          folderId: index === 0 ? target.folderId : String(folder.URI),
          folder,
        }))
      );
    }
    if (typeof targets[0].folder.downloadAllForOffline !== "function") {
      throw H.unsupported(
        "this Thunderbird build has no nsIMsgFolder.downloadAllForOffline"
      );
    }
    const downloaded = [];
    const failures = [];
    for (const target of targets) {
      try {
        await runWithUrlListener(`downloading ${target.folder.name}`, (listener) =>
          target.folder.downloadAllForOffline(listener, null)
        );
        downloaded.push({
          folderId: target.folderId,
          name: target.folder.name,
          messages: folderMessageCount(target.folder),
          bytesOnDisk: folderBytes(target.folder),
        });
      } catch (ex) {
        failures.push({ folderId: target.folderId, error: String(ex.message || ex) });
      }
    }
    return {
      downloaded,
      failures,
      count: downloaded.length,
      note:
        "Offline copies are what make raw source (mail_get_source) and body search " +
        "work on IMAP — without them Thunderbird only holds the headers.",
    };
  };

  /* --------------------------------------------------------- error console */

  function describeConsoleEntry(entry) {
    let text;
    try {
      text = String(entry.message || "");
    } catch (ex) {
      text = `<unreadable console entry: ${ex.message || ex}>`;
    }
    const record = { message: redact(text) };
    try {
      const error = entry.QueryInterface(Ci.nsIScriptError);
      record.severity = error.flags & Ci.nsIScriptError.warningFlag ? "warning" : "error";
      record.category = error.category || null;
      record.sourceName = error.sourceName || null;
      record.lineNumber = error.lineNumber || null;
      if (error.timeStamp) {
        record.at = new Date(error.timeStamp).toISOString();
      }
    } catch (ex) {
      // A plain nsIConsoleMessage — console.log output and XPCOM warnings — carries
      // no location, so the text is all there is.
      record.severity = "message";
    }
    return record;
  }

  TBX_MODULES["admin.consoleMessages"] = async (params) => {
    const limit = Number.isInteger(params.limit) ? Math.min(Math.max(params.limit, 1), 500) : 100;
    const filter = params.filter ? String(params.filter).toLowerCase() : null;
    let entries;
    try {
      entries = Services.console.getMessageArray() || [];
    } catch (ex) {
      throw H.unsupported(`the error console is not readable: ${ex.message || ex}`);
    }
    const matched = [];
    for (const entry of entries) {
      const record = describeConsoleEntry(entry);
      if (filter && !record.message.toLowerCase().includes(filter)) {
        continue;
      }
      matched.push(record);
    }
    // Newest last: that is the order the console keeps them in, and reading a tail
    // top to bottom is how anyone actually debugs.
    return {
      messages: matched.slice(-limit),
      matched: matched.length,
      buffered: entries.length,
      filter: params.filter || null,
    };
  };

  /* ----------------------------------------------------------- diagnostics */

  async function attempt(fn) {
    try {
      return await fn();
    } catch (ex) {
      return { error: String(ex.message || ex) };
    }
  }

  /** Which on-disk store an account uses. mbox reclaims space only when compacted;
   *  maildir never needs it — worth knowing before offering to compact anything. */
  function storeType(server) {
    let contract = "";
    try {
      contract = server.getCharValue("storeContractID") || "";
    } catch (ex) {
      contract = Services.prefs.getCharPref(
        `mail.server.${server.key}.storeContractID`,
        ""
      );
    }
    if (contract.includes("maildir")) {
      return { contractID: contract, kind: "maildir" };
    }
    if (contract.includes("berkeley")) {
      return { contractID: contract, kind: "mbox" };
    }
    return { contractID: contract || null, kind: "mbox", assumedDefault: !contract };
  }

  function accountDiagnostics() {
    const accounts = [...H.accounts.accounts];
    let defaultAccountKey = null;
    try {
      const preferred = H.accounts.defaultAccount;
      defaultAccountKey = preferred ? preferred.key : null;
    } catch (ex) {
      // A profile with no usable account throws here rather than returning null.
      defaultAccountKey = null;
    }
    return {
      count: accounts.length,
      defaultAccountKey,
      identityCount: accounts.reduce(
        (total, account) => total + (account.identities ? account.identities.length : 0),
        0
      ),
      items: accounts.map((account) => {
        const server = account.incomingServer;
        if (!server) {
          return { key: account.key, error: "account has no incoming server" };
        }
        return {
          key: account.key,
          serverKey: H.safeGet(server, "key"),
          name: H.safeGet(server, "prettyName"),
          type: H.safeGet(server, "type"),
          // server.hostName reads back null on 153; the pref is the reliable copy.
          hostname:
            Services.prefs.getStringPref(`mail.server.${server.key}.hostname`, "") || null,
          messageStore: storeType(server),
          offlineSupportLevel: H.safeGet(server, "offlineSupportLevel"),
        };
      }),
    };
  }

  function calendarDiagnostics() {
    const cal = mod("cal");
    if (!cal) {
      return { available: false };
    }
    const calendars = cal.manager.getCalendars();
    return {
      available: true,
      count: calendars.length,
      types: calendars.map((calendar) => calendar.type),
      defaultTimezone:
        cal.dtz && cal.dtz.defaultTimezone ? cal.dtz.defaultTimezone.tzid : null,
    };
  }

  TBX_MODULES["admin.diagnostics"] = async () => ({
    app: await attempt(() => appInfo()),
    privileged: {
      modulesLoaded: [...TBX_MODULE_NAMES].sort(),
      methods: Object.keys(TBX_MODULES).sort(),
      // Which of core.js's MODULE_URLS this build actually has. An entry under
      // `unresolved` is why one capability reports `unsupported` while the rest work.
      resolved: Object.keys(MODULE_URLS).filter((key) => Boolean(mod(key))),
      unresolved: Object.keys(MODULE_URLS).filter((key) => !mod(key)),
    },
    bridgeAddon: await attempt(async () => {
      const AddonManager = needMod("AddonManager");
      const addon = await AddonManager.getAddonByID(OUR_ADDON_ID);
      return addon ? describeAddon(addon) : null;
    }),
    accounts: await attempt(() => accountDiagnostics()),
    calendars: await attempt(() => calendarDiagnostics()),
    index: await attempt(() => ({
      globalIndexerEnabled: Services.prefs.getBoolPref(
        "mailnews.database.global.indexer.enabled",
        false
      ),
      searcherAvailable: Boolean(mod("GlodaMsgSearcher")),
    })),
  });
}

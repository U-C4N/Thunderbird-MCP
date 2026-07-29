/* Privileged capability probe for Thunderbird MCP.
 *
 * Loaded via Services.scriptloader.loadSubScript into the ext-*.js sandbox,
 * which runs with the system principal and pre-injects: Services, Cc/Ci/Cu/Cr,
 * ExtensionAPI, XPCOMUtils, IOUtils, PathUtils, ChromeUtils, console.
 * (Verified against Thunderbird 153 ExtensionCommon.sys.mjs::_createExtGlobal.)
 */

"use strict";

/** Run fn, returning {ok:true, value} or {ok:false, error} — never throws. */
function attempt(fn) {
  try {
    const value = fn();
    return { ok: true, value: value === undefined ? true : value };
  } catch (ex) {
    return { ok: false, error: String(ex && ex.message ? ex.message : ex) };
  }
}

function importOrNull(url) {
  try {
    return ChromeUtils.importESModule(url);
  } catch (ex) {
    return null;
  }
}

this.tbx = class extends ExtensionAPI {
  getAPI() {
    return {
      tbx: {
        async probe() {
          const out = { checks: {}, modules: {}, notes: [] };

          // --- application identity -------------------------------------
          out.app = attempt(() => ({
            name: Services.appinfo.name,
            version: Services.appinfo.version,
            buildID: Services.appinfo.appBuildID,
            platformVersion: Services.appinfo.platformVersion,
            os: Services.appinfo.OS,
            profileDir: Services.dirsvc.get("ProfD", Ci.nsIFile).path,
            installDir: Services.dirsvc.get("GreD", Ci.nsIFile).path,
          }));

          // --- module resolution ----------------------------------------
          const MODULE_URLS = {
            MailServices: "resource:///modules/MailServices.sys.mjs",
            cal: "resource:///modules/calendar/calUtils.sys.mjs",
            MailUtils: "resource:///modules/MailUtils.sys.mjs",
            VirtualFolderHelper: "resource:///modules/VirtualFolderWrapper.sys.mjs",
            FolderUtils: "resource:///modules/FolderUtils.sys.mjs",
            RNP: "chrome://openpgp/content/modules/RNP.sys.mjs",
            OpenPGPMasterpass: "chrome://openpgp/content/modules/masterpass.sys.mjs",
            Gloda: "resource:///modules/gloda/GlodaPublic.sys.mjs",
            GlodaMsgSearcher: "resource:///modules/gloda/GlodaMsgSearcher.sys.mjs",
            MsgUtils: "resource:///modules/MsgUtils.sys.mjs",
            jsmime: "resource:///modules/jsmime.sys.mjs",
            AddonManager: "resource://gre/modules/AddonManager.sys.mjs",
            NetUtil: "resource://gre/modules/NetUtil.sys.mjs",
          };
          const loaded = {};
          for (const [key, url] of Object.entries(MODULE_URLS)) {
            const mod = importOrNull(url);
            out.modules[key] = mod
              ? { ok: true, url, exports: Object.keys(mod).slice(0, 12) }
              : { ok: false, url };
            if (mod) {
              loaded[key] = mod;
            }
          }

          // --- preferences (read + write + clear) ------------------------
          out.checks.prefsRead = attempt(() => ({
            paneConfig: Services.prefs.getIntPref("mail.pane_config.dynamic", -1),
            experimentsEnabled: Services.prefs.getBoolPref(
              "extensions.experiments.enabled",
              false
            ),
            signaturesRequired: Services.prefs.getBoolPref(
              "xpinstall.signatures.required",
              true
            ),
            totalPrefCount: Services.prefs.getChildList("").length,
          }));
          out.checks.prefsWrite = attempt(() => {
            const KEY = "extensions.thunderbird-mcp.probe.scratch";
            Services.prefs.setCharPref(KEY, "hello");
            const readBack = Services.prefs.getCharPref(KEY, null);
            Services.prefs.clearUserPref(KEY);
            return { readBack, cleared: !Services.prefs.prefHasUserValue(KEY) };
          });

          // --- accounts / servers / identities / SMTP --------------------
          const MailServices = loaded.MailServices && loaded.MailServices.MailServices;
          out.checks.accounts = attempt(() => {
            const accounts = [...MailServices.accounts.accounts];
            return {
              count: accounts.length,
              serverTypes: accounts.map(a => a.incomingServer && a.incomingServer.type),
              identityCount: accounts.reduce(
                (n, a) => n + (a.identities ? a.identities.length : 0),
                0
              ),
              defaultAccountKey:
                MailServices.accounts.defaultAccount &&
                MailServices.accounts.defaultAccount.key,
            };
          });
          out.checks.outgoingServers = attempt(() => {
            // TB 128 renamed MailServices.smtp -> MailServices.outgoingServer
            const svc = MailServices.outgoingServer || MailServices.smtp;
            const servers = [...(svc.servers || [])];
            return {
              api: MailServices.outgoingServer ? "outgoingServer" : "smtp",
              count: servers.length,
              defaultServerKey: svc.defaultServer && svc.defaultServer.key,
            };
          });

          // --- message filters -------------------------------------------
          out.checks.filters = attempt(() => {
            const per = [];
            for (const account of MailServices.accounts.accounts) {
              const server = account.incomingServer;
              if (!server) {
                continue;
              }
              try {
                const list = server.getFilterList(null);
                per.push({ serverType: server.type, filterCount: list.filterCount });
              } catch (ex) {
                per.push({ serverType: server.type, error: String(ex.message || ex) });
              }
            }
            return per;
          });
          out.checks.filterService = attempt(
            () =>
              !!Cc["@mozilla.org/messenger/services/filters;1"].getService(
                Ci.nsIMsgFilterService
              )
          );

          // --- tags -------------------------------------------------------
          out.checks.tags = attempt(() => {
            const tags = MailServices.tags.getAllTags();
            return { count: tags.length, keys: tags.map(t => t.key) };
          });

          // --- address books ----------------------------------------------
          out.checks.addressBooks = attempt(() => {
            const dirs = [...MailServices.ab.directories];
            return {
              count: dirs.length,
              kinds: dirs.map(d => ({ type: d.dirType, uri: d.URI, readOnly: d.readOnly })),
            };
          });

          // --- calendar ----------------------------------------------------
          out.checks.calendar = attempt(() => {
            const cal = loaded.cal && loaded.cal.cal;
            if (!cal) {
              throw new Error("calUtils not loaded");
            }
            const calendars = cal.manager.getCalendars();
            return {
              count: calendars.length,
              types: calendars.map(c => c.type),
              canCreateEvent: typeof cal.createEvent === "function",
              timezone: cal.dtz && cal.dtz.defaultTimezone && cal.dtz.defaultTimezone.tzid,
            };
          });

          // --- junk / bayes -------------------------------------------------
          out.checks.junk = attempt(() => {
            const plugin = MailServices.junk;
            return {
              hasPlugin: !!plugin,
              userHasClassified: plugin.userHasClassified,
            };
          });

          // --- virtual folders (saved searches) -----------------------------
          out.checks.virtualFolders = attempt(() => {
            const helper =
              loaded.VirtualFolderHelper && loaded.VirtualFolderHelper.VirtualFolderHelper;
            return { hasHelper: !!helper };
          });

          // --- OpenPGP -------------------------------------------------------
          out.checks.openpgp = attempt(() => {
            const RNP = loaded.RNP && loaded.RNP.RNP;
            if (!RNP) {
              throw new Error("RNP module not loaded");
            }
            return {
              hasRNP: true,
              keyCount: typeof RNP.getKeys === "function" ? RNP.getKeys().length : null,
            };
          });

          // --- gloda (global full-text index) ---------------------------------
          out.checks.gloda = attempt(() => {
            const enabled = Services.prefs.getBoolPref(
              "mailnews.database.global.indexer.enabled",
              false
            );
            const GlodaMsgSearcher =
              loaded.GlodaMsgSearcher && loaded.GlodaMsgSearcher.GlodaMsgSearcher;
            return { indexerEnabled: enabled, hasSearcher: !!GlodaMsgSearcher };
          });

          // --- raw sockets available from privileged code? ---------------------
          out.checks.socketTransport = attempt(() => {
            const sts = Cc["@mozilla.org/network/socket-transport-service;1"].getService(
              Ci.nsISocketTransportService
            );
            return { hasService: !!sts };
          });
          out.checks.serverSocket = attempt(() => {
            const ss = Cc["@mozilla.org/network/server-socket;1"].createInstance(
              Ci.nsIServerSocket
            );
            return { canInstantiate: !!ss };
          });

          // --- compose backend (send without a compose window) ------------------
          out.checks.composeBackend = attempt(() => ({
            hasMsgComposeService: !!Cc[
              "@mozilla.org/messengercompose;1"
            ].getService(Ci.nsIMsgComposeService),
            hasMsgSend: !!Cc["@mozilla.org/messengercompose/send;1"],
          }));

          // --- add-on manager --------------------------------------------------
          out.checks.addonManager = attempt(
            () => !!(loaded.AddonManager && loaded.AddonManager.AddonManager)
          );

          return out;
        },

        async writeReport(filename, text) {
          const dir = Services.dirsvc.get("ProfD", Ci.nsIFile).path;
          const path = PathUtils.join(dir, filename);
          await IOUtils.writeUTF8(path, text);
          return path;
        },
      },
    };
  }
};

/* The privileged half.
 *
 * Loaded by Services.scriptloader.loadSubScript into the ext-*.js sandbox, which
 * runs with the system principal and pre-injects Services, Cc/Ci/Cu/Cr,
 * ExtensionAPI, XPCOMUtils, IOUtils, PathUtils and ChromeUtils. (Verified against
 * Thunderbird 153's ExtensionCommon.sys.mjs::_createExtGlobal.) Because it is a
 * sub-script and not an ES module, top-level `import` is unavailable — use
 * ChromeUtils.importESModule.
 *
 * This file owns three things and nothing else:
 *   1. lazy, failure-tolerant module resolution (`mod`)
 *   2. shared helpers every privileged module needs (`H`)
 *   3. the dispatch table, assembled from experiment/modules/*.js
 *
 * Each capability lives in experiment/modules/<name>.js and appends to
 * `TBX_MODULES`. Those files are plain sub-scripts too, loaded from here.
 *
 * Do NOT declare `"events": ["startup"]` for this API in the manifest unless a
 * matching onStartup() exists — ExtensionAPI has no such base method, and the
 * missing hook throws during bootstrap, which leaves the whole add-on inert.
 */

"use strict";

/* ------------------------------------------------------------------- timers */

/* The sandbox has no DOM globals, so the bare `setTimeout` every deadline here
 * used to call was a ReferenceError: it rejected the deadline promise before the
 * work it guarded had started, which took out gloda search entirely. Chrome code
 * gets its timers from the platform's Timer module instead. */
const { setTimeout, clearTimeout } = ChromeUtils.importESModule(
  "resource://gre/modules/Timer.sys.mjs"
);

/* ------------------------------------------------------------------ modules */

/** Module URLs as they exist on Thunderbird 128–153. Resolution is lazy so a
 *  rename in a future release degrades one capability instead of the add-on. */
const MODULE_URLS = {
  ExtensionPermissions: "resource://gre/modules/ExtensionPermissions.sys.mjs",
  MailServices: "resource:///modules/MailServices.sys.mjs",
  MailUtils: "resource:///modules/MailUtils.sys.mjs",
  FolderUtils: "resource:///modules/FolderUtils.sys.mjs",
  VirtualFolderWrapper: "resource:///modules/VirtualFolderWrapper.sys.mjs",
  MimeMessage: "resource:///modules/MimeMessage.sys.mjs",
  MessageSend: "resource:///modules/MessageSend.sys.mjs",
  MailStringUtils: "resource:///modules/MailStringUtils.sys.mjs",
  cal: "resource:///modules/calendar/calUtils.sys.mjs",
  CalEvent: "resource:///modules/CalEvent.sys.mjs",
  CalTodo: "resource:///modules/CalTodo.sys.mjs",
  Gloda: "resource:///modules/gloda/GlodaPublic.sys.mjs",
  GlodaMsgSearcher: "resource:///modules/gloda/GlodaMsgSearcher.sys.mjs",
  jsmime: "resource:///modules/jsmime.sys.mjs",
  RNP: "chrome://openpgp/content/modules/RNP.sys.mjs",
  EnigmailKeyRing: "chrome://openpgp/content/modules/keyRing.sys.mjs",
  AddonManager: "resource://gre/modules/AddonManager.sys.mjs",
  NetUtil: "resource://gre/modules/NetUtil.sys.mjs",
  FileUtils: "resource://gre/modules/FileUtils.sys.mjs",
  ExtensionUtils: "resource://gre/modules/ExtensionUtils.sys.mjs",
};

const _moduleCache = new Map();

/** Import a known module by key, or null if this build does not have it. */
function mod(key) {
  if (_moduleCache.has(key)) {
    return _moduleCache.get(key);
  }
  const url = MODULE_URLS[key];
  let value = null;
  if (url) {
    try {
      const exports = ChromeUtils.importESModule(url);
      // Most of these export a single same-named symbol.
      value = exports[key] !== undefined ? exports[key] : exports;
    } catch (ex) {
      console.warn(`[tbmcp] module ${key} unavailable at ${url}: ${ex.message || ex}`);
      value = null;
    }
  }
  _moduleCache.set(key, value);
  return value;
}

/** Import a module or throw an `unsupported` error naming what is missing. */
function needMod(key) {
  const value = mod(key);
  if (!value) {
    throw H.unsupported(
      `this Thunderbird build does not expose ${key}, so that capability is unavailable`
    );
  }
  return value;
}

/* ------------------------------------------------------------------ helpers */

const H = {
  usage(message) {
    return Object.assign(new Error(message), { tbxKind: "usage" });
  },
  unsupported(message) {
    return Object.assign(new Error(message), { tbxKind: "unsupported" });
  },
  blocked(message, needs) {
    return Object.assign(new Error(message), { tbxKind: "blocked", needs });
  },

  need(params, name) {
    const value = params ? params[name] : undefined;
    if (value === undefined || value === null || value === "") {
      throw H.usage(`${name} is required`);
    }
    return value;
  },

  get accounts() {
    return needMod("MailServices").accounts;
  },
  get ab() {
    return needMod("MailServices").ab;
  },
  get tags() {
    return needMod("MailServices").tags;
  },
  get junk() {
    return needMod("MailServices").junk;
  },
  /** TB 128 renamed MailServices.smtp to MailServices.outgoingServer. */
  get outgoing() {
    const services = needMod("MailServices");
    return services.outgoingServer || services.smtp;
  },

  /** An account by key, with a message that lists the valid keys. */
  account(key) {
    for (const account of this.accounts.accounts) {
      if (account.key === key) {
        return account;
      }
    }
    const known = [...this.accounts.accounts].map((a) => a.key).join(", ");
    throw H.usage(`no account with key ${key} (known: ${known || "none"})`);
  },

  identity(key) {
    for (const identity of this.accounts.allIdentities) {
      if (identity.key === key) {
        return identity;
      }
    }
    throw H.usage(`no identity with key ${key}`);
  },

  /** Read a scalar XPCOM property without letting one bad getter kill a dump. */
  safeGet(object, name) {
    try {
      const value = object[name];
      if (value === null || value === undefined) {
        return value === undefined ? null : null;
      }
      const kind = typeof value;
      if (kind === "string" || kind === "number" || kind === "boolean") {
        return value;
      }
      return undefined; // not a scalar: callers skip it
    } catch (ex) {
      return { __error: String(ex.message || ex) };
    }
  },

  /** Snapshot the scalar properties named in `names`. */
  scalars(object, names) {
    const out = {};
    for (const name of names) {
      const value = this.safeGet(object, name);
      if (value !== undefined) {
        out[name] = value;
      }
    }
    return out;
  },

  /** Preference read that reports the type alongside the value. */
  readPref(name) {
    const prefs = Services.prefs;
    switch (prefs.getPrefType(name)) {
      case prefs.PREF_BOOL:
        return { name, type: "bool", value: prefs.getBoolPref(name), set: prefs.prefHasUserValue(name) };
      case prefs.PREF_INT:
        return { name, type: "int", value: prefs.getIntPref(name), set: prefs.prefHasUserValue(name) };
      case prefs.PREF_STRING:
        return {
          name,
          type: "string",
          // Some prefs hold localised or unicode values; this handles both.
          value: prefs.getStringPref(name, prefs.getCharPref(name, "")),
          set: prefs.prefHasUserValue(name),
        };
      default:
        return { name, type: "none", value: null, set: false };
    }
  },

  writePref(name, type, value) {
    const prefs = Services.prefs;
    switch (type) {
      case "bool":
        prefs.setBoolPref(name, Boolean(value));
        break;
      case "int":
        if (!Number.isInteger(value)) {
          throw H.usage(`${name} is an integer preference; got ${JSON.stringify(value)}`);
        }
        prefs.setIntPref(name, value);
        break;
      case "string":
        prefs.setStringPref(name, String(value));
        break;
      default:
        throw H.usage(`unknown preference type ${type} (use bool, int or string)`);
    }
  },

  /** Persist prefs.js now, so a crash cannot lose a setting we reported as applied. */
  flushPrefs() {
    try {
      Services.prefs.savePrefFile(null);
    } catch (ex) {
      console.warn("[tbmcp] savePrefFile failed:", ex.message || ex);
    }
  },

  /** Who we are, recorded by getAPI() because `context` reaches nowhere else.
   *
   *  `admin.consoleMessages` needs it to tell our own console output apart from
   *  every other add-on's in the shared ConsoleAPI store. Undefined until the API
   *  is built, so anything reading it must cope with that. */
  extension: null,

  /** Wrap a callback-style Thunderbird API as a promise with a deadline. */
  withTimeout(promise, ms, what) {
    let timer = null;
    const deadline = new Promise((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error(`${what} timed out after ${ms}ms`)), ms);
    });
    // Whichever way the race lands, the timer has to go: an armed one holds the
    // deadline's rejection alive and keeps Thunderbird awake for nothing.
    return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
  },
};

/* ---------------------------------------------------------- errors on the wire */

/** Marks a message as carrying our envelope; background/registry.js unpacks it. */
const TBX_ERROR_TAG = "tbxerr:";

/**
 * Re-mint a failure as something that survives the hop to the background page.
 *
 * `ExtensionCommon.normalizeError` keeps a message only for a plain object, an
 * `ExtensionError`, or an error whose principal the extension subsumes. Ours is
 * none of those — a plain `Error` minted with the system principal — so the
 * background page was handed "An unexpected error occurred" and the whole
 * taxonomy was lost. Serialise it into the one field that does get through.
 */
function wireError(ex) {
  const payload = {
    kind: (ex && ex.tbxKind) || "thunderbird",
    message: String((ex && ex.message) || ex),
    // A bare "Error" name says nothing; a subclass or an explicit code does.
    code: (ex && ex.code) || (ex && ex.name && ex.name !== "Error" ? ex.name : null),
  };
  if (ex && ex.needs !== undefined && ex.needs !== null) {
    payload.needs = [].concat(ex.needs);
  }
  const message = TBX_ERROR_TAG + JSON.stringify(payload);
  const utils = mod("ExtensionUtils");
  if (utils && utils.ExtensionError) {
    return new utils.ExtensionError(message);
  }
  // The other shape normalizeError trusts. Worth keeping: without ExtensionError
  // every privileged failure would go back to being unreadable.
  return { message };
}

/**
 * Put every method of the API surface behind `wireError`.
 *
 * Applied once to the whole namespace rather than method by method, so a method
 * added later cannot forget: the boundary is a property of the surface, not of
 * any one call. `invoke` therefore does no wrapping of its own.
 */
function wired(api) {
  const out = {};
  for (const [name, fn] of Object.entries(api)) {
    out[name] = async (...args) => {
      try {
        return await fn(...args);
      } catch (ex) {
        throw wireError(ex);
      }
    };
  }
  return out;
}

/* --------------------------------------------------------------- dispatch table */

/** Privileged handlers, keyed by the method name minus its "x." prefix.
 *
 *  Capability modules live in `experiment/modules/*.js` and each appends its
 *  entries here. They are *concatenated into this file* by `tools/build_xpi.py`
 *  at the marker below, rather than loaded at runtime: a sub-script loaded into a
 *  fresh scope object would not see `H`/`mod`/`TBX_MODULES`, since this file is
 *  itself a sub-script and its consts are not on the sandbox global. Splicing at
 *  build time keeps one-module-per-file sources with no scope-chain guesswork. */
const TBX_MODULES = {};

const TBX_MODULE_NAMES = [];

/* ===TBX_MODULES=== (build_xpi.py splices experiment/modules/*.js here) */

/** Repeating timer that stops the background page being suspended.
 *
 *  Held here, in privileged code, precisely so it outlives the page it protects. */
let keepAliveTimer = null;
const KEEP_ALIVE_MS = 15000;

this.tbx = class extends ExtensionAPI {
  onShutdown() {
    if (keepAliveTimer) {
      keepAliveTimer.cancel();
      keepAliveTimer = null;
    }
  }

  getAPI(context) {
    // The only place the privileged half ever sees its own identity; keep it
    // where the modules can reach it (see H.extension).
    try {
      H.extension = {
        id: context.extension.id,
        baseURL: String(context.extension.baseURL || ""),
      };
    } catch (ex) {
      H.extension = null;
    }
    return {
      tbx: wired({
        /* -------------------------------------------------- bridge plumbing */

        async readBridgeFile() {
          const path = PathUtils.join(
            Services.dirsvc.get("ProfD", Ci.nsIFile).path,
            "tbmcp-bridge.json"
          );
          if (!(await IOUtils.exists(path))) {
            return null;
          }
          try {
            return await IOUtils.readJSON(path);
          } catch (ex) {
            // A half-written file is normal: the daemon writes via a temp file and
            // renames, but a reader can still lose the race on some filesystems.
            return null;
          }
        },

        /**
         * Record what the add-on managed to load, where `tbmcp doctor` can read it.
         *
         * A background page cannot write files, and when the bridge itself is what is
         * broken there is no channel left to report through. This file is that
         * channel: its absence on an installed, active add-on means the privileged
         * half never loaded, and its contents explain anything subtler.
         */
        async writeStatus(report) {
          const path = PathUtils.join(
            Services.dirsvc.get("ProfD", Ci.nsIFile).path,
            "tbmcp-addon-status.json"
          );
          await IOUtils.writeJSON(path, report);
          return path;
        },

        /**
         * Grant one of our own `optional_permissions` without a user gesture.
         *
         * `messages.send` is an OptionalOnlyPermission: Thunderbird refuses it in the
         * manifest's `permissions` array and only hands it out through
         * `browser.permissions.request()`, which needs a click. A bridge with no UI
         * has no click to offer, so without this `messages.sendMessage` stays hidden
         * and sending has to open a compose window instead.
         *
         * This grants no authority the add-on was not installed for — the manifest
         * already asks for `compose.send`, and every send still passes the Python
         * side's confirmation gate. It only removes the window.
         */
        async grantOptionalPermission(name) {
          const permissions = needMod("ExtensionPermissions");
          const extension = context.extension;
          const already = extension.hasPermission(name);
          if (already) {
            return { name, granted: true, alreadyHad: true };
          }
          const optional = extension.manifest.optional_permissions || [];
          if (!optional.includes(name)) {
            throw H.usage(
              `${name} is not listed in optional_permissions, so it will not be granted`
            );
          }
          // Passing the extension makes it emit "add-permissions", which updates the
          // live policy — no restart needed.
          await permissions.add(extension.id, { permissions: [name], origins: [] }, extension);
          return { name, granted: extension.hasPermission(name), alreadyHad: false };
        },

        /**
         * Stop Thunderbird suspending our background page.
         *
         * Thunderbird 153 ships `extensions.eventPages.enabled = true`, which makes
         * MV2's `"background": {"persistent": true}` a no-op: the page becomes an
         * event page and is suspended after `extensions.background.idle.timeout`
         * (30s) of quiet. Suspension destroys its timers and its WebSocket, so the
         * bridge silently went away and only came back when some unrelated mail event
         * happened to wake the page — measured at 40-60s.
         *
         * `ext-backgroundPage.js` resets that idle timer whenever the extension emits
         * `background-script-reset-idle`; that is the same mechanism an open
         * native-messaging port uses. Emitting it on a timer from privileged code —
         * which is not itself subject to suspension — keeps the page alive for as long
         * as the add-on is installed.
         */
        async keepAlive(enable = true) {
          const extension = context.extension;
          if (!enable) {
            if (keepAliveTimer) {
              keepAliveTimer.cancel();
              keepAliveTimer = null;
            }
            return { enabled: false };
          }
          if (extension.persistentBackground) {
            // Nothing to do: with a genuinely persistent page there is no idle timer.
            return {
              enabled: false,
              persistentBackground: true,
              reason: "the background page is persistent, so it is never suspended",
            };
          }
          if (keepAliveTimer) {
            return { enabled: true, alreadyRunning: true, intervalMs: KEEP_ALIVE_MS };
          }
          keepAliveTimer = Cc["@mozilla.org/timer;1"].createInstance(Ci.nsITimer);
          keepAliveTimer.initWithCallback(
            {
              notify() {
                try {
                  extension.emit("background-script-reset-idle", { reason: "tbmcp-bridge" });
                } catch (ex) {
                  // The extension is shutting down; the shutdown hook will cancel us.
                }
              },
            },
            KEEP_ALIVE_MS,
            Ci.nsITimer.TYPE_REPEATING_SLACK
          );
          return {
            enabled: true,
            intervalMs: KEEP_ALIVE_MS,
            persistentBackground: false,
            idleTimeoutMs: Services.prefs.getIntPref(
              "extensions.background.idle.timeout",
              30000
            ),
          };
        },

        async appInfo() {
          return {
            // Whether Thunderbird honoured our persistent background page. On 153 it
            // does not, which is why keepAlive() exists.
            persistentBackground: Boolean(context.extension.persistentBackground),
            eventPagesEnabled: Services.prefs.getBoolPref(
              "extensions.eventPages.enabled",
              true
            ),
            name: Services.appinfo.name,
            version: Services.appinfo.version,
            buildID: Services.appinfo.appBuildID,
            platformVersion: Services.appinfo.platformVersion,
            os: Services.appinfo.OS,
            profileDir: Services.dirsvc.get("ProfD", Ci.nsIFile).path,
            installDir: Services.dirsvc.get("GreD", Ci.nsIFile).path,
            locale: Services.locale.appLocaleAsBCP47,
          };
        },

        async availableModules() {
          return {
            loaded: TBX_MODULE_NAMES,
            methods: Object.keys(TBX_MODULES).sort(),
            resolvable: Object.keys(MODULE_URLS).filter((key) => Boolean(mod(key))),
          };
        },

        async globalIndexEnabled() {
          return Services.prefs.getBoolPref(
            "mailnews.database.global.indexer.enabled",
            false
          );
        },

        async writeFile({ directory, filename, base64, overwrite }) {
          if (!directory || !filename) {
            throw H.usage("directory and filename are required");
          }
          // Reject traversal outright: the model chose this name.
          if (/[\\/]|^\.\.?$/.test(filename)) {
            throw H.usage("filename must not contain a path separator");
          }
          if (!(await IOUtils.exists(directory))) {
            throw H.usage(`directory does not exist: ${directory}`);
          }
          const path = PathUtils.join(directory, filename);
          if (!overwrite && (await IOUtils.exists(path))) {
            throw H.blocked(
              `${path} already exists`,
              "overwrite=true, or a different filename"
            );
          }
          const bytes = new Uint8Array(
            Array.prototype.map.call(atob(base64), (c) => c.charCodeAt(0))
          );
          await IOUtils.write(path, bytes, { tmpPath: `${path}.tmp` });
          return { path, name: filename, bytes: bytes.length };
        },

        /* ------------------------------------------------- generic dispatch */

        /**
         * Every capability method goes through here so the schema stays small and
         * new privileged methods need no schema change: the background page calls
         * `browser.tbx.invoke("prefs.get", params)`.
         */
        async invoke(method, params) {
          const handler = TBX_MODULES[method];
          if (!handler) {
            const known = Object.keys(TBX_MODULES).sort().join(", ");
            throw H.usage(`unknown privileged method ${method} (known: ${known})`);
          }
          return handler(params || {});
        },
      }),
    };
  }
};

/* Block-scoped helpers, published for the add-on's tests.
 *
 * `TBX_TEST_HOOKS` does not exist in Thunderbird, so this statement is a no-op
 * there — which is the point: no test-only branch ships inside a handler. */
if (typeof TBX_TEST_HOOKS !== "undefined") {
  TBX_TEST_HOOKS.core = { H, mod, needMod, wireError, handlers: TBX_MODULES };
}

/* Accounts and incoming servers — the half of "settings included" that lives on
 * nsIMsgIncomingServer.
 *
 * Three things to know before editing:
 *   - `server.hostName` reads back null on 153 (measured — docs/VERIFIED-FINDINGS.md),
 *     so `mail.server.<key>.hostname` is the source of truth here and the property is
 *     only a cross-check.
 *   - writes go through the XPCOM property where one exists, because the server object
 *     caches its own state; the pref is the fallback, and every write reports which
 *     path it took so a surprising result is debuggable from the transcript.
 *   - passwords are never read, written or reported. Thunderbird's password manager is
 *     the only credential store, and nothing here goes near it.
 *
 * Singular setters (`setServerSetting`, `setJunkSetting`, `setSyncSetting`) take one
 * `{name, value}` pair, which keeps the allowlist and the "what changed" report
 * honest. `setCopiesAndFolders` is plural because moving Drafts without also moving
 * Sent is rarely what anyone means.
 *
 * Signatures and encryption belong to identities.js; the identity's *folder* targets
 * live here, next to the account they hang off.
 */

TBX_MODULE_NAMES.push("accounts");

{
  /** nsMsgSocketType. 1 left the UI years ago but old profiles still carry it, so it
   *  has to round-trip rather than be rejected. */
  const SOCKET_TYPES = {
    0: "plain",
    1: "tryStartTLS",
    2: "alwaysStartTLS",
    3: "SSL",
  };

  /** nsMsgAuthMethod. 0 is "never configured" — it shows up on half-migrated
   *  accounts, so it is readable but deliberately not settable. */
  const AUTH_METHODS = {
    1: "none",
    2: "old",
    3: "passwordCleartext",
    4: "passwordEncrypted",
    5: "GSSAPI",
    6: "NTLM",
    7: "external",
    8: "secure",
    9: "anything",
    10: "OAuth2",
  };
  const AUTH_METHOD_LABELS = Object.assign({ 0: "unconfigured" }, AUTH_METHODS);

  /** nsISpamSettings.MOVE_TARGET_MODE_*: junk goes to the junk folder of an account,
   *  or to one specific folder. */
  const MOVE_TARGET_MODES = { 0: "account", 1: "folder" };
  /** nsISpamSettings.MANUAL_MARK_MODE_*. */
  const MANUAL_MARK_MODES = { 0: "moveToJunkFolder", 1: "delete" };
  /** nsIMsgRetentionSettings.nsMsgRetain*. */
  const RETAIN_BY = { 1: "keepAll", 2: "keepByAge", 3: "keepNewest" };
  /** nsIMsgIdentity archive granularity. */
  const ARCHIVE_GRANULARITY = { 0: "singleFolder", 1: "monthly", 2: "yearly" };

  const MSG_STORES = {
    "@mozilla.org/msgstore/berkeleystore;1": "mbox",
    "@mozilla.org/msgstore/maildirstore;1": "maildir",
  };

  const CREDENTIAL_LIKE = /password|passwd|secret|token|apikey|oauth.*token/i;

  /* ------------------------------------------------------------------ plumbing */

  function labelFor(map, value) {
    return Number.isInteger(value) && map[value] !== undefined ? map[value] : null;
  }

  function enumOptions(map) {
    return Object.keys(map)
      .map((key) => `${key} (${map[key]})`)
      .join(", ");
  }

  /** Accept either the integer or the readable name we hand out on reads. */
  function enumValue(map, value, field) {
    if (Number.isInteger(value) && map[value] !== undefined) {
      return value;
    }
    if (typeof value === "string") {
      if (/^\d+$/.test(value) && map[Number(value)] !== undefined) {
        return Number(value);
      }
      const lowered = value.toLowerCase();
      for (const key of Object.keys(map)) {
        if (map[key].toLowerCase() === lowered) {
          return Number(key);
        }
      }
    }
    throw H.usage(`${field} must be one of ${enumOptions(map)}`);
  }

  function coerce(name, spec, value) {
    let coerced;
    switch (spec.type) {
      case "bool":
        if (typeof value === "boolean") {
          coerced = value;
        } else if (value === "true" || value === "false") {
          coerced = value === "true";
        } else {
          throw H.usage(`${name} is a true/false setting; got ${JSON.stringify(value)}`);
        }
        break;
      case "int": {
        const parsed =
          typeof value === "string" && /^-?\d+$/.test(value.trim()) ? Number(value) : value;
        if (!Number.isInteger(parsed)) {
          throw H.usage(`${name} is an integer setting; got ${JSON.stringify(value)}`);
        }
        coerced = parsed;
        break;
      }
      case "enum":
        coerced = enumValue(spec.map, value, name);
        break;
      default:
        if (value === null) {
          coerced = "";
        } else if (typeof value === "string") {
          coerced = value;
        } else {
          throw H.usage(`${name} is a text setting; got ${JSON.stringify(value)}`);
        }
    }
    if (spec.min !== undefined && coerced < spec.min) {
      throw H.usage(`${name} must be at least ${spec.min}`);
    }
    if (spec.max !== undefined && coerced > spec.max) {
      throw H.usage(`${name} must be at most ${spec.max}`);
    }
    return coerced;
  }

  function refuseCredential(name) {
    if (CREDENTIAL_LIKE.test(name)) {
      throw H.blocked(
        `${name} looks like a credential; this bridge neither reads nor writes passwords.`,
        "enter it in Thunderbird's own account settings"
      );
    }
  }

  function serverOf(account) {
    let server = null;
    try {
      server = account.incomingServer;
    } catch (ex) {
      server = null;
    }
    if (!server) {
      throw H.unsupported(
        `account ${account.key} has no incoming server, so it has no server settings`
      );
    }
    return server;
  }

  /** 153 hands back null for `hostName`, so the pref wins and the property is a
   *  cross-check. serverURI is the last resort: it is derived, but it is never null. */
  function hostOf(server) {
    const pref = H.readPref(`mail.server.${server.key}.hostname`);
    if (pref.type === "string" && pref.value) {
      return pref.value;
    }
    for (const prop of ["hostname", "hostName"]) {
      const value = H.safeGet(server, prop);
      if (typeof value === "string" && value) {
        return value;
      }
    }
    try {
      return Services.io.newURI(server.serverURI).host || null;
    } catch (ex) {
      return null;
    }
  }

  function writeHost(server, value) {
    for (const prop of ["hostname", "hostName"]) {
      try {
        server[prop] = value;
      } catch (ex) {
        continue;
      }
      if (hostOf(server) === value) {
        return prop;
      }
    }
    Services.prefs.setStringPref(`mail.server.${server.key}.hostname`, value);
    return "pref";
  }

  function asImap(server) {
    if (server.type !== "imap") {
      return null;
    }
    try {
      return server.QueryInterface(Ci.nsIImapIncomingServer);
    } catch (ex) {
      return null;
    }
  }

  function asPop3(server) {
    if (server.type !== "pop3") {
      return null;
    }
    try {
      return server.QueryInterface(Ci.nsIPop3IncomingServer);
    } catch (ex) {
      return null;
    }
  }

  function defaultAccountKey() {
    try {
      const account = H.accounts.defaultAccount;
      return account ? account.key : null;
    } catch (ex) {
      return null;
    }
  }

  function identityKeysOf(account) {
    try {
      return [...account.identities].map((identity) => identity.key);
    } catch (ex) {
      return [];
    }
  }

  /* ---------------------------------------------------------------------- list */

  TBX_MODULES["accounts.list"] = async () => {
    const defaultKey = defaultAccountKey();
    const accounts = [...H.accounts.accounts].map((account) => {
      const row = { key: account.key, isDefault: account.key === defaultKey };
      let server = null;
      try {
        server = account.incomingServer;
      } catch (ex) {
        server = null;
      }
      if (server) {
        row.serverKey = server.key;
        row.type = H.safeGet(server, "type");
        row.prettyName = H.safeGet(server, "prettyName");
        row.hostname = hostOf(server);
        row.username = H.safeGet(server, "username");
      } else {
        row.type = null;
        row.prettyName = null;
        row.serverUnavailable = true;
      }
      row.identityKeys = identityKeysOf(account);
      row.defaultIdentityKey = (() => {
        try {
          const identity = account.defaultIdentity;
          return identity ? identity.key : null;
        } catch (ex) {
          return null;
        }
      })();
      return row;
    });
    return { accounts, count: accounts.length, defaultAccountKey: defaultKey };
  };

  /** Identities per account, flat. `x.identities.get` returns the full field dump;
   *  this is the cheap index that answers "which identity should I use". */
  TBX_MODULES["accounts.identities"] = async (params) => {
    const wanted = params.accountKey ? [H.account(params.accountKey)] : [...H.accounts.accounts];
    const identities = [];
    for (const account of wanted) {
      let defaultKey = null;
      try {
        defaultKey = account.defaultIdentity ? account.defaultIdentity.key : null;
      } catch (ex) {
        defaultKey = null;
      }
      let own = [];
      try {
        own = [...account.identities];
      } catch (ex) {
        own = [];
      }
      for (const identity of own) {
        identities.push({
          key: identity.key,
          accountKey: account.key,
          isDefault: identity.key === defaultKey,
          email: H.safeGet(identity, "email"),
          fullName: H.safeGet(identity, "fullName"),
          label: H.safeGet(identity, "label"),
          smtpServerKey: H.safeGet(identity, "smtpServerKey"),
          composeHtml: H.safeGet(identity, "composeHtml"),
        });
      }
    }
    return { identities, count: identities.length };
  };

  /* ------------------------------------------------------------ serverSettings */

  const CORE_SCALARS = [
    "type",
    "port",
    "username",
    "prettyName",
    "socketType",
    "authMethod",
    "biffMinutes",
    "doBiff",
    "loginAtStartUp",
    "downloadOnBiff",
    "emptyTrashOnExit",
    "limitOfflineMessageSize",
    "maxMessageSize",
    "offlineSupportLevel",
    "canBeDefaultServer",
    "isSecure",
  ];
  const IMAP_SCALARS = [
    "useIdle",
    "maximumConnectionsNumber",
    "forceSelect",
    "cleanupInboxOnExit",
  ];
  const POP3_SCALARS = [
    "leaveMessagesOnServer",
    "deleteMailLeftOnServer",
    "headersOnly",
    "deleteByAgeFromServer",
    "numDaysToLeaveOnServer",
  ];

  TBX_MODULES["accounts.serverSettings"] = async (params) => {
    const accountKey = H.need(params, "accountKey");
    const account = H.account(accountKey);
    const server = serverOf(account);
    const settings = Object.assign(
      { accountKey, serverKey: server.key, hostname: hostOf(server) },
      H.scalars(server, CORE_SCALARS)
    );
    settings.socketTypeName = labelFor(SOCKET_TYPES, settings.socketType);
    settings.authMethodName = labelFor(AUTH_METHOD_LABELS, settings.authMethod);

    const imap = asImap(server);
    if (imap) {
      settings.imap = H.scalars(imap, IMAP_SCALARS);
    }
    const pop3 = asPop3(server);
    if (pop3) {
      settings.pop3 = H.scalars(pop3, POP3_SCALARS);
    }
    // Said out loud because a caller that does not see a password field will ask why.
    settings.passwordOmitted = true;
    return settings;
  };

  /** Writable incoming-server settings, with where they are stored and when they
   *  bite. Nothing outside this table is settable — it is the list a reviewer reads
   *  to know what this bridge can do to an account. */
  const SERVER_WRITABLE = {
    prettyName: { type: "string", applies: "immediately" },
    hostname: { type: "string", applies: "restart", host: true },
    port: { type: "int", min: 0, max: 65535, applies: "restart" },
    username: {
      type: "string",
      applies: "restart",
      note:
        "the saved password is keyed to host plus username, so Thunderbird will ask " +
        "for the password again on the next connection",
    },
    socketType: { type: "enum", map: SOCKET_TYPES, applies: "restart" },
    authMethod: { type: "enum", map: AUTH_METHODS, applies: "restart" },
    biffMinutes: { type: "int", min: 1, max: 10080, applies: "immediately" },
    doBiff: { type: "bool", applies: "immediately" },
    loginAtStartUp: { type: "bool", applies: "restart" },
    downloadOnBiff: { type: "bool", applies: "immediately" },
    emptyTrashOnExit: { type: "bool", applies: "immediately" },
    limitOfflineMessageSize: { type: "bool", applies: "immediately" },
    maxMessageSize: { type: "int", min: 1, max: 1048576, applies: "immediately" },
    useIdle: { type: "bool", iface: "imap", applies: "reconnect" },
    maximumConnectionsNumber: { type: "int", iface: "imap", min: 1, max: 32, applies: "reconnect" },
    forceSelect: { type: "bool", iface: "imap", applies: "reconnect" },
    cleanupInboxOnExit: { type: "bool", iface: "imap", applies: "immediately" },
    leaveMessagesOnServer: { type: "bool", iface: "pop3", applies: "immediately" },
    deleteMailLeftOnServer: { type: "bool", iface: "pop3", applies: "immediately" },
    headersOnly: { type: "bool", iface: "pop3", applies: "immediately" },
    deleteByAgeFromServer: { type: "bool", iface: "pop3", applies: "immediately" },
    numDaysToLeaveOnServer: { type: "int", iface: "pop3", min: 0, max: 3650, applies: "immediately" },
  };

  function requireValue(params) {
    if (params.value === undefined) {
      throw H.usage("value is required; send false or 0 explicitly rather than omitting it");
    }
    return params.value;
  }

  function writableTarget(server, name, spec, accountKey) {
    if (!spec.iface) {
      return server;
    }
    const target = spec.iface === "imap" ? asImap(server) : asPop3(server);
    if (!target) {
      throw H.usage(
        `${name} only exists on ${spec.iface.toUpperCase()} accounts, and ${accountKey} ` +
          `is ${server.type}`
      );
    }
    return target;
  }

  TBX_MODULES["accounts.setServerSetting"] = async (params) => {
    const accountKey = H.need(params, "accountKey");
    const name = String(H.need(params, "name"));
    refuseCredential(name);
    const spec = SERVER_WRITABLE[name];
    if (!spec) {
      throw H.usage(
        `${name} is not a writable server setting. Writable: ` +
          `${Object.keys(SERVER_WRITABLE).sort().join(", ")}. Junk, offline and folder ` +
          "settings have their own methods; anything else is a preference (x.prefs.set)."
      );
    }
    const raw = requireValue(params);
    const server = serverOf(H.account(accountKey));
    const target = writableTarget(server, name, spec, accountKey);
    const value = coerce(name, spec, raw);
    if (spec.host && !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(value)) {
      throw H.usage(
        "hostname must be a bare host name such as imap.example.com — no scheme, " +
          "port, path or spaces"
      );
    }

    const previous = spec.host ? hostOf(server) : H.safeGet(target, name);
    let via = "property";
    if (spec.host) {
      via = writeHost(server, value);
    } else {
      try {
        target[name] = value;
      } catch (ex) {
        throw H.blocked(
          `Thunderbird refused to set ${name} on ${accountKey}: ${ex.message || ex}`,
          "a value this server type accepts"
        );
      }
    }
    H.flushPrefs();
    const current = spec.host ? hostOf(server) : H.safeGet(target, name);

    const result = {
      accountKey,
      serverKey: server.key,
      name,
      via,
      previous,
      current,
      // A setter that quietly ignores its argument is the failure mode worth naming:
      // it is indistinguishable from success unless we read back.
      applied: current === value,
      restartRequired: spec.applies === "restart",
      reconnectRequired: spec.applies !== "immediately",
    };
    if (spec.type === "enum") {
      result.previousName = labelFor(spec.map, previous);
      result.currentName = labelFor(spec.map, current);
    }
    if (spec.note) {
      result.note = spec.note;
    }
    return result;
  };

  /* -------------------------------------------------------------- junkSettings */

  const SPAM_SCALARS = [
    "level",
    "moveOnSpam",
    "moveTargetMode",
    "actionTargetAccount",
    "actionTargetFolder",
    "purge",
    "purgeInterval",
    "useWhiteList",
    "whiteListAbURI",
    "manualMark",
    "manualMarkMode",
  ];

  /** spamSettings is a live object rebuilt from the server's own values, so writes go
   *  to the value (which is the pref) and then re-initialise the object. Setting the
   *  property alone survives until the next restart and no longer. */
  const JUNK_WRITABLE = {
    level: {
      type: "int",
      value: "spamLevel",
      kind: "int",
      min: 0,
      max: 100,
      note: "0 turns adaptive junk filtering off for this account, 100 turns it on",
    },
    moveOnSpam: { type: "bool", value: "moveOnSpam", kind: "bool" },
    moveTargetMode: { type: "enum", map: MOVE_TARGET_MODES, value: "moveTargetMode", kind: "int" },
    actionTargetAccount: { type: "string", value: "spamActionTargetAccount", kind: "char" },
    actionTargetFolder: {
      type: "string",
      value: "spamActionTargetFolder",
      kind: "char",
      folder: true,
    },
    purge: { type: "bool", value: "purgeSpam", kind: "bool" },
    purgeInterval: { type: "int", value: "purgeSpamInterval", kind: "int", min: 1, max: 3650 },
    useWhiteList: { type: "bool", value: "useWhiteList", kind: "bool" },
    whiteListAbURI: { type: "string", value: "whiteListAbURI", kind: "char", addressBooks: true },
    manualMark: { type: "bool", value: "manualMark", kind: "bool" },
    manualMarkMode: { type: "enum", map: MANUAL_MARK_MODES, value: "manualMarkMode", kind: "int" },
  };

  function spamSettingsOf(server) {
    let settings = null;
    try {
      settings = server.spamSettings;
    } catch (ex) {
      settings = null;
    }
    if (!settings) {
      throw H.unsupported(
        `${server.type} servers do not expose junk settings on this build`
      );
    }
    return settings;
  }

  function readJunk(server) {
    const settings = spamSettingsOf(server);
    const out = H.scalars(settings, SPAM_SCALARS);
    out.moveTargetModeName = labelFor(MOVE_TARGET_MODES, out.moveTargetMode);
    out.manualMarkModeName = labelFor(MANUAL_MARK_MODES, out.manualMarkMode);
    return out;
  }

  function requireFolder(uri) {
    const utils = mod("MailUtils");
    if (!utils || !utils.getExistingFolder) {
      return null; // cannot validate on this build; the caller's URI stands
    }
    const folder = utils.getExistingFolder(uri);
    if (!folder) {
      throw H.usage(
        `no folder with URI ${uri} — pass a folder URI from folder_list or folder_get`
      );
    }
    return folder;
  }

  function checkAddressBooks(value) {
    if (!value) {
      return;
    }
    let known;
    try {
      known = [...H.ab.directories].map((book) => book.URI);
    } catch (ex) {
      return;
    }
    // The pref holds one or more book URIs separated by spaces.
    const bad = value.split(/\s+/).filter((uri) => uri && !known.includes(uri));
    if (bad.length) {
      throw H.usage(
        `unknown address book URI ${bad.join(", ")} (known: ${known.join(", ")})`
      );
    }
  }

  TBX_MODULES["accounts.junkSettings"] = async (params) => {
    const accountKey = H.need(params, "accountKey");
    const server = serverOf(H.account(accountKey));
    return Object.assign(
      { accountKey, serverKey: server.key, type: H.safeGet(server, "type") },
      readJunk(server)
    );
  };

  TBX_MODULES["accounts.setJunkSetting"] = async (params) => {
    const accountKey = H.need(params, "accountKey");
    const name = String(H.need(params, "name"));
    const spec = JUNK_WRITABLE[name];
    if (!spec) {
      throw H.usage(
        `${name} is not a junk setting. Writable: ` +
          `${Object.keys(JUNK_WRITABLE).sort().join(", ")}`
      );
    }
    const value = coerce(name, spec, requireValue(params));
    if (spec.folder) {
      requireFolder(value);
    }
    if (spec.addressBooks) {
      checkAddressBooks(value);
    }
    const server = serverOf(H.account(accountKey));
    const before = readJunk(server);

    switch (spec.kind) {
      case "bool":
        server.setBoolValue(spec.value, value);
        break;
      case "int":
        server.setIntValue(spec.value, value);
        break;
      default:
        server.setCharValue(spec.value, value);
    }
    try {
      // Rebuild the live object from what we just stored, or this session keeps
      // filtering with the old policy.
      server.spamSettings.initialize(server);
    } catch (ex) {
      console.warn(`[tbmcp] spamSettings.initialize failed for ${server.key}:`, ex.message || ex);
    }
    H.flushPrefs();
    const after = readJunk(server);

    const result = {
      accountKey,
      serverKey: server.key,
      name,
      storedAs: `mail.server.${server.key}.${spec.value}`,
      previous: before[name],
      current: after[name],
      applied: after[name] === value,
    };
    if (spec.type === "enum") {
      result.previousName = labelFor(spec.map, before[name]);
      result.currentName = labelFor(spec.map, after[name]);
    }
    if (spec.note) {
      result.note = spec.note;
    }
    if (name === "moveOnSpam" && value === true && !after.actionTargetFolder && after.moveTargetMode === 1) {
      result.warning =
        "moveTargetMode is 1 (a specific folder) but actionTargetFolder is empty, so " +
        "junk has nowhere to go — set actionTargetFolder as well";
    }
    return result;
  };

  /* --------------------------------------------------------- copies and folders */

  /** Identity folder targets. The picker mode only decides which control the account
   *  manager shows; both modes store a folder URI in the matching *_folder pref. */
  const COPY_FIELDS = {
    fccFolder: { folder: true, attr: "fcc_folder" },
    draftFolder: { folder: true, attr: "draft_folder" },
    stationeryFolder: { folder: true, attr: "stationery_folder" },
    archiveFolder: { folder: true, attr: "archive_folder" },
    doFcc: { type: "bool", attr: "fcc" },
    fccReplyFollowsParent: { type: "bool", attr: "fcc_reply_follows_parent" },
    fccFolderPickerMode: { type: "picker", attr: "fcc_folder_picker_mode" },
    draftsFolderPickerMode: { type: "picker", attr: "drafts_folder_picker_mode" },
    tmplFolderPickerMode: { type: "picker", attr: "tmpl_folder_picker_mode" },
    archivesFolderPickerMode: { type: "picker", attr: "archives_folder_picker_mode" },
    archiveEnabled: { type: "bool", attr: "archive_enabled" },
    archiveGranularity: { type: "enum", map: ARCHIVE_GRANULARITY, attr: "archive_granularity" },
    archiveKeepFolderStructure: { type: "bool", attr: "archive_keep_folder_structure" },
  };

  const PICKER_MODES = { 0: "defaultFolderOnAccount", 1: "chosenFolder" };

  function resolveIdentity(params, what) {
    if (params.identityKey) {
      return H.identity(params.identityKey);
    }
    if (params.accountKey) {
      const account = H.account(params.accountKey);
      let identity = null;
      try {
        identity = account.defaultIdentity;
      } catch (ex) {
        identity = null;
      }
      if (!identity) {
        throw H.usage(`account ${params.accountKey} has no identity to ${what}`);
      }
      return identity;
    }
    throw H.usage("identityKey is required (or accountKey, to use that account's default identity)");
  }

  function accountKeyForIdentity(identityKey) {
    for (const account of H.accounts.accounts) {
      if (identityKeysOf(account).includes(identityKey)) {
        return account.key;
      }
    }
    return null;
  }

  /** Folder targets are URI strings on some 153 builds and nsIMsgFolder on others.
   *  Normalise to a URI, and fall back to the pref, which every build agrees on. */
  function readFolderField(identity, name, spec) {
    let value;
    try {
      value = identity[name];
    } catch (ex) {
      value = undefined;
    }
    if (typeof value === "string" && value) {
      return value;
    }
    if (value && typeof value === "object") {
      const uri = H.safeGet(value, "URI");
      if (typeof uri === "string" && uri) {
        return uri;
      }
    }
    const pref = H.readPref(`mail.identity.${identity.key}.${spec.attr}`);
    return pref.type === "string" && pref.value ? pref.value : null;
  }

  function writeFolderField(identity, name, spec, uri) {
    const folder = uri ? requireFolder(uri) : null;
    const attempts = uri ? (folder ? [uri, folder] : [uri]) : ["", null];
    for (const candidate of attempts) {
      try {
        identity[name] = candidate;
      } catch (ex) {
        continue;
      }
      if ((readFolderField(identity, name, spec) || null) === (uri || null)) {
        return typeof candidate === "string" ? "uri" : "folder";
      }
    }
    // Whatever the property wants, the pref is what compose reads next time.
    const prefName = `mail.identity.${identity.key}.${spec.attr}`;
    if (uri) {
      Services.prefs.setStringPref(prefName, uri);
    } else {
      try {
        Services.prefs.clearUserPref(prefName);
      } catch (ex) {
        Services.prefs.setStringPref(prefName, "");
      }
    }
    return "pref";
  }

  function readCopyField(identity, name, spec) {
    if (spec.folder) {
      return readFolderField(identity, name, spec);
    }
    const direct = H.safeGet(identity, name);
    if (direct !== null && typeof direct !== "object") {
      return spec.type === "picker" ? String(direct) : direct;
    }
    try {
      switch (spec.type) {
        case "bool":
          return identity.getBoolAttribute(spec.attr);
        case "enum":
          return identity.getIntAttribute(spec.attr);
        default:
          return String(identity.getCharAttribute(spec.attr) || "0");
      }
    } catch (ex) {
      return null;
    }
  }

  function writeCopyField(identity, name, spec, value) {
    try {
      identity[name] = value;
      if (readCopyField(identity, name, spec) === value) {
        return "property";
      }
    } catch (ex) {
      // fall through: the generic attribute accessors exist for every identity pref,
      // whether or not this build promoted it to an interface attribute
    }
    switch (spec.type) {
      case "bool":
        identity.setBoolAttribute(spec.attr, value);
        break;
      case "enum":
        identity.setIntAttribute(spec.attr, value);
        break;
      default:
        identity.setCharAttribute(spec.attr, String(value));
    }
    return "attribute";
  }

  function readCopies(identity) {
    const out = { identityKey: identity.key, accountKey: accountKeyForIdentity(identity.key) };
    for (const name of Object.keys(COPY_FIELDS)) {
      out[name] = readCopyField(identity, name, COPY_FIELDS[name]);
    }
    out.archiveGranularityName = labelFor(ARCHIVE_GRANULARITY, out.archiveGranularity);
    out.pickerModes = PICKER_MODES;
    return out;
  }

  TBX_MODULES["accounts.copiesAndFolders"] = async (params) => {
    return readCopies(resolveIdentity(params, "read"));
  };

  TBX_MODULES["accounts.setCopiesAndFolders"] = async (params) => {
    const identity = resolveIdentity(params, "change");
    const wanted = Object.keys(params).filter(
      (name) => name !== "identityKey" && name !== "accountKey"
    );
    if (!wanted.length) {
      throw H.usage(
        `nothing to change — pass one or more of ${Object.keys(COPY_FIELDS).sort().join(", ")}`
      );
    }
    for (const name of wanted) {
      if (!COPY_FIELDS[name]) {
        throw H.usage(
          `${name} is not a copies-and-folders field (settable: ` +
            `${Object.keys(COPY_FIELDS).sort().join(", ")})`
        );
      }
    }

    const changes = [];
    for (const name of wanted) {
      const spec = COPY_FIELDS[name];
      const previous = readCopyField(identity, name, spec);
      let via;
      let expected;
      if (spec.folder) {
        const uri = params[name] === null || params[name] === "" ? null : String(params[name]);
        via = writeFolderField(identity, name, spec, uri);
        expected = uri;
      } else if (spec.type === "picker") {
        expected = String(enumValue(PICKER_MODES, params[name], name));
        via = writeCopyField(identity, name, spec, expected);
      } else {
        expected = coerce(name, spec, params[name]);
        via = writeCopyField(identity, name, spec, expected);
      }
      const current = readCopyField(identity, name, spec);
      const change = { name, previous, current, via, applied: current === expected };
      if (spec.type === "enum") {
        change.previousName = labelFor(spec.map, previous);
        change.currentName = labelFor(spec.map, current);
      }
      changes.push(change);
    }
    H.flushPrefs();
    return {
      identityKey: identity.key,
      accountKey: accountKeyForIdentity(identity.key),
      changes,
      settings: readCopies(identity),
    };
  };

  /* -------------------------------------------------------------- syncSettings */

  const DOWNLOAD_SCALARS = [
    "useServerDefaults",
    "downloadUnreadOnly",
    "downloadByDate",
    "ageLimitOfMsgsToDownload",
  ];
  const RETENTION_SCALARS = [
    "retainByPreference",
    "daysToKeepHdrs",
    "numHeadersToKeep",
    "keepUnreadMessagesOnly",
    "daysToKeepBodies",
    "cleanupBodiesByDays",
    "applyToFlaggedMessages",
    "useServerDefaults",
  ];

  /** `offline_download` is the server value behind nsIImapIncomingServer.offlineDownload;
   *  going through the value works for every server type, including the ones that never
   *  promoted it to an interface attribute. */
  function offlineDownloadOf(server) {
    const direct = H.safeGet(server, "offlineDownload");
    if (typeof direct === "boolean") {
      return direct;
    }
    try {
      return server.getBoolValue("offline_download");
    } catch (ex) {
      return null;
    }
  }

  function storeContractIdOf(server) {
    const direct = H.safeGet(server, "storeContractID");
    if (typeof direct === "string" && direct) {
      return direct;
    }
    try {
      const value = server.getCharValue("storeContractID");
      if (value) {
        return value;
      }
    } catch (ex) {
      // fall through to the application default below
    }
    const fallback = H.readPref("mail.serverDefaultStoreContractID");
    return fallback.type === "string" ? fallback.value : null;
  }

  function downloadSettingsOf(server) {
    try {
      return server.downloadSettings;
    } catch (ex) {
      return null;
    }
  }

  function retentionSettingsOf(server) {
    try {
      return server.retentionSettings;
    } catch (ex) {
      return null;
    }
  }

  const SYNC_WRITABLE = {
    offlineDownload: { type: "bool", store: "server", value: "offline_download" },
    downloadByDate: { type: "bool", store: "download" },
    downloadUnreadOnly: { type: "bool", store: "download" },
    ageLimitOfMsgsToDownload: { type: "int", store: "download", min: 0, max: 36500 },
    retainByPreference: { type: "enum", map: RETAIN_BY, store: "retention" },
    daysToKeepHdrs: { type: "int", store: "retention", min: 0, max: 36500 },
    numHeadersToKeep: { type: "int", store: "retention", min: 0, max: 1000000 },
    keepUnreadMessagesOnly: { type: "bool", store: "retention" },
    daysToKeepBodies: { type: "int", store: "retention", min: 0, max: 36500 },
    cleanupBodiesByDays: { type: "bool", store: "retention" },
    applyToFlaggedMessages: { type: "bool", store: "retention" },
  };

  function readSync(server) {
    const download = downloadSettingsOf(server);
    const retention = retentionSettingsOf(server);
    const storeContractID = storeContractIdOf(server);
    const out = {
      serverKey: server.key,
      type: H.safeGet(server, "type"),
      offlineDownload: offlineDownloadOf(server),
      offlineSupportLevel: H.safeGet(server, "offlineSupportLevel"),
      limitOfflineMessageSize: H.safeGet(server, "limitOfflineMessageSize"),
      maxMessageSize: H.safeGet(server, "maxMessageSize"),
      storeContractID,
      messageStore: MSG_STORES[storeContractID] || "unknown",
      // Reported, never written: rewriting it points Thunderbird at a store format
      // the existing mail is not in, and the mail is then simply gone from the UI.
      storeContractIDWritable: false,
      download: download ? H.scalars(download, DOWNLOAD_SCALARS) : null,
      retention: retention ? H.scalars(retention, RETENTION_SCALARS) : null,
    };
    if (out.retention) {
      out.retention.retainByPreferenceName = labelFor(RETAIN_BY, out.retention.retainByPreference);
    }
    return out;
  }

  TBX_MODULES["accounts.syncSettings"] = async (params) => {
    const accountKey = H.need(params, "accountKey");
    const server = serverOf(H.account(accountKey));
    return Object.assign({ accountKey }, readSync(server));
  };

  TBX_MODULES["accounts.setSyncSetting"] = async (params) => {
    const accountKey = H.need(params, "accountKey");
    const name = String(H.need(params, "name"));
    if (name === "storeContractID" || name === "messageStore") {
      throw H.blocked(
        "the message store format cannot be changed here: pointing an account at a " +
          "different store leaves its existing mail unreadable.",
        "convert the account in Thunderbird itself, then re-read syncSettings"
      );
    }
    const spec = SYNC_WRITABLE[name];
    if (!spec) {
      throw H.usage(
        `${name} is not an offline or retention setting. Writable: ` +
          `${Object.keys(SYNC_WRITABLE).sort().join(", ")}`
      );
    }
    const value = coerce(name, spec, requireValue(params));
    const server = serverOf(H.account(accountKey));
    const before = readSync(server);

    if (spec.store === "server") {
      server.setBoolValue(spec.value, value);
    } else {
      const bag = spec.store === "download" ? downloadSettingsOf(server) : retentionSettingsOf(server);
      if (!bag) {
        throw H.unsupported(
          `${server.type} servers do not expose ${spec.store} settings on this build`
        );
      }
      bag[name] = value;
      // The setter is what writes the prefs; mutating the object alone changes nothing
      // that survives a restart.
      if (spec.store === "download") {
        server.downloadSettings = bag;
      } else {
        server.retentionSettings = bag;
      }
    }
    H.flushPrefs();
    const after = readSync(server);
    const where = spec.store === "server" ? null : spec.store;
    const pick = (snapshot) =>
      where ? (snapshot[where] || {})[name] : snapshot[name];

    const result = {
      accountKey,
      serverKey: server.key,
      name,
      scope: spec.store,
      previous: pick(before),
      current: pick(after),
      applied: pick(after) === value,
      restartRequired: false,
      messageStore: after.messageStore,
    };
    if (spec.type === "enum") {
      result.previousName = labelFor(spec.map, pick(before));
      result.currentName = labelFor(spec.map, pick(after));
    }
    if (name === "offlineDownload" && value === true) {
      result.note =
        "existing folders keep their own offline flag; use folder_sync_offline to " +
        "download what is already there";
    }
    return result;
  };
}

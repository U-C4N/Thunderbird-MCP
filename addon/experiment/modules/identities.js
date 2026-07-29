/* Sending identities: addresses, composition defaults, signatures, encryption.
 *
 * An identity is the from-address the recipient actually sees, together with its
 * signature, outgoing server and filing folders. Everything here is stored as
 * `mail.identity.<key>.*` preferences behind nsIMsgIdentity, which is why each
 * write ends with H.flushPrefs() — without it a crash before Thunderbird's next
 * periodic save would silently discard a change we already reported as applied.
 *
 * Passwords are never touched. Outgoing servers are referenced by key only; the
 * credential lives in the login manager and this bridge has no business there.
 */

TBX_MODULE_NAMES.push("identities");

{
  /** Typed nsIMsgIdentity properties we expose, with the coercion each needs.
   *  Keeping this table explicit is the point: it is both the allowlist for writes
   *  and the field list for reads, so the two can never drift apart. */
  const FIELDS = {
    fullName: "string",
    email: "string",
    replyTo: "string",
    organization: "string",
    composeHtml: "bool",
    attachVCard: "bool",
    escapedVCard: "string",
    smtpServerKey: "string",
    doBcc: "bool",
    doBccList: "string",
    catchAll: "bool",
    catchAllHint: "string",
    attachSignature: "bool",
    sigBottom: "bool",
    htmlSigText: "string",
    htmlSigFormat: "bool",
    autoQuote: "bool",
    replyOnTop: "int",
  };

  /** End-to-end encryption fields. `e2etechpref` is 0 = choose automatically,
   *  1 = S/MIME, 2 = OpenPGP; `encryptionPolicy` is 0 = never, 2 = require. */
  const ENCRYPTION_FIELDS = {
    encryptionPolicy: "int",
    signMail: "bool",
    e2etechpref: "int",
    attachPgpKey: "bool",
    autoEncryptDrafts: "bool",
    protectSubject: "bool",
    is_gnupg_key_id: "bool",
    openPgpKeyId: "string",
  };

  const FOLDER_FIELDS = [
    "draftFolder",
    "fccFolder",
    "archiveFolder",
    "stationeryFolder",
    "archiveEnabled",
    "archiveGranularity",
    "archiveKeepFolderStructure",
    "doFcc",
  ];

  /** Read one attribute, preferring the typed property and falling back to the
   *  generic attribute accessors for the pref-only fields (OpenPGP settings are
   *  not all exposed as IDL properties). */
  function readField(identity, name, kind) {
    try {
      const direct = identity[name];
      if (direct !== undefined) {
        return direct;
      }
    } catch (ex) {
      // fall through to the attribute accessors
    }
    try {
      if (kind === "bool") {
        return identity.getBoolAttribute(name);
      }
      if (kind === "int") {
        return identity.getIntAttribute(name);
      }
      return identity.getUnicharAttribute(name);
    } catch (ex) {
      return null;
    }
  }

  function writeField(identity, name, kind, value) {
    const coerced = coerce(name, kind, value);
    try {
      // Assigning the IDL property keeps Thunderbird's own side effects (it
      // notifies observers); the attribute accessors do not always.
      if (identity[name] !== undefined || name in identity) {
        identity[name] = coerced;
        return coerced;
      }
    } catch (ex) {
      // Some properties are read-only on certain identity types; fall through.
    }
    if (kind === "bool") {
      identity.setBoolAttribute(name, coerced);
    } else if (kind === "int") {
      identity.setIntAttribute(name, coerced);
    } else {
      identity.setUnicharAttribute(name, coerced);
    }
    return coerced;
  }

  function coerce(name, kind, value) {
    if (kind === "bool") {
      if (typeof value === "boolean") {
        return value;
      }
      if (value === "true" || value === "false") {
        return value === "true";
      }
      throw H.usage(`${name} is a true/false setting; got ${JSON.stringify(value)}`);
    }
    if (kind === "int") {
      const n = typeof value === "number" ? value : Number(value);
      if (!Number.isInteger(n)) {
        throw H.usage(`${name} is a whole number; got ${JSON.stringify(value)}`);
      }
      return n;
    }
    return value === null || value === undefined ? "" : String(value);
  }

  function snapshot(identity) {
    const out = { key: identity.key, label: identity.label || null, valid: identity.valid };
    for (const [name, kind] of Object.entries(FIELDS)) {
      out[name] = readField(identity, name, kind);
    }
    for (const name of FOLDER_FIELDS) {
      out[name] = H.safeGet(identity, name) ?? null;
    }
    const encryption = {};
    for (const [name, kind] of Object.entries(ENCRYPTION_FIELDS)) {
      encryption[name] = readField(identity, name, kind);
    }
    out.encryption = encryption;

    // The signature file is an nsIFile; report its path, not the object.
    try {
      out.signatureFile = identity.signature ? identity.signature.path : null;
    } catch (ex) {
      out.signatureFile = null;
    }
    // Which SMTP server this identity actually sends through, resolved for the
    // caller so it does not have to cross-reference smtp.list itself.
    try {
      const server = identity.smtpServerKey
        ? H.outgoing.getServerByKey(identity.smtpServerKey)
        : H.outgoing.defaultServer;
      out.outgoingServer = server
        ? {
            key: server.key,
            description: server.description || null,
            usingDefault: !identity.smtpServerKey,
          }
        : null;
    } catch (ex) {
      out.outgoingServer = null;
    }
    // Which account owns it, so a caller can jump straight to account_get_server.
    try {
      const account = H.accounts.FindAccountForServer
        ? null
        : [...H.accounts.accounts].find((a) =>
            [...(a.identities || [])].some((i) => i.key === identity.key)
          );
      out.accountKey = account ? account.key : null;
    } catch (ex) {
      out.accountKey = null;
    }
    return out;
  }

  TBX_MODULES["identities.get"] = async (params) => {
    const identity = H.identity(H.need(params, "identityKey"));
    return snapshot(identity);
  };

  TBX_MODULES["identities.set"] = async (params) => {
    const key = H.need(params, "identityKey");
    const identity = H.identity(key);
    const requested = Object.keys(params).filter((name) => name !== "identityKey");
    if (!requested.length) {
      throw H.usage("pass at least one field to change");
    }
    const unknown = requested.filter((name) => !(name in FIELDS));
    if (unknown.length) {
      throw H.usage(
        `cannot set ${unknown.join(", ")} here (writable: ${Object.keys(FIELDS).join(", ")}); ` +
          "signatures go through identities.setSignature, filing folders through " +
          "accounts.setCopiesAndFolders, encryption through identities.setEncryption"
      );
    }

    // Changing the from-address quietly breaks replies and any filter matching on
    // it, so refuse an obviously malformed one rather than let it through.
    if ("email" in params && params.email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(params.email)) {
      throw H.usage(`${params.email} does not look like an email address`);
    }
    if ("smtpServerKey" in params && params.smtpServerKey) {
      try {
        H.outgoing.getServerByKey(params.smtpServerKey);
      } catch (ex) {
        const keys = [...H.outgoing.servers].map((s) => s.key).join(", ");
        throw H.usage(`no outgoing server with key ${params.smtpServerKey} (known: ${keys})`);
      }
    }

    const before = snapshot(identity);
    const applied = {};
    for (const name of requested) {
      applied[name] = writeField(identity, name, FIELDS[name], params[name]);
    }
    H.flushPrefs();
    const after = snapshot(identity);

    const previous = {};
    const current = {};
    for (const name of requested) {
      previous[name] = before[name];
      current[name] = after[name];
    }
    return { identityKey: key, previous, current, applied: requested };
  };

  TBX_MODULES["identities.setSignature"] = async (params) => {
    const key = H.need(params, "identityKey");
    const identity = H.identity(key);
    const before = snapshot(identity);

    const text = params.signature;
    const filePath = params.filePath;
    if (text !== null && text !== undefined && filePath) {
      throw H.usage("pass either signature text or filePath, not both");
    }

    const warnings = [];
    if (filePath) {
      const FileUtils = needMod("FileUtils");
      const file = new FileUtils.File(filePath);
      if (!file.exists()) {
        throw H.usage(`signature file does not exist: ${filePath}`);
      }
      identity.signature = file;
      identity.setUnicharAttribute("sig_file", filePath);
      // A file signature and inline text are mutually exclusive; clearing the text
      // avoids Thunderbird appending both.
      identity.htmlSigText = "";
    } else if (text !== null && text !== undefined) {
      const isHtml = Boolean(params.isHtml);
      const looksLikeMarkup = /<\s*(a|b|i|br|div|p|span|table|img|strong|em)\b/i.test(text);
      if (looksLikeMarkup && !isHtml) {
        // Getting this flag wrong makes every outgoing mail show raw tags, which the
        // user will not notice until a recipient tells them.
        warnings.push(
          "the signature contains markup but isHtml was false, so recipients would " +
            "see the tags literally; set isHtml=true if that was not intended"
        );
      }
      identity.htmlSigText = text;
      identity.htmlSigFormat = isHtml;
      identity.signature = null;
      identity.setUnicharAttribute("sig_file", "");
    }

    if (params.attach !== undefined && params.attach !== null) {
      identity.attachSignature = Boolean(params.attach);
    } else if (text || filePath) {
      identity.attachSignature = true;
    }
    if (params.belowQuote !== undefined && params.belowQuote !== null) {
      identity.sigBottom = Boolean(params.belowQuote);
    }

    H.flushPrefs();
    const after = snapshot(identity);
    const view = (snap) => ({
      attachSignature: snap.attachSignature,
      htmlSigFormat: snap.htmlSigFormat,
      sigBottom: snap.sigBottom,
      signatureFile: snap.signatureFile,
      textLength: (snap.htmlSigText || "").length,
    });
    return {
      identityKey: key,
      previous: view(before),
      current: view(after),
      warnings,
    };
  };

  TBX_MODULES["identities.setEncryption"] = async (params) => {
    const key = H.need(params, "identityKey");
    const identity = H.identity(key);
    const requested = Object.keys(params).filter((name) => name !== "identityKey");
    const unknown = requested.filter((name) => !(name in ENCRYPTION_FIELDS));
    if (unknown.length) {
      throw H.usage(
        `cannot set ${unknown.join(", ")} (writable: ${Object.keys(ENCRYPTION_FIELDS).join(", ")})`
      );
    }
    if (!requested.length) {
      throw H.usage("pass at least one encryption field to change");
    }

    const before = snapshot(identity).encryption;
    for (const name of requested) {
      writeField(identity, name, ENCRYPTION_FIELDS[name], params[name]);
    }
    H.flushPrefs();
    const after = snapshot(identity).encryption;

    const warnings = [];
    if (after.encryptionPolicy === 2 && !after.openPgpKeyId) {
      warnings.push(
        "encryption is now required but no OpenPGP key is selected for this identity, " +
          "so sending will fail until one is chosen in Account Settings"
      );
    }
    return { identityKey: key, previous: before, current: after, warnings };
  };
}

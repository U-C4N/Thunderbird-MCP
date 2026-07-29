/* OpenPGP — key inspection, and a public-key import. Nothing else.
 *
 * EnigmailKeyRing (chrome://openpgp/content/modules/keyRing.sys.mjs) and RNP
 * (chrome://openpgp/content/modules/RNP.sys.mjs) both import cleanly on 153. The
 * test profile held zero keys, so every handler here treats an empty key ring as a
 * normal answer rather than a failure — that is the state most profiles are in.
 *
 * Policy, spelled out because it will otherwise read as a missing feature: this
 * bridge does not move private keys. exportKey returns public key blocks only; it
 * will not call EnigmailKeyRing.extractSecretKey or RNP.backupSecretKeys, will not
 * unlock the OpenPGP master password, and will not decrypt a message on request.
 * Secret key material that leaves a profile through an automated channel cannot be
 * un-leaked, and a tool call driven by a language model is the wrong place for that
 * decision. Those requests are refused with H.blocked and pointed at Thunderbird's
 * own Key Manager, where the user is present. This is deliberate, not an oversight;
 * if you are here to "finish" secret export, don't.
 *
 * Importing is a public-key-only path for the same reason: adding secret material to
 * a profile needs the master password and a human.
 */

TBX_MODULE_NAMES.push("openpgp");

{
  /** GnuPG-style validity codes as they appear in EnigmailKeyObj.keyTrust and
   *  ownerTrust. Mapped here rather than through EnigmailTrust so this module needs
   *  only the two OpenPGP URLs core.js already knows about. */
  const TRUST = {
    o: "unknown (new key)",
    i: "invalid",
    d: "disabled",
    r: "revoked",
    e: "expired",
    q: "undefined",
    "-": "undefined",
    n: "not trusted",
    m: "marginal",
    f: "full",
    u: "ultimate",
  };

  const SECRET_REFUSAL =
    "this bridge will not move private keys: it does not export secret key " +
    "material, unlock the OpenPGP master password, or decrypt on request";

  function ring() {
    return needMod("EnigmailKeyRing");
  }

  /** The whole key ring as a plain array.
   *
   *  getAllKeys() has returned both a bare array and the internal key-list object
   *  over the life of this API, so accept either shape instead of pinning one. */
  function allKeys() {
    const keyring = ring();
    let result = null;
    try {
      result = keyring.getAllKeys();
    } catch (ex) {
      throw H.unsupported(
        `Thunderbird's OpenPGP key ring would not load (${ex.message || ex}); ` +
          "the RNP backend may be disabled in this profile"
      );
    }
    if (Array.isArray(result)) {
      return result;
    }
    const list = result && (result.keyList || result.keys);
    return Array.isArray(list) ? list : [];
  }

  function iso(seconds) {
    const value = Number(seconds);
    return Number.isFinite(value) && value > 0 ? new Date(value * 1000).toISOString() : null;
  }

  function trustLabel(code) {
    const text = String(code === undefined || code === null ? "" : code).trim();
    // Several flags can be packed into one string ("er"); the first is the one the
    // Key Manager displays.
    return TRUST[text.charAt(0)] || (text || "unknown");
  }

  function summarise(key) {
    const trust = String(key.keyTrust || "");
    const expiry = Number(key.expiryTime) || 0;
    return {
      keyId: key.keyId || null,
      fingerprint: key.fpr || null,
      userId: key.userId || null,
      userIds: (key.userIds || []).map((uid) => ({
        userId: uid.userId || null,
        validity: trustLabel(uid.keyTrust),
        type: uid.type || null,
      })),
      created: iso(key.keyCreated),
      expires: iso(expiry),
      neverExpires: expiry === 0,
      expired: trust.charAt(0) === "e" || (expiry > 0 && expiry * 1000 < Date.now()),
      revoked: trust.charAt(0) === "r",
      disabled: trust.charAt(0) === "d",
      validity: trustLabel(trust),
      ownerTrust: trustLabel(key.ownerTrust),
      algorithm: key.algoSym || null,
      bits: Number(key.keySize) || null,
      // Secret material *present* is not the same as usable — an offline primary key
      // looks like this too. It is still the flag that decides whether this key can
      // sign, and the flag that makes exportKey refuse anything but the public half.
      secretAvailable: Boolean(key.secretAvailable),
      useFor: key.keyUseFor || null,
      canEncrypt: /e/i.test(key.keyUseFor || ""),
      canSign: /s/i.test(key.keyUseFor || ""),
      canCertify: /c/i.test(key.keyUseFor || ""),
    };
  }

  function searchText(summary) {
    return [
      summary.keyId,
      summary.fingerprint,
      summary.userId,
      ...summary.userIds.map((uid) => uid.userId),
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
  }

  /** Locate a key by fingerprint, long or short key id, with or without 0x. */
  function findKey(id) {
    const wanted = String(id).trim().replace(/\s+/g, "").replace(/^0x/i, "").toLowerCase();
    if (!wanted) {
      throw H.usage("keyId must be a fingerprint or key id from openpgp.listKeys");
    }
    const keys = allKeys();
    for (const key of keys) {
      const fpr = String(key.fpr || "").toLowerCase();
      const keyId = String(key.keyId || "").toLowerCase();
      if (fpr === wanted || keyId === wanted || fpr.endsWith(wanted) || keyId.endsWith(wanted)) {
        return key;
      }
    }
    throw H.usage(
      keys.length
        ? `no key in this profile matches ${id} (${keys.length} present; use the ` +
            "keyId or fingerprint exactly as openpgp.listKeys reported it)"
        : `no key matches ${id} — this profile's OpenPGP key ring is empty`
    );
  }

  TBX_MODULES["openpgp.listKeys"] = async (params) => {
    const keys = allKeys();
    const search = params.search ? String(params.search).toLowerCase() : "";
    const onlySecret = Boolean(params.onlySecret);
    const limit = Number.isInteger(params.limit) ? Math.min(Math.max(params.limit, 1), 500) : 200;

    const matched = [];
    let secretCount = 0;
    for (const key of keys) {
      const summary = summarise(key);
      if (summary.secretAvailable) {
        secretCount += 1;
      }
      if (onlySecret && !summary.secretAvailable) {
        continue;
      }
      if (search && !searchText(summary).includes(search)) {
        continue;
      }
      matched.push(summary);
    }
    const shown = matched.slice(0, limit);
    return {
      keys: shown,
      count: shown.length,
      matched: matched.length,
      omitted: matched.length - shown.length,
      total: keys.length,
      secretKeys: secretCount,
      note: keys.length
        ? undefined
        : "This profile has no OpenPGP keys. Keys are created or imported in " +
          "Account Settings > End-to-End Encryption; nothing here can be signed or " +
          "decrypted until one exists.",
    };
  };

  /** The Key Manager's real answer to "can I use this?", which accounts for expiry,
   *  revocation and acceptance in a way keyTrust alone does not. */
  function usability(key, method) {
    const fn = key[method];
    if (typeof fn !== "function") {
      return null;
    }
    try {
      const result = fn.call(key) || {};
      const valid = result.valid !== undefined ? result.valid : result.keyValid;
      return {
        valid: valid === undefined ? null : Boolean(valid),
        reason: result.reason || null,
      };
    } catch (ex) {
      // Some builds want an argument here. Report why we could not tell rather than
      // implying the key is unusable.
      return { valid: null, reason: String(ex.message || ex) };
    }
  }

  TBX_MODULES["openpgp.keyDetails"] = async (params) => {
    const key = findKey(H.need(params, "keyId"));
    const details = summarise(key);
    details.subKeys = (key.subKeys || []).map((sub) => {
      const expiry = Number(sub.expiryTime) || 0;
      return {
        keyId: sub.keyId || null,
        created: iso(sub.keyCreated),
        expires: iso(expiry),
        neverExpires: expiry === 0,
        algorithm: sub.algoSym || null,
        bits: Number(sub.keySize) || null,
        validity: trustLabel(sub.keyTrust),
        useFor: sub.keyUseFor || null,
      };
    });
    details.photoAvailable = Boolean(key.photoAvailable);
    details.usability = {
      encryption: usability(key, "getEncryptionValidity"),
      signing: usability(key, "getSigningValidity"),
    };
    return details;
  };

  TBX_MODULES["openpgp.importKey"] = async (params) => {
    const armored = String(H.need(params, "armored"));
    if (/BEGIN PGP PRIVATE KEY BLOCK/i.test(armored)) {
      throw H.blocked(
        `${SECRET_REFUSAL}, and it does not import them either — that needs the ` +
          "master password and a human decision",
        "import it by hand: Account Settings > End-to-End Encryption > Add Key"
      );
    }
    if (!/BEGIN PGP PUBLIC KEY BLOCK/i.test(armored)) {
      throw H.usage(
        "armored must be an ASCII-armoured public key block starting with " +
          "-----BEGIN PGP PUBLIC KEY BLOCK----- (binary .gpg key files are not accepted)"
      );
    }

    const keyring = ring();
    const before = new Set(allKeys().map((key) => String(key.fpr || "").toLowerCase()));
    const errorObj = {};
    const importedObj = {};
    let exitCode = 0;
    if (typeof keyring.importKeyAsync === "function") {
      // (window, askToConfirm, keyBlock, isBinary, keyIds, errorMsgObj, importedKeysObj)
      exitCode = await keyring.importKeyAsync(
        null,
        false,
        armored,
        false,
        null,
        errorObj,
        importedObj
      );
    } else if (typeof keyring.importKeyDataSilent === "function") {
      await keyring.importKeyDataSilent(null, armored, false);
    } else {
      throw H.unsupported(
        "this build's EnigmailKeyRing exposes no import entry point this bridge knows"
      );
    }
    if (exitCode) {
      throw new Error(errorObj.value || `key import failed (code ${exitCode})`);
    }

    // Diffing fingerprints is the only reliable report: importedKeysObj is populated
    // inconsistently, and re-importing a key the ring already has is a no-op that
    // still returns success.
    const after = allKeys();
    const added = after
      .filter((key) => !before.has(String(key.fpr || "").toLowerCase()))
      .map(summarise);
    return {
      imported: added.length,
      keys: added,
      alreadyPresent: added.length === 0,
      total: after.length,
      reported: Array.isArray(importedObj.value) ? importedObj.value : undefined,
      note:
        "An imported key starts as undecided: Thunderbird will not encrypt to it " +
        "until someone accepts it in the OpenPGP Key Manager.",
    };
  };

  TBX_MODULES["openpgp.exportKey"] = async (params) => {
    const kind = String(params.kind || params.type || "public").toLowerCase();
    if (params.secret === true || params.includeSecret === true || kind === "secret" || kind === "private") {
      throw H.blocked(
        SECRET_REFUSAL,
        "back it up in Thunderbird: OpenPGP Key Manager > select the key > " +
          "File > Backup Secret Key(s)"
      );
    }
    const key = findKey(H.need(params, "keyId"));

    // Both entry points have wanted the id with and without the 0x prefix across
    // versions; try the combinations rather than guess this build's convention.
    const attempts = [];
    const keyring = ring();
    if (typeof keyring.extractPublicKey === "function") {
      attempts.push((id) => keyring.extractPublicKey(id));
    }
    const rnp = mod("RNP");
    if (rnp && typeof rnp.getPublicKey === "function") {
      attempts.push((id) => rnp.getPublicKey(id));
    }
    let armored = null;
    let failure = null;
    for (const attempt of attempts) {
      for (const id of [`0x${key.fpr}`, key.fpr]) {
        try {
          const block = attempt(id);
          if (block && String(block).includes("BEGIN PGP PUBLIC KEY BLOCK")) {
            armored = String(block);
            break;
          }
        } catch (ex) {
          failure = String(ex.message || ex);
        }
      }
      if (armored) {
        break;
      }
    }
    if (!armored) {
      throw H.unsupported(
        `could not extract the public key for ${key.fpr || params.keyId}` +
          (failure ? ` (${failure})` : "") +
          "; export it from the OpenPGP Key Manager instead"
      );
    }
    // Belt and braces. If some future API hands back secret material for a public
    // export, it stops here rather than going out on the wire.
    if (/PRIVATE KEY BLOCK/i.test(armored)) {
      throw H.blocked(
        "the key ring returned secret key material for a public-key export; " +
          "refusing to pass it on"
      );
    }

    return {
      keyId: key.keyId || null,
      fingerprint: key.fpr || null,
      userId: key.userId || null,
      kind: "public",
      armored,
      bytes: armored.length,
      secretAvailable: Boolean(key.secretAvailable),
      secretExported: false,
    };
  };
}

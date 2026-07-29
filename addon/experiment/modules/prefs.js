/* Preferences — the reference privileged module.
 *
 * Pattern for every module in this directory:
 *   - append `TBX_MODULE_NAMES.push("<name>")` once
 *   - assign handlers onto TBX_MODULES with dotted keys; the background page
 *     reaches them as method "x.<key>"
 *   - use H.need / H.usage / H.unsupported for argument and capability failures
 *   - return plain JSON-serialisable values
 *   - report the previous value on every write, so the caller can undo
 *
 * These files are spliced into experiment/implementation.js at build time, so
 * `H`, `mod`, `needMod`, `Services`, `Ci`, `IOUtils` and `PathUtils` are all in
 * scope. Do not add `import` statements or an IIFE that hides your handlers.
 */

TBX_MODULE_NAMES.push("prefs");

{
  /** Branches whose values are secrets or would let a caller redirect traffic.
   *  The Python layer enforces its own allow/deny lists; this is defence in depth
   *  at the only layer that actually has the privilege. */
  const HARD_DENY = [
    "network.proxy.",
    "security.",
    "signon.",
    "general.config.",
    "autoadmin.",
    "extensions.experiments.enabled",
    "xpinstall.signatures.required",
    "marionette.",
    "remote.",
    "devtools.",
  ];

  function assertWritable(name) {
    const lowered = String(name).toLowerCase();
    for (const prefix of HARD_DENY) {
      if (lowered.startsWith(prefix)) {
        throw H.blocked(
          `${name} is not writable through this bridge: it controls credentials, ` +
            "transport security, or the add-on trust model.",
          "change it by hand in Settings, or about:config"
        );
      }
    }
    if (/password|passwd|secret|token|apikey/i.test(name)) {
      throw H.blocked(`${name} looks like a credential and will not be written.`);
    }
  }

  TBX_MODULES["prefs.get"] = async (params) => {
    const name = H.need(params, "name");
    const info = H.readPref(name);
    if (info.type === "none") {
      return { name, exists: false, value: null, type: null };
    }
    return { ...info, exists: true, locked: Services.prefs.prefIsLocked(name) };
  };

  TBX_MODULES["prefs.getMany"] = async (params) => {
    const names = H.need(params, "names");
    if (!Array.isArray(names)) {
      throw H.usage("names must be an array of preference names");
    }
    return {
      prefs: names.map((name) => {
        const info = H.readPref(name);
        return info.type === "none"
          ? { name, exists: false, value: null, type: null }
          : { ...info, exists: true };
      }),
    };
  };

  TBX_MODULES["prefs.list"] = async (params) => {
    const prefix = params.prefix === undefined ? "" : String(params.prefix);
    const onlyUserSet = Boolean(params.onlyUserSet);
    const limit = Number.isInteger(params.limit) ? params.limit : 500;
    const names = Services.prefs.getChildList(prefix).sort();
    const out = [];
    let skipped = 0;
    for (const name of names) {
      if (onlyUserSet && !Services.prefs.prefHasUserValue(name)) {
        continue;
      }
      if (out.length >= limit) {
        skipped += 1;
        continue;
      }
      const info = H.readPref(name);
      if (info.type !== "none") {
        out.push(info);
      }
    }
    return { prefs: out, matched: names.length, omitted: skipped, prefix };
  };

  TBX_MODULES["prefs.set"] = async (params) => {
    const name = H.need(params, "name");
    const type = H.need(params, "type");
    assertWritable(name);
    if (Services.prefs.prefIsLocked(name)) {
      throw H.blocked(
        `${name} is locked by an enterprise policy or autoconfig file`,
        "unlock it in policies.json or mozilla.cfg"
      );
    }
    const before = H.readPref(name);
    if (before.type !== "none" && before.type !== type) {
      throw H.usage(
        `${name} is a ${before.type} preference, not ${type}. ` +
          `Pass type="${before.type}".`
      );
    }
    H.writePref(name, type, params.value);
    H.flushPrefs();
    const after = H.readPref(name);
    return {
      name,
      previous: before.type === "none" ? null : { value: before.value, type: before.type, wasSet: before.set },
      current: { value: after.value, type: after.type },
      // Most mail/display prefs apply live; a handful only bind at startup. We
      // cannot know reliably, so we say which ones are known to need a restart.
      restartRequired: /^(app\.update\.|mail\.server\.[^.]+\.type|network\.)/.test(name),
    };
  };

  TBX_MODULES["prefs.reset"] = async (params) => {
    const name = H.need(params, "name");
    assertWritable(name);
    const before = H.readPref(name);
    if (!Services.prefs.prefHasUserValue(name)) {
      return { name, changed: false, reason: "already at its default value" };
    }
    Services.prefs.clearUserPref(name);
    H.flushPrefs();
    const after = H.readPref(name);
    return {
      name,
      changed: true,
      previous: { value: before.value, type: before.type },
      current: { value: after.value, type: after.type },
    };
  };

  /** Everything the user has changed from default — the useful "show me my
   *  settings" answer, and small enough to return whole. */
  TBX_MODULES["prefs.userSet"] = async (params) => {
    const prefix = params.prefix === undefined ? "" : String(params.prefix);
    const out = [];
    for (const name of Services.prefs.getChildList(prefix)) {
      if (!Services.prefs.prefHasUserValue(name)) {
        continue;
      }
      if (/password|passwd|secret|token|apikey/i.test(name)) {
        out.push({ name, type: "string", value: "<redacted>", redacted: true });
        continue;
      }
      const info = H.readPref(name);
      if (info.type !== "none") {
        out.push(info);
      }
    }
    out.sort((a, b) => (a.name < b.name ? -1 : 1));
    return { prefs: out, count: out.length };
  };
}

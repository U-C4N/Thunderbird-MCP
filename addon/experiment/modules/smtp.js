/* Outgoing (SMTP) servers.
 *
 * The service is `MailServices.outgoingServer` on 128+ — `MailServices.smtp` is gone,
 * and `H.outgoing` papers over both. Measured surface on 153: createServer,
 * deleteServer, findServer, getServerByKey, getServerByIdentity, defaultServer,
 * servers (docs/VERIFIED-FINDINGS.md).
 *
 * Reads prefer the interface property and fall back to `mail.smtpserver.<key>.*`,
 * because the same build that returns null for an incoming server's hostName may well
 * do it here too; writes try the property first (so the service invalidates its own
 * cache) and fall back to the pref, reporting which path was taken. The socket type is
 * `socketType` on the interface but `try_ssl` in the pref, which is worth knowing
 * before you go looking for a pref that does not exist.
 *
 * Passwords are never touched. Deleting a server that identities point at is refused,
 * because the alternative is mail that silently cannot be sent.
 */

TBX_MODULE_NAMES.push("smtp");

{
  /** nsMsgSocketType, as for incoming servers. */
  const SOCKET_TYPES = {
    0: "plain",
    1: "tryStartTLS",
    2: "alwaysStartTLS",
    3: "SSL",
  };

  /** nsMsgAuthMethod. 0 ("never configured") is readable but not settable. */
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

  const WRITABLE = {
    hostname: { type: "string", props: ["hostname", "hostName"], pref: "hostname", host: true },
    port: {
      type: "int",
      props: ["port"],
      pref: "port",
      min: 0,
      max: 65535,
      note: "0 means the default port for the chosen socket type (587, or 465 for SSL)",
    },
    username: {
      type: "string",
      props: ["username"],
      pref: "username",
      note:
        "the stored password is keyed to host plus username, so Thunderbird will ask " +
        "for it again on the next send",
    },
    description: { type: "string", props: ["description"], pref: "description" },
    authMethod: { type: "enum", map: AUTH_METHODS, props: ["authMethod"], pref: "authMethod" },
    socketType: {
      type: "enum",
      map: SOCKET_TYPES,
      props: ["socketType", "trySSL"],
      pref: "try_ssl",
    },
  };

  /* ------------------------------------------------------------------ plumbing */

  function labelFor(map, value) {
    return Number.isInteger(value) && map[value] !== undefined ? map[value] : null;
  }

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
    throw H.usage(
      `${field} must be one of ${Object.keys(map)
        .map((key) => `${key} (${map[key]})`)
        .join(", ")}`
    );
  }

  function coerce(name, spec, value) {
    let coerced;
    if (spec.type === "enum") {
      coerced = enumValue(spec.map, value, name);
    } else if (spec.type === "int") {
      const parsed =
        typeof value === "string" && /^\d+$/.test(value.trim()) ? Number(value) : value;
      if (!Number.isInteger(parsed)) {
        throw H.usage(`${name} is an integer setting; got ${JSON.stringify(value)}`);
      }
      coerced = parsed;
    } else if (value === null) {
      coerced = "";
    } else if (typeof value === "string") {
      coerced = value;
    } else {
      throw H.usage(`${name} is a text setting; got ${JSON.stringify(value)}`);
    }
    if (spec.min !== undefined && coerced < spec.min) {
      throw H.usage(`${name} must be at least ${spec.min}`);
    }
    if (spec.max !== undefined && coerced > spec.max) {
      throw H.usage(`${name} must be at most ${spec.max}`);
    }
    if (spec.host && !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(coerced)) {
      throw H.usage(
        "hostname must be a bare host name such as smtp.example.com — no scheme, port, " +
          "path or spaces"
      );
    }
    return coerced;
  }

  function prefBase(server) {
    return `mail.smtpserver.${server.key}.`;
  }

  function uriOf(server) {
    try {
      const value = server.serverURI;
      if (!value) {
        return null;
      }
      return typeof value === "string" ? Services.io.newURI(value) : value;
    } catch (ex) {
      return null;
    }
  }

  function stringField(server, spec) {
    for (const prop of spec.props) {
      const direct = H.safeGet(server, prop);
      if (typeof direct === "string" && direct) {
        return direct;
      }
    }
    const pref = H.readPref(prefBase(server) + spec.pref);
    if (pref.type === "string" && pref.value) {
      return pref.value;
    }
    // Preserve a genuine empty string rather than pretending the field is absent.
    const direct = H.safeGet(server, spec.props[0]);
    return typeof direct === "string" ? direct : null;
  }

  function intField(server, spec) {
    for (const prop of spec.props) {
      const direct = H.safeGet(server, prop);
      if (typeof direct === "number") {
        return direct;
      }
    }
    const pref = H.readPref(prefBase(server) + spec.pref);
    return pref.type === "int" ? pref.value : null;
  }

  function readField(server, name) {
    const spec = WRITABLE[name];
    if (spec.host) {
      const found = stringField(server, spec);
      if (found) {
        return found;
      }
      const uri = uriOf(server);
      return uri ? uri.host || null : null;
    }
    return spec.type === "string" ? stringField(server, spec) : intField(server, spec);
  }

  function applyField(server, name, value) {
    const spec = WRITABLE[name];
    for (const prop of spec.props) {
      try {
        server[prop] = value;
      } catch (ex) {
        continue;
      }
      if (readField(server, name) === value) {
        return prop;
      }
    }
    const prefName = prefBase(server) + spec.pref;
    if (spec.type === "string") {
      Services.prefs.setStringPref(prefName, String(value));
    } else {
      Services.prefs.setIntPref(prefName, value);
    }
    return "pref";
  }

  function defaultServerKey(service) {
    try {
      const server = service.defaultServer;
      if (server && server.key) {
        return server.key;
      }
    } catch (ex) {
      // an empty profile has no default; the pref below is the next best answer
    }
    const pref = H.readPref("mail.smtp.defaultserver");
    return pref.type === "string" && pref.value ? pref.value : null;
  }

  /** Which identities point at which server, both explicitly (`smtpServerKey`) and
   *  after the service has resolved the default. The second list is what actually
   *  decides where a message goes. */
  function identityUsage(service) {
    const usage = new Map();
    const bucket = (key) => {
      if (!usage.has(key)) {
        usage.set(key, { explicit: [], effective: [] });
      }
      return usage.get(key);
    };
    let identities = [];
    try {
      identities = [...H.accounts.allIdentities];
    } catch (ex) {
      identities = [];
    }
    for (const identity of identities) {
      const label = { key: identity.key, email: H.safeGet(identity, "email") };
      const explicit = H.safeGet(identity, "smtpServerKey");
      if (typeof explicit === "string" && explicit) {
        bucket(explicit).explicit.push(label);
      }
      try {
        const resolved = service.getServerByIdentity(identity);
        if (resolved && resolved.key) {
          bucket(resolved.key).effective.push(label);
        }
      } catch (ex) {
        // an identity with a dangling smtpServerKey resolves to nothing; not our problem
      }
    }
    return usage;
  }

  function describe(server, defaultKey, usage) {
    const socketType = intField(server, WRITABLE.socketType);
    const authMethod = intField(server, WRITABLE.authMethod);
    const port = intField(server, WRITABLE.port);
    const uri = uriOf(server);
    const use = (usage && usage.get(server.key)) || { explicit: [], effective: [] };
    return {
      key: server.key,
      type: H.safeGet(server, "type") || "smtp",
      description: stringField(server, WRITABLE.description),
      UID: H.safeGet(server, "UID"),
      hostname: readField(server, "hostname"),
      port,
      // What the service will actually dial when `port` is 0.
      resolvedPort: uri && uri.port > 0 ? uri.port : port || null,
      username: stringField(server, WRITABLE.username),
      authMethod,
      authMethodName: labelFor(AUTH_METHOD_LABELS, authMethod),
      socketType,
      socketTypeName: labelFor(SOCKET_TYPES, socketType),
      // Same integer under its pref name, so a caller reading prefs.js recognises it.
      trySSL: socketType,
      isDefault: server.key === defaultKey,
      serverURI: uri ? uri.spec : null,
      usedByIdentities: use.explicit,
      sendingIdentities: use.effective,
      passwordOmitted: true,
    };
  }

  function byKey(service, key) {
    try {
      const server = service.getServerByKey(key);
      if (server) {
        return server;
      }
    } catch (ex) {
      // fall through to an error that lists what does exist
    }
    const known = [...service.servers].map((server) => server.key).join(", ");
    throw H.usage(`no outgoing server with key ${key} (known: ${known || "none"})`);
  }

  function writableNames() {
    return Object.keys(WRITABLE).sort().join(", ");
  }

  /* ---------------------------------------------------------------------- list */

  TBX_MODULES["smtp.list"] = async () => {
    const service = H.outgoing;
    const defaultKey = defaultServerKey(service);
    const usage = identityUsage(service);
    const servers = [...service.servers].map((server) => describe(server, defaultKey, usage));
    return { servers, count: servers.length, defaultServerKey: defaultKey };
  };

  /* -------------------------------------------------------------------- create */

  TBX_MODULES["smtp.create"] = async (params) => {
    const service = H.outgoing;
    const hostname = coerce("hostname", WRITABLE.hostname, H.need(params, "hostname"));
    const existing = [...service.servers];

    let server;
    try {
      server = service.createServer("smtp");
    } catch (ex) {
      // Pre-128 signature took no argument; the type is implicit there.
      server = service.createServer();
    }

    const applied = [];
    applied.push({ name: "hostname", value: hostname, via: applyField(server, "hostname", hostname) });
    for (const name of ["port", "username", "description", "authMethod", "socketType"]) {
      if (params[name] === undefined) {
        continue;
      }
      const value = coerce(name, WRITABLE[name], params[name]);
      applied.push({ name, value, via: applyField(server, name, value) });
    }
    H.flushPrefs();

    const defaultKey = defaultServerKey(service);
    const result = {
      created: true,
      server: describe(server, defaultKey, identityUsage(service)),
      applied,
      hint:
        "no identity uses this server yet — point one at it with x.identities.set " +
        "smtpServerKey, or make it the default with x.smtp.setDefault",
    };
    const clash = existing.find((other) => readField(other, "hostname") === hostname);
    if (clash) {
      // Duplicate SMTP entries for one host are a classic self-inflicted mess.
      result.warning = `${clash.key} already points at ${hostname}; you now have two entries for it`;
    }
    if (params.password !== undefined) {
      result.passwordIgnored =
        "passwords are not accepted here — Thunderbird will prompt on the first send " +
        "and store it in its own password manager";
    }
    return result;
  };

  /* -------------------------------------------------------------------- update */

  TBX_MODULES["smtp.update"] = async (params) => {
    const service = H.outgoing;
    const key = H.need(params, "key");
    const server = byKey(service, key);
    const wanted = Object.keys(params).filter((name) => name !== "key");
    if (!wanted.length) {
      throw H.usage(`nothing to change — pass one or more of ${writableNames()}`);
    }
    for (const name of wanted) {
      if (!WRITABLE[name]) {
        throw H.usage(
          `${name} is not a writable outgoing-server setting (writable: ${writableNames()}). ` +
            "Passwords are never written through this bridge."
        );
      }
    }

    const changes = [];
    for (const name of wanted) {
      const spec = WRITABLE[name];
      const value = coerce(name, spec, params[name]);
      const previous = readField(server, name);
      const via = applyField(server, name, value);
      const current = readField(server, name);
      const change = { name, previous, current, via, applied: current === value };
      if (spec.type === "enum") {
        change.previousName = labelFor(spec.map, previous);
        change.currentName = labelFor(spec.map, current);
      }
      if (spec.note) {
        change.note = spec.note;
      }
      changes.push(change);
    }
    H.flushPrefs();
    return {
      key: server.key,
      changes,
      server: describe(server, defaultServerKey(service), identityUsage(service)),
    };
  };

  /* -------------------------------------------------------------------- delete */

  TBX_MODULES["smtp.delete"] = async (params) => {
    const service = H.outgoing;
    const key = H.need(params, "key");
    const server = byKey(service, key);
    const defaultKey = defaultServerKey(service);
    const usage = identityUsage(service);
    const use = usage.get(server.key) || { explicit: [], effective: [] };

    if (use.explicit.length) {
      const who = use.explicit.map((id) => `${id.key} <${id.email || "no address"}>`).join(", ");
      throw H.blocked(
        `outgoing server ${key} (${readField(server, "hostname")}) is still used by ${who}, ` +
          "and deleting it would leave those identities unable to send.",
        "point those identities at another server first (x.identities.set smtpServerKey=…)"
      );
    }

    const snapshot = describe(server, defaultKey, usage);
    service.deleteServer(server);
    H.flushPrefs();

    const remaining = [...service.servers].map((other) => other.key);
    const result = {
      deleted: true,
      key,
      previous: snapshot,
      wasDefault: key === defaultKey,
      defaultServerKey: defaultServerKey(service),
      remaining,
    };
    if (result.wasDefault && use.effective.length) {
      // These identities had no explicit key, so they followed the default and have
      // just been moved somewhere else without being asked.
      result.warning =
        `${use.effective.map((id) => id.key).join(", ")} sent through this server as the ` +
        `default and will now use ${result.defaultServerKey || "no server at all"}`;
    }
    if (!remaining.length) {
      result.warning = "no outgoing servers are left, so nothing can be sent until one is added";
    }
    return result;
  };

  /* ---------------------------------------------------------------- setDefault */

  TBX_MODULES["smtp.setDefault"] = async (params) => {
    const service = H.outgoing;
    const key = H.need(params, "key");
    const server = byKey(service, key);
    const previous = defaultServerKey(service);
    if (previous === key) {
      return { key, changed: false, reason: "already the default outgoing server" };
    }
    service.defaultServer = server;
    H.flushPrefs();
    const current = defaultServerKey(service);
    const usage = identityUsage(service);
    return {
      changed: current === key,
      previous,
      current,
      // Identities with an explicit smtpServerKey are unaffected; these are the ones
      // whose outgoing route just changed.
      affectedIdentities: (usage.get(key) || { effective: [] }).effective,
      server: describe(server, current, usage),
    };
  };
}

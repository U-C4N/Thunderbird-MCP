/* Method registry and the error contract.
 *
 * Handler modules call `tbxRegistry.define("messages.query", fn)` at load time.
 * `fn` receives the request `params` and returns a JSON-serialisable value.
 *
 * Throwing is the normal way to fail. Use the `tbxError.*` helpers so the Python
 * side receives a typed error (see docs/PROTOCOL.md) instead of a bare string —
 * the `kind` decides whether the model is told to fix its arguments, told the
 * build cannot do it, or shown a Thunderbird failure.
 */

var tbxError = {
  usage(message, extra) {
    return Object.assign(new Error(message), { tbxKind: "usage", ...extra });
  },
  unsupported(message, extra) {
    return Object.assign(new Error(message), { tbxKind: "unsupported", ...extra });
  },
  blocked(message, needs, extra) {
    return Object.assign(new Error(message), { tbxKind: "blocked", needs, ...extra });
  },
  thunderbird(message, extra) {
    return Object.assign(new Error(message), { tbxKind: "thunderbird", ...extra });
  },
  /**
   * Unpack a failure the privileged half packed into a message, or null.
   *
   * Only a message survives the hop out of the experiment sandbox, so
   * experiment/core.js serialises the whole taxonomy into one and everything
   * that calls `browser.tbx.invoke` unpacks it here.
   */
  fromWire(ex) {
    const message = String((ex && ex.message) || ex);
    if (!message.startsWith("tbxerr:")) {
      return null;
    }
    let payload = null;
    try {
      payload = JSON.parse(message.slice("tbxerr:".length));
    } catch (parseError) {
      return null; // a message that merely starts like ours
    }
    if (!payload || !payload.message) {
      return null;
    }
    const extra = payload.code ? { code: payload.code } : undefined;
    switch (payload.kind) {
      case "usage":
        return this.usage(payload.message, extra);
      case "unsupported":
        return this.unsupported(payload.message, extra);
      case "blocked":
        return this.blocked(payload.message, payload.needs, extra);
      default:
        return this.thunderbird(payload.message, extra);
    }
  },
  /** What to print about a failure: our envelope unwrapped, or the message as-is.
   *  For a log line, where the kind and the needs have nowhere to go. */
  readable(ex) {
    const typed = this.fromWire(ex);
    return String((typed && typed.message) || (ex && ex.message) || ex);
  },
  /** Normalise anything thrown into the wire shape. */
  serialize(ex) {
    if (!ex) {
      return { kind: "internal", message: "unknown failure" };
    }
    const payload = {
      kind: ex.tbxKind || "thunderbird",
      message: String(ex.message || ex),
      // A bare "Error" is not a code — it reached the model as a second, empty
      // failure tag alongside the real one.
      code: ex.code || (ex.name && ex.name !== "Error" ? ex.name : null),
    };
    if (ex.needs) {
      payload.needs = Array.isArray(ex.needs) ? ex.needs : [ex.needs];
    }
    if (ex.result !== undefined) {
      // XPCOM failures carry an nsresult; it is the most useful code we have.
      payload.code = String(ex.result);
    }
    return payload;
  },
};

var tbxRegistry = (() => {
  const handlers = new Map();

  return {
    /**
     * @param {string} method  "<module>.<action>", prefixed "x." when privileged.
     * @param {(params: object, ctx: object) => Promise<any>} fn
     */
    define(method, fn) {
      if (handlers.has(method)) {
        throw new Error(`duplicate handler for ${method}`);
      }
      handlers.set(method, fn);
    },
    has(method) {
      return handlers.has(method);
    },
    methods() {
      return [...handlers.keys()].sort();
    },
    async invoke(method, params, ctx) {
      const fn = handlers.get(method);
      if (!fn) {
        throw tbxError.usage(
          `unknown method ${method}. This add-on is older than the server; ` +
            "run `tbmcp install-addon` to update it."
        );
      }
      return fn(params || {}, ctx || {});
    },
  };
})();

/* Small shared helpers for handlers. */
var tbxUtil = {
  /** Require a parameter, with a message that tells the caller what to send. */
  need(params, name, type) {
    const value = params[name];
    if (value === undefined || value === null || value === "") {
      throw tbxError.usage(`${name} is required`);
    }
    if (type === "int" && !Number.isInteger(value)) {
      throw tbxError.usage(`${name} must be an integer`);
    }
    if (type === "array" && !Array.isArray(value)) {
      throw tbxError.usage(`${name} must be an array`);
    }
    if (type === "string" && typeof value !== "string") {
      throw tbxError.usage(`${name} must be a string`);
    }
    return value;
  },
  /** Run promises with bounded concurrency; keeps bulk ops from stalling the UI. */
  async mapLimited(items, limit, fn) {
    const results = new Array(items.length);
    let cursor = 0;
    const workers = new Array(Math.min(limit, items.length || 1)).fill(0).map(async () => {
      while (cursor < items.length) {
        const index = cursor++;
        try {
          results[index] = { ok: true, value: await fn(items[index], index) };
        } catch (ex) {
          results[index] = { ok: false, error: String(ex.message || ex), item: items[index] };
        }
      }
    });
    await Promise.all(workers);
    return results;
  },
  /** Clamp a caller-supplied limit into a sane range. */
  limit(value, fallback, max) {
    const n = Number.isInteger(value) ? value : fallback;
    return Math.min(Math.max(n, 1), max);
  },
  /** Split a mapLimited result into values and failures. */
  partition(results) {
    const values = [];
    const failures = [];
    for (const entry of results) {
      if (entry && entry.ok) {
        values.push(entry.value);
      } else if (entry) {
        failures.push({ item: entry.item, error: entry.error });
      }
    }
    return { values, failures };
  },
};

/* Binary payloads travel as base64 in JSON frames. */
var tbxBase64 = {
  fromArrayBuffer(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    // Chunked to stay under the argument limit of String.fromCharCode.
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary);
  },
  toUint8Array(base64) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
  },
};

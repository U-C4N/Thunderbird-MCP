/* The outbound half of the bridge.
 *
 * Python owns the listener; we dial out. The daemon writes
 * <profile>/tbmcp-bridge.json with the port and a token, which we read through the
 * privileged half (a background page cannot read arbitrary files). Then we connect,
 * authenticate, and answer requests until the socket drops — at which point we back
 * off and try again, so a daemon restart heals itself without touching Thunderbird.
 */

var tbxTransport = (() => {
  const PROTOCOL = 1;
  const CHUNK_SIZE = 3 * 1024 * 1024; // stay under the daemon's 4 MiB frame ceiling
  /* Two schedules, because the two failure modes are nothing alike.
   *
   * "No pairing file" is the normal resting state: no daemon has started yet. The
   * check is a local file stat, so polling it briskly costs nothing — and backing
   * off here is what made a freshly started daemon wait up to half a minute before
   * anything worked.
   *
   * A failed connection is different: something is listening or the port is wrong,
   * and hammering it helps nobody. */
  const WAIT_MS = [500, 1000, 1000, 2000, 2000];
  const BACKOFF_MS = [500, 1000, 2000, 4000, 8000, 15000, 30000];

  /** How often to re-check that we are still attached to the advertised daemon. */
  const SUPERVISE_MS = 10000;

  /* An open socket that never gets its `welcome` is the worst state to be in: it
   * looks connected from here, answers nothing, and used to sit there until the
   * daemon's own timeout dropped it — silently, on repeat, for as long as
   * Thunderbird stayed up. Give up on it ourselves, and say so. */
  const HELLO_TIMEOUT_MS = 8000;

  /* And the same for the step before it. A socket that reaches TCP but never
   * completes the HTTP upgrade stays CONNECTING forever as far as Gecko is
   * concerned — no error event, no close — so nothing else would ever free it. */
  const CONNECT_TIMEOUT_MS = 15000;

  let socket = null;
  let attempt = 0;
  let waits = 0;
  let supervisor = null;
  let currentPort = null;
  let rejections = 0;
  let retryTimer = null;
  let stopped = false;
  /* What `hello` announces about this Thunderbird. Kept here, ready to send,
   * because the handshake must not wait on a probe: both probes cross into the
   * privileged half, and one that never answers used to leave the socket open and
   * silent until the daemon gave up on it. */
  let identity = { app: null, capabilities: null };
  let helloTimer = null;
  let connectTimer = null;
  let welcomed = false; // has the live socket completed its handshake?
  const inFlight = new Map(); // request id -> AbortController-ish flag

  function state() {
    if (!socket) {
      return "disconnected";
    }
    if (socket.readyState !== WebSocket.OPEN) {
      return "connecting";
    }
    // Open but unanswered is its own state, and the one worth reporting: it is
    // what a wedged handshake looks like from in here.
    return welcomed ? "connected" : "handshaking";
  }

  async function readPairing() {
    if (!browser.tbx) {
      throw new Error("the privileged half of the add-on is unavailable");
    }
    const pairing = await browser.tbx.readBridgeFile();
    if (!pairing) {
      throw new Error("no pairing file yet");
    }
    if (Number(pairing.version) !== PROTOCOL) {
      throw new Error(
        `pairing file is protocol ${pairing.version}, this add-on speaks ${PROTOCOL}`
      );
    }
    return pairing;
  }

  function send(message) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }
    socket.send(JSON.stringify(message));
    return true;
  }

  /** Send a result, splitting anything oversized into base64 chunks. */
  function sendResult(id, ok, payload) {
    const frame = ok
      ? { t: "res", id, ok: true, result: payload }
      : { t: "res", id, ok: false, error: payload };
    const encoded = JSON.stringify(frame);
    if (encoded.length <= CHUNK_SIZE) {
      send(frame);
      return;
    }
    // Chunk the *result* only; errors are never this large.
    const body = new TextEncoder().encode(JSON.stringify(payload));
    let offset = 0;
    let seq = 0;
    while (offset < body.length) {
      const slice = body.subarray(offset, offset + CHUNK_SIZE);
      offset += slice.length;
      let binary = "";
      for (let i = 0; i < slice.length; i += 0x8000) {
        binary += String.fromCharCode.apply(null, slice.subarray(i, i + 0x8000));
      }
      send({
        t: "res",
        id,
        ok: true,
        chunked: { seq, last: offset >= body.length },
        result: btoa(binary),
      });
      seq += 1;
    }
  }

  async function handleRequest(frame) {
    const { id, method, params } = frame;
    const ctx = {
      cancelled: false,
      progress(done, total, message) {
        send({ t: "progress", id, done, total, message });
      },
    };
    inFlight.set(id, ctx);
    const startedAt = Date.now();
    try {
      const result = await tbxRegistry.invoke(method, params, ctx);
      tbxLog.debug(`${method} ok in ${Date.now() - startedAt}ms`);
      sendResult(id, true, result === undefined ? null : result);
    } catch (ex) {
      const error = tbxError.serialize(ex);
      // Genuine Thunderbird failures are worth surfacing in the console; usage
      // errors are the model's problem and would just be noise.
      if (error.kind !== "usage") {
        tbxLog.warn(`${method} failed:`, error.message);
      } else {
        tbxLog.debug(`${method} rejected: ${error.message}`);
      }
      sendResult(id, false, error);
    } finally {
      inFlight.delete(id);
    }
  }

  function onMessage(event) {
    let frame;
    try {
      frame = JSON.parse(event.data);
    } catch (ex) {
      tbxLog.warn("dropping malformed frame from the daemon");
      return;
    }
    switch (frame && frame.t) {
      case "req":
        handleRequest(frame);
        break;
      case "cancel": {
        const ctx = inFlight.get(frame.id);
        if (ctx) {
          ctx.cancelled = true;
        }
        break;
      }
      case "ping":
        send({ t: "pong", ts: frame.ts });
        break;
      case "welcome":
        tbxLog.info("bridge established");
        clearTimeout(helloTimer);
        welcomed = true;
        attempt = 0;
        waits = 0;
        rejections = 0;
        refreshIdentity();
        break;
      default:
        break;
    }
  }

  /**
   * Re-probe once the connection is up, so the next reconnect is not stale.
   *
   * Off the handshake path on purpose: this is where the slow calls live, and
   * nothing waits for the answer. A probe that fails leaves the previous identity
   * in place — a stale description is still better than none.
   */
  function refreshIdentity() {
    Promise.all([tbxCapabilities.appInfo(), tbxCapabilities.describe()])
      .then(([app, capabilities]) => {
        identity = { app, capabilities };
      })
      .catch((ex) => {
        tbxLog.debug("identity refresh failed:", ex.message || ex);
      });
  }

  /** @param {"waiting"|"failed"} kind  which schedule to use. */
  function scheduleRetry(why, kind) {
    if (stopped) {
      return;
    }
    let delay;
    if (kind === "waiting") {
      delay = WAIT_MS[Math.min(waits, WAIT_MS.length - 1)];
      waits += 1;
    } else {
      delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
      attempt += 1;
    }
    tbxLog.debug(`retrying in ${delay}ms (${why})`);
    clearTimeout(retryTimer);
    retryTimer = setTimeout(connect, delay);
  }

  async function connect() {
    if (stopped || (socket && socket.readyState <= WebSocket.OPEN)) {
      return;
    }
    let pairing;
    try {
      pairing = await readPairing();
    } catch (ex) {
      const message = String(ex.message || ex);
      // No pairing file is the resting state, not a failure: poll, do not back off.
      scheduleRetry(message, message.includes("no pairing file") ? "waiting" : "failed");
      return;
    }

    // The pairing file exists, so we are no longer waiting for a daemon to appear.
    // Resetting here matters: after a long stretch with no daemon the wait counter
    // sits at its ceiling, and a freshly written pairing file would otherwise not be
    // acted on for seconds.
    waits = 0;

    const url = `ws://127.0.0.1:${pairing.port}/tbmcp`;
    tbxLog.debug(`connecting to ${url}`);
    currentPort = pairing.port;
    try {
      socket = new WebSocket(url);
    } catch (ex) {
      socket = null;
      currentPort = null;
      scheduleRetry(`WebSocket constructor failed: ${ex.message || ex}`, "failed");
      return;
    }

    // Handlers bind to this socket rather than to whatever `socket` holds when they
    // fire, so a late event from a superseded socket cannot disturb the live one.
    const ws = socket;
    welcomed = false;

    clearTimeout(connectTimer);
    connectTimer = setTimeout(() => {
      if (ws.readyState !== WebSocket.CONNECTING) {
        return;
      }
      tbxLog.error(
        `no WebSocket handshake with the daemon on port ${pairing.port} within ` +
          `${CONNECT_TIMEOUT_MS}ms — closing the socket; \`tbmcp doctor\` shows the ` +
          "daemon's view"
      );
      ws.close(4000, "connect timeout");
    }, CONNECT_TIMEOUT_MS);

    ws.onopen = () => {
      clearTimeout(connectTimer);
      send({
        t: "hello",
        token: pairing.token,
        protocol: PROTOCOL,
        addonVersion: browser.runtime.getManifest().version,
        app: identity.app || tbxCapabilities.appInfoSync(),
        capabilities: identity.capabilities || tbxCapabilities.describeSync(),
      });
      clearTimeout(helloTimer);
      helloTimer = setTimeout(() => {
        tbxLog.error(
          `no welcome from the daemon within ${HELLO_TIMEOUT_MS}ms after hello — closing ` +
            "the socket; `tbmcp doctor` shows the daemon's view"
        );
        ws.close(4000, "no welcome");
      }, HELLO_TIMEOUT_MS);
    };
    ws.onmessage = onMessage;
    ws.onerror = () => {
      // onclose always follows; retry is scheduled there.
      tbxLog.debug("socket error");
    };
    ws.onclose = (event) => {
      clearTimeout(helloTimer);
      clearTimeout(connectTimer);
      socket = null;
      currentPort = null;
      for (const ctx of inFlight.values()) {
        ctx.cancelled = true;
      }
      inFlight.clear();
      const code = event ? event.code : 1006;

      // A rejected token (4001) nearly always means the daemon we paired with has been
      // replaced and the file we read is stale — not that the install is broken. So
      // re-read and try again promptly, and only complain after several rounds have
      // failed with the file we just read.
      if (code === 4001) {
        rejections += 1;
        if (rejections >= 4) {
          tbxLog.error(
            "the daemon keeps rejecting our token. Reinstall with `tbmcp install-addon`, " +
              "or stop any stray `tbmcp daemon` processes."
          );
        } else {
          tbxLog.debug(`token rejected (round ${rejections}); re-reading the pairing file`);
        }
      } else {
        rejections = 0;
      }

      // Almost every other close means the daemon went away: it exited on its idle
      // timer (1000/1001), was superseded (1012), or its process died and took the
      // socket with it (1006). In all of those the right response is to poll for a new
      // pairing file, not to back off — the next daemon may be seconds away and the
      // check is a local file stat.
      //
      // Only a protocol mismatch is worth backing off for: that will not fix itself.
      if (code === 4002) {
        tbxLog.error(
          `daemon rejected the connection (code ${code}: ${event && event.reason}). ` +
            "The add-on and the server disagree on the protocol — reinstall with " +
            "`tbmcp install-addon`."
        );
      }
      // 4000 is ours: a watchdog gave up on this socket. Whatever is on that port
      // is not talking to us, so back off rather than poll.
      scheduleRetry(`closed ${code}`, code === 4002 || code === 4000 ? "failed" : "waiting");
    };
  }

  /**
   * Keep following the advertisement, and keep the retry chain alive.
   *
   * Two failures this guards against, both observed:
   *
   * 1. A second daemon starts and rewrites the pairing file. Our socket to the first
   *    one stays open, so nothing tells us to move, and every tool call through the
   *    new daemon reports "Thunderbird is not connected" while both sides look fine.
   *    Comparing the advertised port to ours catches that.
   * 2. `connect()` returns early whenever a socket already exists, so if it is ever
   *    entered while one is still CONNECTING, no new timer gets armed and the chain
   *    dies until that socket closes. A periodic tick makes that unrecoverable state
   *    impossible.
   */
  async function supervise() {
    if (stopped) {
      return;
    }
    if (!socket) {
      connect();
      return;
    }
    if (socket.readyState !== WebSocket.OPEN) {
      return; // a handshake in flight; onclose will re-arm if it fails
    }
    let pairing = null;
    try {
      pairing = await readPairing();
    } catch (ex) {
      // The daemon has gone and taken its pairing file with it. Our socket will
      // notice soon enough; do not tear down a working connection on a read error.
      return;
    }
    if (pairing.port !== currentPort) {
      tbxLog.info(
        `daemon moved from port ${currentPort} to ${pairing.port} — reconnecting`
      );
      socket.close(1000, "following the pairing file");
    }
  }

  return {
    /**
     * @param {{app: object|null, capabilities: object|null}} [seed]  what main.js
     *   already fetched for the status file; either half may be null.
     */
    start(seed) {
      stopped = false;
      identity = {
        app: (seed && seed.app) || null,
        capabilities: (seed && seed.capabilities) || null,
      };
      connect();
      clearInterval(supervisor);
      supervisor = setInterval(supervise, SUPERVISE_MS);
    },
    stop() {
      stopped = true;
      clearTimeout(retryTimer);
      clearTimeout(helloTimer);
      clearTimeout(connectTimer);
      clearInterval(supervisor);
      supervisor = null;
      if (socket) {
        socket.close(1000, "shutting down");
        socket = null;
      }
    },
    /** Used by events.js to push unsolicited notifications. */
    emit(name, data) {
      send({ t: "event", name, data });
    },
    status() {
      return { state: state(), attempt, inFlight: inFlight.size };
    },
  };
})();

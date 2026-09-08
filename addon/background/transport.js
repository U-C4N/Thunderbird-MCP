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
   * Waiting is the normal resting state: no daemon has started yet, or one that was
   * working has gone. The check is a local file stat, so polling it briskly costs
   * nothing — and backing off here is what made a freshly started daemon wait up to
   * half a minute before anything worked.
   *
   * A failed connection is different: something is listening and not completing the
   * handshake, and hammering it helps nobody. */
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

  /** Connections in a row that reached the daemon and got no `welcome`. */
  const HANDSHAKE_ALARM_AFTER = 3;

  /** Floor on how often the state is pushed out to whoever asked to hear about it. */
  const NOTIFY_THROTTLE_MS = 10000;

  /* A read that never settles used to be terminal: nothing clears `connecting`, so
   * no later attempt can start, and no retry was ever armed to try again. The
   * privileged half is exactly the part that wedges, so the read gets a deadline. */
  const PAIRING_TIMEOUT_MS = 5000;

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
  let connecting = false; // a connect() is between reading the pairing and its socket
  let lastPairing = null; // {port, token} of the daemon the schedules apply to
  let consecutiveFailures = 0; // attempts in a row that ended without a `welcome`
  let lastCloseCode = null;
  let lastCloseAt = null;
  let lastWelcomeAt = null;
  let onStateChange = null;
  let lastNotifyAt = 0;
  let pairingTimer = null;
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

  /**
   * Everything `tbmcp doctor` needs when the bridge itself is what is broken.
   *
   * It ends up in <profile>/tbmcp-addon-status.json, which is the only channel
   * left when no connection is working.
   */
  function status() {
    return {
      state: state(),
      // Separate from `state`, which is about the socket: this one says an attempt
      // is waiting on the privileged half, which is where a wedge shows up first.
      connecting,
      attempt,
      inFlight: inFlight.size,
      consecutiveFailures,
      lastCloseCode,
      lastCloseAt,
      lastWelcomeAt,
      port: currentPort,
    };
  }

  /**
   * @param {boolean} force  a transition worth reporting whatever the last one
   *   cost — coming up is always news, a failure in a burst of them is not.
   */
  function notify(force) {
    if (!onStateChange || stopped) {
      return;
    }
    const at = Date.now();
    if (!force) {
      if (at - lastNotifyAt < NOTIFY_THROTTLE_MS) {
        return;
      }
      lastNotifyAt = at;
    }
    try {
      onStateChange(status());
    } catch (ex) {
      // Reporting our own state must not be able to break the connection.
      tbxLog.debug("state listener failed:", ex.message || ex);
    }
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

  /**
   * `readPairing()` with a deadline, so an attempt always ends.
   *
   * A late answer is dropped: this promise has already rejected, the attempt that
   * was waiting on it has gone, and a retry is on its way — resolving now would
   * open a second socket behind the live one.
   */
  function readPairingWithin(ms) {
    return new Promise((resolve, reject) => {
      clearTimeout(pairingTimer);
      pairingTimer = setTimeout(() => {
        tbxLog.error(
          `readBridgeFile did not answer within ${ms}ms — the privileged half of the ` +
            "add-on is not responding; `tbmcp doctor` shows the daemon's view"
        );
        reject(new Error(`readBridgeFile did not answer within ${ms}ms`));
      }, ms);
      readPairing().then(
        (pairing) => {
          clearTimeout(pairingTimer);
          resolve(pairing);
        },
        (ex) => {
          clearTimeout(pairingTimer);
          reject(ex);
        }
      );
    });
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
        consecutiveFailures = 0;
        lastWelcomeAt = new Date().toISOString();
        notify(true);
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
    retryTimer = setTimeout(() => {
      // Cleared before the attempt, not after it: `retryTimer` is what tells the
      // supervisor whether the chain still has a link left to follow.
      retryTimer = null;
      connect();
    }, delay);
  }

  /**
   * One attempt at a time.
   *
   * `attemptConnection` awaits the pairing read before it has a socket to show for
   * itself, and both the supervisor and the retry timer can call in during that
   * window — which is how two sockets to the same daemon came about, one of them
   * invisible to everything that inspects `socket`.
   */
  async function connect() {
    if (stopped || connecting || (socket && socket.readyState <= WebSocket.OPEN)) {
      return;
    }
    connecting = true;
    try {
      await attemptConnection();
    } finally {
      connecting = false;
    }
  }

  async function attemptConnection() {
    let pairing;
    try {
      pairing = await readPairingWithin(PAIRING_TIMEOUT_MS);
    } catch (ex) {
      const message = String(ex.message || ex);
      // No pairing file is the resting state, not a failure: poll, do not back off.
      scheduleRetry(message, message.includes("no pairing file") ? "waiting" : "failed");
      return;
    }

    // A daemon we have not tried before starts with a clean slate: after a long
    // stretch with no daemon the counters sit at their ceiling, and a freshly
    // written pairing file would otherwise not be acted on for half a minute — or
    // would be blamed, on its first close, for the previous daemon's failures.
    //
    // The same file we have been failing against gets no such reprieve: it is the
    // one that used to leave us reconnecting every half second for as long as
    // Thunderbird stayed up.
    if (
      !lastPairing ||
      lastPairing.port !== pairing.port ||
      lastPairing.token !== pairing.token
    ) {
      waits = 0;
      attempt = 0;
      consecutiveFailures = 0;
    }
    lastPairing = { port: pairing.port, token: pairing.token };

    const url = `ws://127.0.0.1:${pairing.port}/tbmcp`;
    tbxLog.debug(`connecting to ${url}`);
    currentPort = pairing.port;
    try {
      socket = new WebSocket(url);
    } catch (ex) {
      socket = null;
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
      if (socket !== ws) {
        // A socket we have already moved on from. Acting on this would null the
        // live one and leave nothing to notice.
        return;
      }
      clearTimeout(helloTimer);
      clearTimeout(connectTimer);
      const hadWelcome = welcomed;
      welcomed = false;
      socket = null;
      // currentPort survives the close on purpose: a status read after a failure
      // is exactly when the port we were failing against matters.
      for (const ctx of inFlight.values()) {
        ctx.cancelled = true;
      }
      inFlight.clear();
      const code = event ? event.code : 1006;
      const reason = (event && event.reason) || "";
      lastCloseCode = code;
      lastCloseAt = new Date().toISOString();
      // Reported at info, not debug: a connection that keeps dropping is the one
      // thing a user needs to be able to see without turning on verbose logging.
      tbxLog.info(`socket closed (code ${code}: ${reason})`);

      // Only an attempt that never reached `welcome` counts: a daemon that worked
      // and then went away is not this end failing, and counting it would make the
      // complaint below claim a handshake failed when one had just succeeded.
      if (!hadWelcome) {
        consecutiveFailures += 1;
        if (consecutiveFailures === HANDSHAKE_ALARM_AFTER) {
          // Once per run of failures. Saying it every time would bury it.
          tbxLog.error(
            `the daemon on port ${pairing.port} accepted ${consecutiveFailures} connections ` +
              "but none completed the handshake — restart Thunderbird if this persists; " +
              "`tbmcp doctor` shows the daemon's view"
          );
        }
      }
      notify(false);

      // A rejected token (4001) nearly always means the daemon we paired with has been
      // replaced and the file we read is stale — not that the install is broken. The
      // next attempt re-reads the file, and a file that has changed resets the
      // counters, so a replacement daemon is reached quickly; only a run of
      // rejections against the file we keep reading is worth complaining about.
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

      // A protocol mismatch will not fix itself, so say so rather than retrying
      // quietly for the rest of the session.
      if (code === 4002) {
        tbxLog.error(
          `daemon rejected the connection (code ${code}: ${event && event.reason}). ` +
            "The add-on and the server disagree on the protocol — reinstall with " +
            "`tbmcp install-addon`."
        );
      }
      /* Which schedule applies is decided by how far this socket got, not by the
       * code it closed with:
       *
       * - never welcomed: something is listening and not talking to us, whatever it
       *   says on the way out, so back off (BACKOFF_MS) instead of dialling it every
       *   half second for as long as Thunderbird is up;
       * - welcomed, then closed: the daemon went away — it exited on its idle timer
       *   (1000/1001), was superseded (1012), or died with the socket (1006) — and
       *   the next one may be seconds away, so poll (WAIT_MS); the check is a local
       *   file stat and costs nothing.
       *
       * Either way, a pairing file that has changed resets both counters, so a new
       * daemon never waits out the previous one's backoff (see attemptConnection). */
      scheduleRetry(`closed ${code}`, hadWelcome ? "waiting" : "failed");
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
   * 2. The retry chain ends up with no link left to follow. The watchdogs above make
   *    that all but impossible now — a socket that stalls at either half of the
   *    handshake closes itself, and a close always arms a retry — so this is a
   *    safety net, and it must behave like one: a retry that is already armed owns
   *    the schedule, and taking it over turned every backoff longer than the tick
   *    into a ten-second one.
   */
  async function supervise() {
    if (stopped) {
      return;
    }
    if (!socket) {
      if (!retryTimer) {
        connect();
      }
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
     * @param {{onStateChange: (status: object) => void}} [options]  told when the
     *   bridge comes up and when a connection fails.
     */
    start(seed, options) {
      stopped = false;
      identity = {
        app: (seed && seed.app) || null,
        capabilities: (seed && seed.capabilities) || null,
      };
      onStateChange = (options && options.onStateChange) || null;
      connect();
      clearInterval(supervisor);
      supervisor = setInterval(supervise, SUPERVISE_MS);
    },
    stop() {
      stopped = true;
      clearTimeout(retryTimer);
      retryTimer = null;
      clearTimeout(pairingTimer);
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
    status,
  };
})();

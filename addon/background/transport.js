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

  let socket = null;
  let attempt = 0;
  let waits = 0;
  let supervisor = null;
  let currentPort = null;
  let rejections = 0;
  let retryTimer = null;
  let stopped = false;
  const inFlight = new Map(); // request id -> AbortController-ish flag

  function state() {
    if (!socket) {
      return "disconnected";
    }
    return socket.readyState === WebSocket.OPEN ? "connected" : "connecting";
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
        attempt = 0;
        waits = 0;
        rejections = 0;
        break;
      default:
        break;
    }
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

    socket.onopen = async () => {
      const hello = {
        t: "hello",
        token: pairing.token,
        protocol: PROTOCOL,
        addonVersion: browser.runtime.getManifest().version,
        app: await tbxCapabilities.appInfo(),
        capabilities: await tbxCapabilities.describe(),
      };
      send(hello);
    };
    socket.onmessage = onMessage;
    socket.onerror = () => {
      // onclose always follows; retry is scheduled there.
      tbxLog.debug("socket error");
    };
    socket.onclose = (event) => {
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
      scheduleRetry(`closed ${code}`, code === 4002 ? "failed" : "waiting");
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
    start() {
      stopped = false;
      connect();
      clearInterval(supervisor);
      supervisor = setInterval(supervise, SUPERVISE_MS);
    },
    stop() {
      stopped = true;
      clearTimeout(retryTimer);
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

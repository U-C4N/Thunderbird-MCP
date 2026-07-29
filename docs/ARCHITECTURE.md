# Architecture

## Why this design

Thunderbird has no external API. Anything that wants to drive it must run *inside*
Thunderbird. The only questions are how privileged that inside code can be, and how
it talks to the outside.

Every claim below was verified against a live **Thunderbird 153.0** (Windows 11,
build `20260717002111`), not inferred from documentation. See
[`docs/VERIFIED-FINDINGS.md`](VERIFIED-FINDINGS.md) for the raw probe output.

### The privilege question

| Route | Verdict |
| --- | --- |
| Plain MailExtension (WebExtension) API | 250 functions, but **no** access to preferences, account/server config, filters, calendar, junk training, virtual folders, or OpenPGP. |
| WebExtension **Experiment API** (`experiment_apis`) | Runs in a sandbox with the **system principal** — full XPCOM. This is the escape hatch. |
| Marionette (`-marionette`) | Also full chrome privileges, but needs Thunderbird relaunched with a flag. |
| Reading the profile directly from Python | Fine for bulk reads when Thunderbird is closed; unsafe for writes. |

Thunderbird 153 release builds ship `MOZ_REQUIRE_SIGNING=false` and default
`extensions.experiments.enabled=true`, and `ExtensionData.canUseAPIExperiment()`
returns true when either `isPrivileged` **or** `EXPERIMENTS_ENABLED` holds. So an
**unsigned** add-on carrying `experiment_apis` loads with full privileges on a stock
release install. Confirmed at runtime: `REQUIRE_SIGNING: false`,
`EXPERIMENTS_ENABLED: true`, add-on installed with `signedState: 0` and `isActive: true`.

So: the add-on uses the official API where it exists (it is stable and well-typed) and
drops to the experiment API only for what the official API cannot reach.

### The transport question

Thunderbird cannot be given an inbound listener without embedding an HTTP server in a
privileged module (what the JavaScript reference implementation does). Instead we invert
it: **Python listens, the add-on dials out.**

Verified working from a MailExtension background page: `new WebSocket("ws://127.0.0.1:PORT")`
and `fetch("http://127.0.0.1:PORT/...")`. The default MV2 CSP does not restrict
`connect-src`, and loopback is a potentially-trustworthy origin.

The honest cost of this choice: the connection lives in the add-on's background page,
so its lifetime and reconnect behaviour are now ours to manage. A server *inside*
Thunderbird would simply be listening whenever Thunderbird is up. In practice the page
is persistent on 153 and stays connected, but reattaching after the daemon dies
abnormally is still slower than it should be — measured, and left open, in
[`VERIFIED-FINDINGS.md`](VERIFIED-FINDINGS.md).

Benefits over an in-Thunderbird HTTP server:

- No port bound inside Thunderbird, no Windows Firewall listener prompt for Thunderbird.
- Works unchanged under Snap/Flatpak, where reaching *into* the sandbox is the hard part.
- Reconnect is trivial and one-directional; Thunderbird restarts heal themselves.
- No need to bundle Mozilla's MPL-2.0 `httpd.sys.mjs`.

### Process topology

Multiple MCP clients (Claude Code *and* Codex, per the project goal) must share one
Thunderbird. Since Python owns the listener, exactly one process may bind it. Hence a
**broker daemon**:

```
  Claude Code ──stdio──▶ tbmcp serve ─┐
                                      ├─local RPC─▶ tbmcp daemon ◀──WebSocket── Thunderbird add-on
  Codex CLI  ──stdio──▶ tbmcp serve ─┘              (owns the socket,            (background page
                                                     multiplexes requests)        + experiment API)
```

- `tbmcp serve` is the MCP server a client spawns. It is thin: it translates MCP tool
  calls into broker RPCs. It auto-spawns the daemon if none is running.
- `tbmcp daemon` owns the loopback listener and the single add-on connection, and
  multiplexes concurrent requests from every `serve` process.
- If Thunderbird is closed, read-only tools that can be served from the profile on disk
  keep working (`profile` backend); everything else returns a clear "Thunderbird is not
  running" error rather than hanging.

### Pairing and auth

The daemon picks a free port, then writes a connection file into the Thunderbird
profile directory it discovered from `profiles.ini`:

```jsonc
// <profile>/tbmcp-bridge.json   (0600 on POSIX; ACL-restricted on Windows)
{ "version": 1, "port": 51234, "token": "<32 random bytes, base64url>", "pid": 4242 }
```

The add-on reads that file with privileged file I/O, connects, and authenticates with
the token in its first frame. The daemon rejects any connection whose token does not
match, and rejects frames from non-loopback peers. Nothing is ever bound to a
non-loopback interface.

## Repository layout

```
addon/                      the Thunderbird add-on (built into an XPI)
  manifest.json
  background/               non-privileged half: transport + dispatch
  experiment/               privileged half: one module per capability area
src/tbmcp/
  cli.py                    tbmcp serve | daemon | doctor | install-addon | setup
  daemon.py                 broker: loopback listener + add-on session
  bridge.py                 RPC envelope, timeouts, cancellation
  server.py                 MCP server construction, toolset gating
  tools/                    one module per toolset
  profile.py                profiles.ini, prefs.js, sqlite readers (Thunderbird-closed path)
docs/
tests/
```

## Wire protocol

See [`docs/PROTOCOL.md`](PROTOCOL.md).

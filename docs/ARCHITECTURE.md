# Architecture

## Why this design

Thunderbird has no external API. Anything that wants to drive it must run *inside*
Thunderbird. The only questions are how privileged that inside code can be, and how
it talks to the outside.

The original transport and privilege probes used Thunderbird **153.0** on Windows
11 (build `20260717002111`), followed by **155.0** for 1.3.0. The
[1.4.0 validation record](VALIDATION-1.4.md) covers isolated profiles on
128.0, 128.4.3, 128.14, 153.4 and 159, including the guarded native editor on
128.0. These checks complement the Python/JavaScript contract tests; they do
not cover every account provider. See [the original probes](VERIFIED-FINDINGS.md).

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
is persistent on 153–155 and stays connected. Reattaching after the daemon dies
abnormally used to be slow — measured in
[`VERIFIED-FINDINGS.md`](VERIFIED-FINDINGS.md); since 1.3.0 the add-on's transport
watchdogs reconnect within seconds, and the daemon records every attempt.

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
  multiplexes concurrent reads from every `serve` process. Mutations share one
  write lock, including older direct-write tools and the 1.4.0 operation worker.
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

### Operations, identity and recovery

The daemon owns durable jobs because it survives MCP client reconnects and is
shared by all clients. `operations.py` keeps a profile-scoped SQLite journal at
`<state-dir>/profiles/<profile-hash>/operations.sqlite`. The platform state directory
comes from `ipc.state_dir()`; `TBMCP_STATE_DIR` overrides its base. A canonicalized
profile path separates histories. POSIX directories/files use private modes and
Windows uses a restricted ACL; inability to establish the required privacy fails
closed.

The journal stores frozen selections, message references, content/request hashes,
field changes and an allowlist of result metadata. It excludes mail bodies,
subjects, sender/recipient lists and attachment payloads. Export paths and filenames
are retained as operation metadata. Compose request content lives in worker memory
only. Plans can be applied for 15 minutes; completed history and reference metadata
remain for seven days. Retention is a local history policy, not a backup of mail.

Applying one plan twice returns the same operation. Draft/send request keys bind to
the exact request hash; a changed payload cannot reuse the key. The worker commits
each item as running before dispatch. The add-on's `operations.js` maintains a
second, runtime-local receipt cache so reconnecting the socket cannot execute a
keyed step twice. On daemon restart, running items become unknown and queued items
become interrupted. They are not automatically resumed: a missing receipt says
nothing about whether the original mutation occurred.

A persistent write barrier handles the interval between dispatch and a reliable
completion receipt. Reads remain available while writes are blocked. A normal late
response or same-runtime receipt can resolve transport uncertainty. Native work can
outlive an add-on reload, so a native timeout or explicitly unknown native result
requires evidence of a newer Thunderbird process, not merely a new socket/runtime
epoch. Status reconciliation never sends the original mutation again. Cancellation
stops unstarted work; completed items remain changed and dispatched sends cannot be
recalled.

Thunderbird's integer message ids are session-local. `references.js` combines the
account, expected concrete folder, RFC Message-ID and a hash of immutable header
metadata, retaining the runtime/id only as a hint. Resolution requires a unique
match in that expected folder. It does not broaden into a mailbox-wide scan after a
folder rename or move. Each successful move captures a fresh destination reference.

### Feature boundaries in 1.4.0

| Feature | Ownership and limit |
| --- | --- |
| Saved drafts | The background handler reads MIME and a revision bracket; the experiment opens Thunderbird's native draft editor and owns its temporary edit guards. Body text is limited to 200,000 characters and raw input to 64 MiB. Protected, ambiguous or unsupported drafts are read-only. |
| Batch apply/undo | Preview freezes at most 500 unique messages for mark/tag or same-account move. Undo touches only verified successes and checks that the fields it would restore still match the recorded post-change state. |
| Search/conversation | The background snapshot store wraps Gloda; legacy `x.gloda.*` remains available for older clients. At most 1,000 rows per snapshot, 16 snapshots and a 32 MiB serialized-content budget, with a five-minute expiry. |
| Reply follow-ups | A bounded read examines at most 500 outgoing messages / 100 indexed conversations, with four concurrent conversations and a 60-second budget. It returns evidence-backed candidates, never sends a follow-up. |
| Attachment export | Preview freezes up to 100 messages / 200 attachments, with 25 MiB per file and 250 MiB total. A read-only preparation phase persists a hash before the atomic write. |

Draft reads bracket MIME/header/attachment extraction with the same raw-content
revision, excluding mutable Mozilla status/tag headers. Edits and sends revalidate
that revision and native editor state before committing. The workflow preserves the
saved identity and body format, refuses another open editor, and reports a recovery
window when native completion is uncertain. It does not construct a replacement
draft from an incomplete preview.

Batch move correlation subscribes to the add-on's full normalized MessageList event
stream before moving one message. It is independent of the compact public event
ring. Mark/undo comparisons cover only fields touched by the operation; unrelated
changes are preserved. Missing or ambiguous references become conflicts rather than
guessed replacements.

Follow-up classification uses configured own identities and RFC reply headers. It
excludes known unsent Drafts/Outbox/Templates, self-only mail and automated messages;
archived sent mail remains eligible. Mailing-list exclusion uses headers inspected
for outgoing messages and subsequent replies, not an unbounded scan of every
historical message. Missing indexing, unlinked replies, read failures or scan limits
are reported as uncertainty/partial coverage. The default window is 30 days and the
waiting threshold seven days; snapshots stabilize pagination, not classification
outside the observed window.

Attachment names are sanitized and collisions receive deterministic suffixes at
preview time. The plan does not reserve filesystem paths. At apply time, an occupied
frozen target is a conflict, even if its bytes happen to match. `files.js` writes a
unique temporary file in the destination directory, flushes it, then atomically
moves it with no overwrite. Failure cleanup removes only the helper's own temporary
file. After a known uncertain dispatch, read-only reconciliation may recognize a
target matching the previously persisted size and SHA-256; it does not retry the
write or delete an exported file as undo.

### Read continuity and compatibility

Two cursor families have different effects. Thunderbird message-list cursors consume
their underlying list and cannot safely be replayed after a lost response. Search,
conversation and follow-up snapshot cursors are immutable, runtime-bound and safe to
read again. Their bounded store may evict old snapshots; callers then repeat the
original query rather than receiving a silently different page.

Gloda coverage is always identified as indexed mail. Results expose truncation and
unmatchable terms instead of claiming a complete mailbox search. Alternative literal
subject searches are suggestions with an explicit change in meaning, never hidden
fallback scans. A 1.4.0 server with a 1.3 add-on can still perform non-cursor global
search/conversation calls through legacy `x.gloda.*`, with explicitly non-snapshot
pagination. New workflows and unsupported snapshot continuation fail with a typed
capability error. Upgrading one half does not imply the other half has upgraded.

The daemon event ring is also non-consuming. Each client continues from the last
event it received, not the ring's newest sequence. Session-bound cursors detect a
daemon restart; exact overwritten sequence gaps and unknown disconnect losses are
reported separately. Events are observations that can require mailbox resync, not a
durable change log or a source of truth for undo.

Toolsets control public MCP discovery, not which add-on modules load. The `mail`
toolset includes batch, follow-up and attachment tools; `compose` includes saved
draft tools. Both receive operation status/history/cancel tools even without the
`admin` toolset. Read-only mode omits mutations and cancellation while retaining
read/preview/history tools. Confirmation remains at the public mutation tool layer;
a successful preview does not itself authorize apply.

## Repository layout

```
addon/                      the Thunderbird add-on (built into an XPI)
  manifest.json
  background/               non-privileged half: transport + dispatch
    operations.js           add-on epoch and keyed runtime receipts
    references.js           conservative message identity and resolution
    snapshots.js            bounded immutable read pages
    events.js               public notifications and internal normalized subscribers
    handlers/               official-API workflows, including drafts/batch/export
  experiment/               privileged half: one module per capability area
src/tbmcp/
  cli.py                    tbmcp serve | daemon | doctor | install-addon | setup
  daemon.py                 broker: loopback listener + add-on session
  bridge.py                 RPC envelope, timeouts, cancellation
  methods.py                read effects and narrower transport replay rules
  operations.py             durable journal, worker, cancellation and reconciliation
  server.py                 MCP server construction, toolset gating
  tools/                    one module per toolset
  profile.py                profiles.ini, prefs.js, sqlite readers (Thunderbird-closed path)
docs/
tests/
```

## Testing

Three layers, because no single one can see the whole chain:

- **pytest** (`tests/`) runs the Python side end to end through an in-memory MCP
  client, with the bridge replaced by a recorder (`tests/conftest.py`). It proves
  argument validation, gating, the shape of every result and the daemon's own
  protocol, and needs no Thunderbird — which is also its limit: it cannot see the
  add-on, and every 1.2.0 search defect lived there.
- **node** (`tests/js`, `node --test "tests/js/*.test.mjs"`) runs the add-on's
  real background and privileged scripts under `node:vm` against fakes of the
  WebExtension and XPCOM surfaces (`tests/js/harness.mjs`). The privileged half is
  spliced exactly as `build_xpi.py` splices it, so the sandbox rules — no DOM
  timers, a separate realm, errors that must be `ExtensionError`s — are exercised.
- **live** (`tools/smoke_live.py`, `tools/smoke_search.py`) drives a running
  Thunderbird read-only and is the only layer that sees Thunderbird's actual
  behaviour. Mutating draft validation uses isolated, synthetic mail in a separate
  profile. Live checks are not in CI; run the applicable checks before a release.

The operation tests exercise crash recovery, key reuse, cancellation, late receipts,
privacy and write barriers. JavaScript tests cover MIME/revision races, event-list
normalization, snapshot expiry, reference conflicts and atomic export collisions.
`test_v140_integration.py` covers mixed-version reads and the tools available under
custom/read-only configurations. These contract tests do not substitute for a live
provider-specific IMAP/SMTP failure test. `tools/check_consistency.py` and the XPI
build check that Python routes, registered handlers, privileged modules and manifest
load order remain aligned.

## Wire protocol

See [`docs/PROTOCOL.md`](PROTOCOL.md).

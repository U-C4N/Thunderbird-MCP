# Bridge protocol

One WebSocket connection carries JSON text frames between the Python daemon
(server side) and the Thunderbird add-on (client side). Frames are UTF-8 JSON
objects, one message per frame. Field names are stable; unknown fields must be
ignored so either side can be upgraded independently.

## Envelope

Every frame has a `t` (type) discriminator.

### `hello` — add-on → daemon, first frame

```json
{
  "t": "hello",
  "token": "<from tbmcp-bridge.json>",
  "protocol": 1,
  "addonVersion": "1.4.0",
  "runtimeEpoch": "<random id for this add-on load>",
  "app": { "name": "Thunderbird", "version": "159.0", "processStartedAt": 1793400000000 },
  "capabilities": {
    "experiment": true,
    "namespaces": ["accounts", "messages", "compose"],
    "methods": ["messages.query", "search.global", "compose.draftGet", "operations.receipt"],
    "features": { "operationsV1": true, "guardedMailV1": true, "draftRead": true }
  }
}
```

The daemon replies `{"t":"welcome","protocol":1,"server":"tbmcp"}` or closes the
socket. Close codes, from either side:

| Code | Sent by | Meaning |
|---|---|---|
| 1012 | daemon | superseded — a newer add-on connection replaced this one |
| 4000 | add-on | watchdog — no `welcome` within 8 s of `hello`, or the socket never left CONNECTING within 15 s |
| 4001 | daemon | bad token — the pairing file the add-on read is stale |
| 4002 | daemon | no `hello` within 10 s, a malformed `hello`, or a protocol mismatch |
| 4003 | daemon | non-loopback peer |

The daemon records the outcome of every add-on connection attempt; `tb_status`,
`tb_diagnostics` and `tbmcp doctor` report the recent failures and what they mean.

Protocol version remains **1**. The capability lists above are abbreviated. New
features are negotiated from advertised methods and the runtime epoch, rather than
assuming support from a version string. `runtimeEpoch` survives socket reconnects
but changes when the add-on reloads. `app.processStartedAt` (Unix milliseconds) and
`processId` are optional native process evidence; they may be absent from the first
hello while the app-info probe is still warming up. A new epoch alone does not prove
that native work from the previous add-on load has stopped.

### `req` — daemon → add-on

```json
{ "t": "req", "id": 17, "method": "prefs.get", "params": { "name": "mail.pane_config.dynamic" }, "deadlineMs": 30000 }
```

`id` is a monotonic integer per session. `method` is `"<module>.<action>"`.

### `res` — add-on → daemon

```json
{ "t": "res", "id": 17, "ok": true, "result": { "value": 2, "type": "int" } }
```

Failure:

```json
{ "t": "res", "id": 17, "ok": false,
  "error": { "code": "NS_ERROR_UNEXPECTED", "message": "…", "kind": "thunderbird", "retryable": false } }
```

`kind` is one of `thunderbird` (an XPCOM/API failure), `usage` (bad arguments — the
model should fix the call), `unsupported` (this Thunderbird build cannot do it),
`blocked` (a safety rule refused; `error.needs` lists what would unblock it), or
`internal`. The daemon/control connection also uses `transport` for connection and
deadline failures. `code` is machine-readable; callers must not parse message text.

Inside the add-on, an error crossing from the privileged half to the background page
travels as an `ExtensionError` whose message is `tbxerr:` followed by that same JSON
object (`kind`, `message`, `code`, `needs`): Thunderbird keeps the message of an
`ExtensionError` and discards the rest, so the message is the only channel. The
background page unpacks it (`tbxError.fromWire`) before the frame above is built;
nothing tagged `tbxerr:` ever reaches the daemon.

#### Cursors

`messages.query` and `messages.list` return a `cursor` when more remains. It is
either a raw Thunderbird message-list id (the page boundary coincided with `limit`)
or `tbx:<load>:<n>`, minted by the add-on when `limit` stopped mid-page: the unread
remainder of that page is parked behind it, and at most 32 such tails are kept —
the oldest is evicted and its underlying list aborted. Resuming a cursor from an
earlier page load, or an evicted one, is a `usage` error rather than a different
page.

`search.global`, `search.conversation` and `messages.followups` instead return
immutable, non-consuming snapshot pages: `items`, `count`, `snapshotAt`, `hasMore`
and, when more remains, `nextCursor`. Continue with `cursor` and `limit` only. The
same cursor produces the same page. These cursors are opaque and bound to the
method and add-on runtime; expiry, eviction or reload returns `CURSOR_EXPIRED`.
Snapshots expire after five minutes, hold at most 1,000 rows each, and share a
16-snapshot / 32 MiB serialized-content accounting budget. Oldest snapshots are
evicted when either shared limit is reached.

Search results identify `engine: "global_index"` and `coverage: "indexed_only"`.
`partialReasons` distinguishes a retrieval limit, disabled indexing and terms the
index cannot match. A truncated global retrieval has no `totalAvailable` corpus
claim. `alternativeSearch` is only a suggestion with `semanticsChanged: true`; no
folder scan runs automatically. With an older add-on, the Python search tools can
use the existing `x.gloda.*` methods for requests without a cursor, explicitly
reporting `snapshotSupported: false` and `pagination: "offset"` or `"limit"`.
Unsupported snapshot continuation and new follow-up reads fail with
`UNSUPPORTED_CAPABILITY` before dispatch.

### `progress` — add-on → daemon, optional, repeatable

```json
{ "t": "progress", "id": 17, "done": 120, "total": 4000, "message": "indexing" }
```

Forwarded to the MCP client as a progress notification when the client supplied a
progress token.

### `cancel` — daemon → add-on

```json
{ "t": "cancel", "id": 17 }
```

The add-on sets the request context's `cancelled` flag; a handler must explicitly
observe it. This is best-effort and is not a native rollback or send recall. The
daemon does not cancel dispatched writes merely because a caller disconnects or
times out. Use the operation cancellation method below to stop work that has not
started.

### `event` — add-on → daemon, unsolicited

```json
{ "t": "event", "name": "messages.onNewMailReceived", "data": { "folderId": "…", "count": 3 } }
```

Events feed a bounded daemon ring (500 entries by default). `tb_events` reads it
without consuming entries, so multiple clients can each maintain their own cursor.
Events are never pushed at an MCP client uninvited. The daemon stamps each entry
with `seq` and `ts`.

`daemon.events` accepts `{cursor, limit}` or the legacy `{since, limit}`; `limit`
is 1–500. Its result contains:

| Field | Meaning |
| --- | --- |
| `events` | This page, oldest first |
| `latestSeq` | Last sequence delivered in this page; safe legacy continuation |
| `newestSeq` | Current end of the ring, which may be beyond this page |
| `earliestAvailableSeq` | Oldest retained event |
| `nextCursor`, `hasMore` | Session-bound continuation and whether another page exists |
| `gap` | `{from, to, count}` for overwritten events, or `null` |
| `resyncRequired` | The consumer needs to re-query current mailbox state |

A cursor from another daemon session returns `EVENT_SESSION_CHANGED`. Numeric
`since` cannot detect every daemon restart; use `nextCursor` for new clients.
Disconnect/reconnect markers report `resyncRequired: true` and `lostCount: null`:
the number of events missed while detached is unknown. A ring gap has an exact
count of overwritten daemon entries, not an estimate of all mailbox changes.

The add-on normalizes complete paged MessageLists before publishing move/copy/delete
notifications. Public message ids are compact previews, with counts and a
`countExact` flag; a failed or bounded-out list read reports incomplete data.
Batch move correlation uses a separate internal subscriber and full normalized
headers, never the public ring or its compact id preview.

### `ping` / `pong`

Either side may send `{"t":"ping","ts":<ms>}`; the peer answers
`{"t":"pong","ts":<same>}`. The daemon pings every 20 s and drops a session that
misses two consecutive pongs.

## Chunking large results

A single `res` above 4 MiB is split. The add-on sends
`{"t":"res","id":17,"ok":true,"chunked":{"seq":0,"last":false},"result":"<base64 slice>"}`
frames with increasing `seq`; the daemon concatenates the slices and JSON-parses the
result once `last` is true. Used for `messages.getRaw` on large messages and
attachment payloads.

## Method namespaces

`method` maps to a handler registered in the add-on. Handlers whose name starts with
`x.` require the experiment API; the add-on reports them as unavailable if the
experiment failed to load, so the daemon can degrade gracefully instead of hanging.

| Prefix | Backed by | Examples |
| --- | --- | --- |
| `accounts.` | official API | `accounts.list`, `accounts.getDefault` |
| `folders.` | official API | `folders.query`, `folders.create`, `folders.markAsRead` |
| `messages.` | official API, with privileged helpers where needed | `messages.query`, `messages.read`, `messages.followups`, `messages.batchStep`, `messages.attachmentsSave` |
| `compose.` | official compose API and native draft helpers | `compose.send`, `compose.draftGet`, `compose.draftUpdate`, `compose.draftSend` |
| `search.` | snapshot wrapper over the experiment index | `search.global`, `search.conversation` |
| `operations.` | add-on runtime receipt cache | `operations.receipt` |
| `contacts.` | official API | `contacts.query`, `contacts.create` |
| `tags.` | official API | `tags.list`, `tags.create` |
| `x.prefs.` | experiment | `x.prefs.get`, `x.prefs.set`, `x.prefs.list` |
| `x.accounts.` | experiment | `x.accounts.serverSettings`, `x.accounts.setServerSetting` |
| `x.smtp.` | experiment | `x.smtp.list`, `x.smtp.create`, `x.smtp.setDefault` |
| `x.identities.` | experiment | `x.identities.getFull`, `x.identities.setSignature` |
| `x.filters.` | experiment | `x.filters.list`, `x.filters.create`, `x.filters.run` |
| `x.calendar.` | experiment | `x.calendar.listCalendars`, `x.calendar.createEvent` |
| `x.junk.` | experiment | `x.junk.getSettings`, `x.junk.train` |
| `x.vfolders.` | experiment | `x.vfolders.list`, `x.vfolders.create` |
| `x.openpgp.` | experiment | `x.openpgp.listKeys` |
| `x.gloda.` | experiment | `x.gloda.search`, `x.gloda.conversation`, `x.gloda.stats` |
| `x.files.` | experiment | `x.files.read`, `x.files.write`, `x.files.stat`, `x.files.prepareNames`, `x.files.hash` |
| `x.admin.` | experiment | `x.admin.appInfo`, `x.admin.restart`, `x.admin.addons` |

`tools/check_consistency.py` compares the three layers — the methods Python calls,
the handlers the add-on registers, and the `PREREGISTERED` forwarding list — and
fails when they disagree. Nothing else notices a mismatch until runtime, and then
only as an error at the wrong layer.

## Daemon ⇄ `serve` RPC

`tbmcp serve` talks to the daemon over an authenticated loopback TCP connection on
all platforms. After `{"t":"auth","id":1,"token":"…"}` / `{"t":"ready","id":1}`,
it uses JSON-lines `req`/`res`/`progress` envelopes. A control request carries
`timeout` in seconds and optional `wantsProgress`; the add-on request carries
`deadlineMs`. The add-on session assigns a separate request id before forwarding,
so concurrent clients never collide.

## Durable operations (1.4.0)

The following are daemon-local RPCs, not add-on method names. Status/history stay
available when Thunderbird is closed; a fresh plan or mutation requires the
corresponding add-on capabilities.

| Method | Parameters / behavior |
| --- | --- |
| `daemon.operations.plan` | `{kind: "batch" | "attachments", params}` freezes items and returns a `planId`, preview and `expiresAt`; no mail/file mutation |
| `daemon.operations.apply` | `{planId}` starts the frozen selection once; repeating it returns the existing operation |
| `daemon.operations.undo` | `{operationId}` starts at most one undo of verified successful batch items |
| `daemon.operations.submit` | `{kind, requestKey?, params}` submits `draft_update`, `draft_send`, `send`, `reply` or `forward` |
| `daemon.operations.status` | `{operationId, cursor?, limit?}` reads receipts; may reconcile an uncertain result without repeating its mutation |
| `daemon.operations.list` | `{cursor?, limit?}` reads this profile's retained history |
| `daemon.operations.cancel` | `{operationId}` requests stopping unstarted steps |
| `daemon.drafts.get` | `{messageId}` or `{draftRef}` returns a reviewed draft snapshot and remembers a durable reference |

Plans expire after 15 minutes. Operation receipts and reference metadata have a
seven-day retention window; in-progress jobs and an unresolved write barrier are
not discarded simply because they are old. `requestKey` binds a submission to a
hash of its kind and parameters. Reusing it with different content returns
`REQUEST_KEY_REUSED`; replay with identical content returns the existing operation,
including when Thunderbird is now unavailable. Saved-draft updates/sends require
an explicit request key at both the public tool and daemon layers. Legacy send/reply/forward calls
may omit it, in which case automatic transport replay is disabled.

`draftRead` is independent of `draftEditNative`. For the latter, `false`
means the native editor is unsupported; an initial `null` means the bounded
native capability probe has not completed. Before admitting a new saved-draft
mutation the daemon resolves `null` via a read-only probe. The native open
path also enforces the guard. Affected Thunderbird builds return
`UNSUPPORTED_CAPABILITY` without opening a window; see [tested versions](VALIDATION-1.4.md).

Status returns `operationId`, `kind`, `status`, `createdAt`, `expiresAt`, per-item
`counts`, paged `items`, `nextCursor`, `hasMore` and `cancelRequested`. Operation
states include `queued`, `running`, `succeeded`, `partial`, `failed`, `cancelled`,
`interrupted` and `unknown`; individual items can also be `conflict` or `skipped`.
`cancelled` describes the remaining work, not rollback of completed items. A send
already dispatched reports `cancellationTooLate: true`. `queued: true` / `sent:
false` means Outbox placement, not delivery. Requested send mode never establishes
success by itself.

All daemon writes share one lock. Before dispatch, the daemon persists a write
barrier and the worker persists the item's running state. Transport loss or a
native timeout leaves the outcome uncertain and blocks later writes. Reads and
explicit recovery remain available. A completed same-runtime receipt can settle
ordinary lost responses; `NATIVE_TIMEOUT` and an explicitly unknown native outcome
require evidence that the Thunderbird process restarted after dispatch. Merely
reconnecting or reloading the add-on does not clear those barriers.

The add-on caches up to 10,000 receipts per runtime, keyed by method,
`operationId` and `itemId`, with a parameter hash. Duplicate keys return the same
pending promise or outcome; changed parameters fail. A full cache refuses new
keyed work rather than evicting replay protection. `operations.receipt` is read-only
and returns `found`, `pending`, `result`/`error`, and `runtimeEpoch`. Neither a
missing receipt nor a daemon restart authorizes replay of an uncertain mutation.

Saved-draft mutations carry a `reference` and `expectedRevision`. Attachment exports
use a read-only `messages.attachmentsPrepare` phase to persist `expectedSha256` and
`expectedBytes` before `messages.attachmentsSave`. The initial save treats any
newly occupied destination as a conflict, even if its bytes match. Only
`messages.attachmentsReconcile`, used after a known uncertain dispatch, may accept
an existing target whose bytes and hash match the durable record.

Transport replay is deliberately narrower than read-only classification. Immutable
snapshot pages can be retried; consuming `messages.query`/`messages.list` cursors
cannot. Keyed apply/undo/cancel and submissions with an explicit request key are
replay-safe. Unkeyed writes are never automatically repeated after connection loss.

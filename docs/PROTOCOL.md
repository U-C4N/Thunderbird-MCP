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
  "addonVersion": "1.0.0",
  "app": { "name": "Thunderbird", "version": "153.0", "buildID": "20260717002111" },
  "capabilities": { "experiment": true, "namespaces": ["accounts", "messages", "..."] }
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
`internal`.

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

Best-effort. The add-on still sends a `res` (usually `ok:false`, `kind:"internal"`,
`code:"CANCELLED"`).

### `event` — add-on → daemon, unsolicited

```json
{ "t": "event", "name": "messages.onNewMailReceived", "data": { "folderId": "…", "count": 3 } }
```

Events feed the daemon's ring buffer, which the `tb_events` tool drains. They are
never pushed at an MCP client uninvited.

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
| `messages.` | official API | `messages.query`, `messages.getFull`, `messages.send` |
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
| `x.files.` | experiment | `x.files.read`, `x.files.write`, `x.files.stat` |
| `x.admin.` | experiment | `x.admin.appInfo`, `x.admin.restart`, `x.admin.addons` |

`tools/check_consistency.py` compares the three layers — the methods Python calls,
the handlers the add-on registers, and the `PREREGISTERED` forwarding list — and
fails when they disagree. Nothing else notices a mismatch until runtime, and then
only as an error at the wrong layer.

## Daemon ⇄ `serve` RPC

`tbmcp serve` talks to the daemon over the same JSON-lines envelope on a loopback
port (Windows) or a Unix socket (POSIX), using only `req`/`res`/`progress`/`cancel`.
The daemon assigns its own `id` space per client and rewrites ids when forwarding, so
concurrent clients never collide.

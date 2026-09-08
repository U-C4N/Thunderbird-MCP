# Changelog

## 1.3.0 — 2026-09-08

Every search tool in 1.2.0 was broken, and the bridge could fail in a way nothing
reported. This release fixes both, adds a test layer that can see the add-on, and
was verified against Thunderbird 155.

### Fixed

- `mail_search` returned nothing, for every filter. The add-on asked
  `messages.query` for a list id, which changes the return value to a bare string,
  and then walked the string.
- `search_global` and `search_conversation` failed with "An unexpected error
  occurred". The privileged half called `setTimeout`, which does not exist in its
  sandbox, so every deadline rejected before the work it guarded began. That also
  left `search_index_status.indexedMessages` at `null` and quietly removed the
  deadlines from the calendar, filter and junk tools. Timers now come from the
  platform's `Timer.sys.mjs`.
- Every global-search hit carried `date: null`: gloda's `Date` objects come from
  another realm, so `instanceof Date` was always false. Threads therefore came back
  unordered. Dates are duck-typed now, and a conversation is oldest-first again.
- A folder-scoped `search_global` matched nothing. A folder id such as
  `account1://INBOX` was mistaken for a URI, and the path was sliced one character
  short. Scoping now compares a hit's own folder id or URI against what the caller
  passed, and an unknown reference is a usage error that names it.
- A term the index cannot tokenise (`2.0`) zeroed the whole query. The result now
  names such terms in `unmatchableTerms` and in the empty-result note; the query
  itself is unchanged.
- Errors raised in the privileged half reached the caller as "An unexpected error
  occurred" with a spurious `[Error]` tag: Thunderbird keeps a message only for an
  `ExtensionError`. Every method of the privileged API now crosses the boundary in
  an envelope the background page unpacks, so `mail_save_attachment`'s
  "already exists, pass overwrite=true" survives the hop as a blocked error.
- Paging skipped most of a folder. When `limit` stopped mid-page the rest of that
  page was lost, because continuing a Thunderbird list yields the *next* page:
  `mail_list(limit=3)` followed by its cursor skipped 97 of 100 messages. The
  unread remainder is now parked against a cursor of the form `tbx:<load>:<n>`, and
  a cursor from an earlier Thunderbird session or an evicted page is refused rather
  than silently resolving elsewhere.
- Error tags doubled (`[NOT_CONNECTED] [NOT_CONNECTED]`) because the daemon relayed
  a rendered message and the bridge rendered it again.
- On `mcp` 2.1 and later every deliberate error message was dropped — the SDK
  replaces an unrecognised exception with "Error executing tool …". Tools now raise
  the SDK's `ToolError`, which both 2.0 and 2.2 pass through. Five tests were red on
  a fresh install before this.
- A wide answer (about 64 KB, a 200-hit search) dropped the bridge: neither end
  of the control connection set asyncio's stream limit, so `readline()` raised past
  the default, and the next call waited out its whole timeout on a dead socket. Both
  ends now use the protocol's ceiling, an oversized frame is a typed `TOO_LARGE`
  error, and a stopped read loop retires its connection so the next call reconnects.
- `mail_search` read `searchedFolders`, `search_global` read `totalMatched` and
  `search_conversation` read `participants` — none of which the add-on wrote.
- A daemon that stood down because a newer one had taken over deleted the newer
  daemon's pairing file and advertisement on its way out.
- `install-addon` could leave Thunderbird closed: a lost Marionette reply raised
  before the restart. It now restarts Thunderbird on every exit path and checks a
  lost reply against the AddonManager before calling it a failure.
- The manifest asked for a `messages.tags` permission Thunderbird 155 does not
  recognise (the real ones, `messagesTags` and `messagesTagsList`, were already
  listed).

### Added

- Handshake telemetry. The daemon records what became of every add-on connection —
  never upgraded, no hello, bad token, protocol mismatch, welcomed, superseded,
  disconnected — and `tb_status`, `tb_diagnostics` and `tbmcp doctor` say so.
  An add-on that keeps connecting without completing the handshake is now reported
  as exactly that, with the remedy, instead of "ask the user to start Thunderbird".
- A daemon log file, `<state dir>/tbmcp/daemon.log` (rotating), since the daemon
  runs detached with its output discarded. `doctor` prints the path.
- `doctor` compares the installed add-on with the one in the package and tells you
  to reinstall when they differ, whether or not the bridge is connected; it also
  shows the add-on's own view of its transport from the startup report.
- The add-on's transport is defensive now: the hello never waits on a capability
  probe, a handshake that gets no welcome within 8 s and a connect that never
  completes within 15 s are closed and retried, a pairing read has a deadline, a
  daemon that keeps rejecting is backed off rather than hammered, every close is
  logged with its code, and the state is written to `tbmcp-addon-status.json`.
- `tb_console` shows the add-on's own `[tbmcp]` lines, which live in the ConsoleAPI
  storage rather than the error console, merged by time.
- A node test layer for the add-on (`tests/js`): the real background and
  privileged scripts run under `node:vm` against fakes of the WebExtension and
  XPCOM surfaces. 111 tests, run in CI.
- `tools/smoke_search.py`, a read-only live acceptance run for the search tools.
- CI covers Python 3.14; Thunderbird 155 is the verified version.

### Changed

- `mail_search` returns `scope` (the folder and account ids the query covered) on a
  first page instead of a never-populated `searchedFolders`.
- `search_global` returns `matched`, `retrieved`, `truncated` and, when present,
  `unmatchableTerms`; `totalAvailable` is only claimed when the whole result set
  was retrieved.
- `search_conversation` returns `participants` and `totalAvailable`.
- Result payloads no longer carry keys with `null` values for fields that were not
  produced.

Reported in #3.

## 1.2.0 — 2026-08-11

One-command bootstrap. See the GitHub release.

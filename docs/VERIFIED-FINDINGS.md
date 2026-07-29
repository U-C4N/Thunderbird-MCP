# Verified findings — Thunderbird 153.0 on Windows 11

Everything here was measured on a live install, not read from documentation.
Reproduce with `probe/` (see [`probe/README.md`](../probe/README.md)).

Environment: Thunderbird `153.0`, buildID `20260717002111`, channel `release`,
`MOZ_BUILD_APP=comm/mail`, Windows 11 Pro 26200, Python 3.14.

## Build constants (from `omni.ja` → `modules/AppConstants.sys.mjs`)

```
NIGHTLY_BUILD: false      RELEASE_OR_BETA: true       MOZ_DEV_EDITION: false
MOZ_REQUIRE_SIGNING: false      MOZ_ALLOW_ADDON_SIDELOAD: false
MOZ_UPDATE_CHANNEL: "release"   MOZ_APP_VERSION: "153.0"
```

`MOZ_REQUIRE_SIGNING: false` is the load-bearing one. Via
`toolkit/mozapps/extensions/AddonSettings.sys.mjs`:

- `REQUIRE_SIGNING` follows `xpinstall.signatures.required`, whose default in
  `greprefs.js` is `false` → **unsigned add-ons install**.
- `EXPERIMENTS_ENABLED` follows `extensions.experiments.enabled`, which
  `defaults/pref/all-thunderbird.js` sets to `true` → **experiment APIs load in
  unsigned add-ons**.

Confirmed at runtime through Marionette:
`{"REQUIRE_SIGNING": false, "EXPERIMENTS_ENABLED": true, "SCOPES_SIDELOAD": 1}`.

`ExtensionData.canUseAPIExperiment()` (`Extension.sys.mjs`) returns true when
`isPrivileged || AddonSettings.EXPERIMENTS_ENABLED`, so `isPrivileged: false` on our
add-on is harmless — `experiment_apis` is still honoured. Verified: parsing our
manifest yielded `{"hasExperimentApis": true, "canUseAPIExperiment": true, "errors": []}`.

## The experiment sandbox has the system principal

`ExtensionCommon.sys.mjs::SchemaAPIManager._createExtGlobal` builds the sandbox with
`Services.scriptSecurityManager.getSystemPrincipal()` and pre-injects `Services`,
`Cc`/`Ci`/`Cu`/`Cr`, `ExtensionAPI`, `XPCOMUtils`, `IOUtils`, `PathUtils`,
`ChromeUtils`, `WebExtensionPolicy`. Scripts are loaded with
`Services.scriptloader.loadSubScript`, so the implementation file is **classic JS**
(not an ES module) and assigns `this.<apiName> = class extends ExtensionAPI {…}`.

## Privileged capability results

| Capability | Result |
| --- | --- |
| `Services.prefs` read | ok — 5713 prefs present |
| `Services.prefs` write + `clearUserPref` | ok — round-tripped and cleared |
| `MailServices.accounts` | ok — 3 accounts (`imap`, `imap`, `none`), 2 identities |
| Outgoing servers | ok — API is **`MailServices.outgoingServer`** (not `.smtp`); 2 servers; `createServer`/`deleteServer`/`findServer`/`getServerByKey`/`getServerByIdentity`/`defaultServer` |
| `server.getFilterList(null)` | ok on all three servers |
| `nsIMsgFilterService` | ok |
| `MailServices.tags` | ok — 5 tags with colours and ordinals |
| `MailServices.ab` | ok — 2 books, both `jsaddrbook://` (`abook.sqlite`, `history.sqlite`) |
| `cal.manager.getCalendars()` | ok — 1 `storage` calendar, default tz `Europe/Istanbul` |
| `MailServices.junk` (bayes) | ok — `userHasClassified: false` |
| `VirtualFolderHelper` | ok — statics `createNewVirtualFolder`, `wrapVirtualFolder` |
| OpenPGP `RNP` | ok — `genKey`, `deleteKey`, `getKeys`, `encryptAndOrSign`, `backupSecretKeys`, … |
| Gloda | ok — `mailnews.database.global.indexer.enabled: true`, `GlodaMsgSearcher` present |
| `nsIServerSocket` / `nsISocketTransportService` | ok (both instantiable, unused by the shipped design) |
| `nsIMsgComposeService` / `nsIMsgSend` | ok |
| `AddonManager` | ok |

### Correct module URLs on 153

```js
resource:///modules/MailServices.sys.mjs            → { MailServices }
resource:///modules/calendar/calUtils.sys.mjs       → { cal }
resource:///modules/CalEvent.sys.mjs                → { CalEvent }     // cal.createEvent is GONE
resource:///modules/CalTodo.sys.mjs                 → { CalTodo }
resource:///modules/VirtualFolderWrapper.sys.mjs    → { VirtualFolderHelper, VirtualFolderWrapper }
resource:///modules/FolderUtils.sys.mjs             → { FolderUtils }
resource:///modules/MailUtils.sys.mjs               → { MailUtils }
resource:///modules/MimeMessage.sys.mjs             → { MimeMessage }
resource:///modules/MessageSend.sys.mjs             → { MessageSend, MessageSendReport }
resource:///modules/MailStringUtils.sys.mjs         → { MailStringUtils }
resource:///modules/MsgIncomingServer.sys.mjs       → { MsgIncomingServer, migrateServerUris }
resource:///modules/gloda/GlodaPublic.sys.mjs       → { Gloda }
resource:///modules/gloda/GlodaMsgSearcher.sys.mjs  → { GlodaMsgSearcher }
resource:///modules/jsmime.sys.mjs                  → { jsmime }
chrome://openpgp/content/modules/RNP.sys.mjs        → { RNP, … }
chrome://openpgp/content/modules/keyRing.sys.mjs    → { EnigmailKeyRing }
```

Does **not** exist on 153: `resource:///modules/MsgUtils.sys.mjs`,
`resource:///modules/FilterEditor.sys.mjs`, `cal.createEvent`.

### Settings surfaces confirmed present

- `incomingServer`: `type`, `port`, `username`, `socketType`, `authMethod`,
  `prettyName`, `biffMinutes`, `doBiff`, `downloadOnBiff`, `loginAtStartUp`,
  `limitOfflineMessageSize`, `maxMessageSize`, `emptyTrashOnExit`,
  `offlineSupportLevel`. **Caveat:** `server.hostName` read back as `null` on this
  build — read the host from `mail.server.<key>.hostname` or `server.serverURI` and
  cross-check.
- `nsIImapIncomingServer`: `useIdle`, `maximumConnectionsNumber`, `forceSelect`,
  `cleanupInboxOnExit`.
- `server.spamSettings`: `level`, `moveOnSpam`, `moveTargetMode`, `purge`,
  `purgeInterval`, `useWhiteList`, `whiteListAbURI`, `manualMark`.
- identity: `fullName`, `email`, `replyTo`, `organization`, `composeHtml`,
  `attachSignature`, `sigBottom`, `htmlSigText`, `htmlSigFormat`, `attachVCard`,
  `smtpServerKey`, `doBcc`, `doBccList`, `draftFolder`, `fccFolder`,
  `archiveFolder`, `stationeryFolder`, `catchAll`.
- The live profile holds 275 `user_pref` entries, dominated by `mail.server.*` (72),
  `mail.identity.*` (53), `mail.smtpserver.*` (14), `mailnews.tags.*` (11),
  `calendar.registry.*` (6), `ldap_2.servers.*` (2).

## Official MailExtension API on 153

Extracted from `omni.ja` → `chrome/messenger/content/messenger/schemas/*.json`:
**250 functions** across 28 namespaces. Notable, and newer than what the JavaScript
reference implementation targets:

- `messages.sendMessage(details, options)` and `messages.saveMessage(details, options)`
  — compose and send **without opening a compose window**. `options.mode` is
  `default | sendNow | sendLater` (send) and `draft | template` (save).

  There is a catch the schema alone does not reveal. `sendMessage` requires the
  `messages.send` permission, and that permission is declared as an
  **`OptionalOnlyPermission`** — Thunderbird refuses it in the manifest's
  `permissions` array and only grants it through `browser.permissions.request()`,
  which needs a user gesture. Measured consequence: with `compose.send` and
  `messages.save` granted and everything else working, `browser.messages.sendMessage`
  was still `undefined` (`headlessSend: false` in the add-on's own startup report).
  `messages.saveMessage` needs only `messages.save`, an ordinary optional permission,
  so drafts and templates work from the manifest alone.

  A bridge with no UI has no gesture to offer, so the privileged half grants the
  permission to itself via `ExtensionPermissions.add(id, …, extension)` — passing the
  extension makes it emit `add-permissions`, which updates the live policy with no
  restart. This adds no authority the add-on was not installed for (`compose.send` is
  already in the manifest) and every send still passes the confirmation gate; it only
  removes the window. See `grantOptionalPermission` in `addon/experiment/core.js`.
- `messages.query` filters: `accountId`, `attachment`, `author`, `body`, `flagged`,
  `folderId`, `fromDate`, `fromMe`, **`fullText`** (Gloda-backed), `headerMessageId`,
  `includeSubFolders`, `online`, `recipients`, `size`, `subject`, `tags`, `toDate`,
  `toMe`, `junk`, `junkScore`, `new`, `unread`, `read`, plus
  `messagesPerPage` / `autoPaginationTimeout` / `returnMessageListId` paging.
- `messages.import`, `messages.listInlineTextParts`, `messages.deleteAttachments`,
  `messages.openAttachment`.
- `folders.query` (17 predicates incl. `isVirtual`, `isTag`, `isUnified`, `isFavorite`),
  `folders.getFolderCapabilities`, `folders.getUnifiedFolder`, `folders.getTagFolder`.
- `identities.create` / `update` / `delete`.
- `messengerSettings` — exists but exposes only four settings
  (`composeInlineSpellCheckEnabled`, `messageLineLengthLimit`,
  `messagePlainTextFlowedOutputEnabled`, `readerDisplayAttachmentsInline`), three of
  them read-only. Not a substitute for preference access.

Live smoke test through the installed add-on: `messages.query({fullText:"invoice"})`
returned a match plus a `messageListId`; `folders.query({isVirtual:true})` worked.

Permissions declared by Thunderbird's schemas: `accountsFolders`, `accountsIdentities`,
`accountsRead`, `addressBooks`, `compose`, `compose.save`, `compose.send`,
`messages.save`, `messagesDelete`, `messagesImport`, `messagesModify`,
`messagesModifyPermanent`, `messagesMove`, `messagesRead`, `messagesTags`,
`messagesTagsList`, `messagesUpdate`, `messengerSettings`, `pkcs11`,
`sensitiveDataUpload`, `sessions`, `tabs` (all optional), plus `menus` and `theme`
(no prompt) and `activeTab` / `menus.overrideContext`.

## Background page lifetime — and a theory that was wrong

Manifest: `manifest_version` accepts **2 or 3**. MV2 with a *persistent* background
page is what this add-on uses.

Thunderbird 153 ships `extensions.eventPages.enabled = true` (`greprefs.js:777`;
`Extension.sys.mjs:104` reads it with `default: true`), and
`toolkit/.../parent/ext-backgroundPage.js` installs its suspend-after-idle timer only
`if (!extension.persistentBackground)`. It looked very much as though that pref would
demote our page to an event page and have it suspended after
`extensions.background.idle.timeout` (30 s), taking the bridge socket with it — which
would have neatly explained the slow reconnects described below.

**It does not.** Asked at runtime, the add-on reports
`persistentBackground: true, eventPagesEnabled: true`: the `persistent` key is
honoured, no idle timer is installed, and the page is not suspended. Recorded here
because the pref makes the opposite look true, and because the wrong conclusion nearly
went into this document as fact.

What survives from that investigation is `browser.tbx.keepAlive()`, which resets the
idle timer via `background-script-reset-idle` (the mechanism an open native-messaging
port uses, Firefox bug 1770696) from an `nsITimer` in privileged code. On 153 it
reports `enabled: false, reason: "the background page is persistent"` and does
nothing. It is kept because it costs nothing, it is the correct remedy if a future
release changes this, and `appInfo()` now reports both flags so the add-on's startup
report says which world it is in.

## Reconnect latency after an abnormal daemon exit — open

Normal operation is solid: with the daemon running, 17 read-only tools and 12
gated-write checks pass against the live Thunderbird every time.

But if the daemon is **force-killed** and its pairing file deleted, the add-on takes
roughly **40-60 s** to reattach, rather than the ~1-2 s the reconnect schedule
intends. Measured repeatedly: 54 s, then 20 s, then 122 s across two attempts.

What was ruled out: the reconnect schedule itself (a clean daemon exit is picked up in
about a second — measured 4.1 s including spawning a replacement); two daemons racing
(only one process ever held the lock and the listening sockets); background-page
suspension (above). The remaining suspects are the WebSocket close not firing promptly
when the peer process is killed rather than closed, and Gecko caching around the
pairing file read.

Mitigations that are in place: `Bridge` waits once for the add-on to attach before its
first Thunderbird-bound call rather than letting the first few fail; a clean daemon
exit uses the fast schedule; and a token rejection re-reads the pairing file instead
of backing off. The remaining case only arises when the daemon dies abnormally.

## Transports (measured from a background page)

| Transport | Result |
| --- | --- |
| `new WebSocket("ws://127.0.0.1:8271/probe")` | **works** — handshake completed, message echoed |
| `fetch("http://127.0.0.1:8271/…", {method:"POST"})` | **works** — 200, body round-tripped (needs the `http://127.0.0.1/*` host permission) |

Independent corroboration: an unrelated add-on already on this profile
(`thunderbird-ai@extension`) repeatedly attempts `ws://127.0.0.1:7701/` and logs
*"The connection was refused"* — a connection-level failure, not a security block.

## Installation without any UI

Marionette ships in release Thunderbird 153 (`chrome/remote/content/marionette/*`).
Launch with **`-marionette -remote-allow-system-access`** — the second flag is now
required or `Marionette:SetContext` fails with *"System access is required"*. Then
`AddonManager.getInstallForFile()` + `install.install()` installs an unsigned XPI
silently: no permission doorhanger appeared, and the result was
`{"ok": true, "signedState": 0, "isActive": true, "appDisabled": false}`.

Two traps found the hard way:

1. **Dropping the XPI into `<profile>\extensions\` does not work.** Modern Gecko no
   longer scans that directory; the file is deleted on the next start.
2. **Never build the XPI with PowerShell's `ZipFile::CreateFromDirectory`.** On .NET
   Framework it writes `\` separators, `nsIZipReader` takes them literally, and the
   install fails with `ERROR_CORRUPT_FILE` (-3) and
   `NS_ERROR_FILE_NOT_FOUND` on `experiment/schema.json`. Build with
   `zipfile` and forward slashes (`probe/build_xpi.py`).
3. Gecko caches jar files by path. Reinstalling a *changed* XPI at the *same* path can
   re-read the stale zip; bump the filename or the version.

## End-to-end result

The finished bridge was installed by `tbmcp install-addon` into the same live
Thunderbird — no clicks — and reported
`bridge@thunderbird-mcp 0.1.0, signedState: 0 (unsigned), isActive: true`. The
add-on then dialled the daemon on its own:

```
tbmcp.daemon: pairing file written to …\tbmcp-bridge.json (port 53222)
tbmcp.daemon: daemon ready (control 53221, add-on 53222)
tbmcp.daemon: add-on connected: Thunderbird 153.0 (experiment=True)
```

`tools/smoke_live.py` then drove 17 read-only tools through a real in-memory MCP
client against that Thunderbird. All passed, across both halves:

| Tool | Result |
| --- | --- |
| `tb_status` / `tb_diagnostics` | connected; Thunderbird 153.0, buildID 20260717002111 |
| `account_list` | 3 accounts |
| `folder_list` | 8 folders |
| `mail_tags` | 5 tags |
| `contact_list` | 50 contacts |
| `identity_list` / `smtp_list` | 2 / 2 |
| `pref_get` | `mail.pane_config.dynamic` = 2 |
| `pref_list` | 11 user-set `mailnews.tags.*` prefs |
| `filter_list` | 0 filters |
| `calendar_list` | 1 calendar |
| `search_index_status` | global indexer enabled |
| `openpgp_list_keys` | 0 keys |
| `junk_get`, `search_saved_list`, `settings_describe` | ok |

One real defect the first run exposed: the add-on took ~54 s to connect after the
daemon appeared. Its reconnect backoff had climbed to the 30 s tier while no daemon
existed, and "no pairing file yet" — a local file check that costs nothing — was
being treated as a connection failure. The two cases now use separate schedules, so
a freshly started daemon is picked up in about a second.

## Traps

Declaring `"events": ["startup"]` in `experiment_apis` requires the API class to
implement `onStartup()` — `ExtensionAPI` has no such base method, and omitting it
throws during bootstrap (`api.onStartup is not a function`), which leaves
`WebExtensionPolicy.getByID()` null and the background page dead. Omit the event
unless you implement the hook.

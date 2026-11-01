[![CI](https://github.com/U-C4N/Thunderbird-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/U-C4N/Thunderbird-MCP/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-1.4.0%20unreleased-0A84FF)](CHANGELOG.md)
[![Thunderbird](https://img.shields.io/badge/Thunderbird-128%2B-0A84FF)](https://www.thunderbird.net/)
[![Python](https://img.shields.io/badge/python-3.11%20%E2%80%93%203.14-blue)](https://www.python.org/)
[![Tools](https://img.shields.io/badge/tools-124-brightgreen)](docs/TOOL-REFERENCE.md)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

# Thunderbird MCP

An MCP server that lets an AI agent drive the Thunderbird already running on your
machine — mail, folders, contacts, calendar, filters and settings — through
Thunderbird's own internals. Not a copy of your mailbox and not an IMAP client: the
same Thunderbird you have open.

Built for **Claude Code** and **Codex CLI**, and able to serve both at once.

## What is new in 1.4.0

- **Continue a saved draft.** Read its current revision, edit the actual draft and
  send the reviewed version. User-owned compose windows remain untouched.
- **Preview, apply and undo a batch.** Freeze a selection before changing flags,
  tags or moving mail within an account. Later user edits become conflicts.
- **Know what a search covered.** Stable conversation/search pages carry coverage
  and truncation information; an unavailable index does not silently change the query.
- **Find possible follow-ups.** Inspect outgoing conversations with no observed
  human reply, with header evidence and explicit limits.
- **Save selected attachments together.** Preview filenames and sizes, then
  export without overwriting existing files.
- **Inspect uncertain work.** Seven-day local operation history and runtime
  receipts replace blind retries after a lost reply. Event pages no longer skip
  buffered notifications.

Thunderbird 128 remains the minimum. Reading a saved draft works on 128.0;
editing or sending it requires **128.4.3 or newer on the 128 ESR branch**, or
**133.0b3 or newer** on the main release line.

This is the unreleased 1.4.0 development version. See the
[workflow guide](docs/WORKFLOWS-1.4.md), [validation results](docs/VALIDATION-1.4.md)
and [changelog](CHANGELOG.md).

## Quick start

```bash
git clone https://github.com/U-C4N/Thunderbird-MCP
cd Thunderbird-MCP
python bootstrap.py --clients claude-code,codex
```

One command builds the environment, installs the add-on (Thunderbird restarts
once), registers the clients you name and verifies the chain. Re-running it is safe.
Agents can parse `python bootstrap.py --json` instead of the human output.

Then ask your agent something — *"what did the accountant say about VAT in June?"*,
*"archive everything from newsletters older than a month"*, *"draft a reply to the
last mail from Ali and let me read it first"*.

Prefer to register a client by hand? `tbmcp setup --print-config all` prints the
exact block for Claude Code, Codex, Claude Desktop, Cursor, VS Code, Gemini CLI and
Zed. Use `python -m tbmcp serve` as the command, with an absolute interpreter path.

## What you can do

| | |
| --- | --- |
| **Find** | ranked full-text search across every account, threads rebuilt across Inbox, Sent and archives, substring filters by folder, sender, date, tag |
| **Triage** | preview and undo marks/tags and same-account moves; archive/copy/delete remain separate tools |
| **Write** | create a draft, continue editing the saved version, then send with a revision check and confirmation |
| **Follow up** | find possible unanswered outgoing conversations with evidence and coverage limits |
| **Collect files** | freeze attachment selections and export with non-overwriting writes and per-file outcomes |
| **Configure** | server ports and security, identities and signatures, SMTP servers, junk handling, ~5,700 preferences — every write reports the old value so it can be undone |
| **Automate** | create, reorder and run message filters; events and tasks on the calendar |
| **Diagnose** | `tb_status`, `tb_diagnostics`, `tb_console` and `tbmcp doctor` say what is wrong and what to do |

## Safety

Reads are unrestricted. Anything that sends, deletes or changes configuration needs
`confirm=true`, carries `destructiveHint`, and prompts for approval on clients that
support elicitation. `mail_send` drafts unless told `mode="send"`; `dry_run_only`
previews a write; preference writes are allowlisted and credentials, proxy and
security prefs are refused at both layers. `--read-only` registers no mutating tool
at all.

## Toolsets

Tools are grouped so an agent only pays context for what it uses. The default set is
`mail`, `folders`, `compose`, `search` and `admin`; add the rest with `--toolsets`.

```bash
tbmcp serve --toolsets all              # everything
tbmcp serve --toolsets +calendar        # the default set plus one
tbmcp tools --toolsets all              # list what would be registered
```

<!-- BEGIN GENERATED TOOL CATALOGUE -->
124 tools across 10 toolsets; 54 of them read-only.
Full signatures in [docs/TOOL-REFERENCE.md](docs/TOOL-REFERENCE.md).

<details>
<summary><b><code>mail</code></b> — 21 tools · on by default — Search, read, triage and file messages, plus attachments and tags.</summary>

| Tool | | What it does |
| --- | --- | --- |
| `mail_search` | read | Search the user's mail. Combine `full_text` with any filters below |
| `mail_list` | read | List messages in one folder, newest first by default |
| `mail_get` | read | Read one message. `text` gives headers plus the plain-text body |
| `mail_get_many` | read | Read up to 50 messages in one round trip — for triaging a search result |
| `mail_get_source` | read | Fetch a message's raw RFC 5322 source, for header forensics |
| `mail_attachments` | read | List a message's attachments with part names, sizes and content types |
| `mail_save_attachment` | write | Write one attachment to a directory on this machine |
| `mail_mark` | write | Set read/flagged/junk state or adjust tags on one or more messages |
| `mail_move` | write | Move messages into another folder |
| `mail_copy` | write | Copy messages into another folder, leaving the originals in place |
| `mail_archive` | write | Archive messages using each account's configured archive layout |
| `mail_delete` | **destructive** | Delete messages. Moves to Trash unless `permanent=true` |
| `mail_tags` | read | List the tags defined in Thunderbird, with keys, labels and colours |
| `mail_tag_upsert` | write | Create a tag, or recolour/rename an existing one |
| `mail_tag_delete` | **destructive** | Remove a tag definition. Messages keep the raw keyword but lose the label |
| `mail_batch_preview` | read | Preview changes to up to 500 messages without changing mail |
| `mail_batch_apply` | write | Apply a preview once and return its operation id for tb_operation_status |
| `mail_batch_undo` | write | Undo confirmed batch changes whose touched fields have not changed again |
| `mail_followups` | read | Find possible follow-ups using outgoing mail and indexed reply headers |
| `mail_attachments_plan` | read | Preview attachment downloads from up to 100 selected messages |
| `mail_attachments_save` | write | Save a frozen attachment plan once, without overwriting existing files |

</details>

<details>
<summary><b><code>folders</code></b> — 15 tools · on by default — The folder tree, its counts, and creating/renaming/emptying folders.</summary>

| Tool | | What it does |
| --- | --- | --- |
| `folder_list` | read | List mail folders with their ids and message counts |
| `folder_get` | read | Get one folder: counts, special use, flags and IMAP quota |
| `folder_capabilities` | read | Report what may be done to a folder before attempting it |
| `folder_get_unified` | read | Get the unified folder that spans every account, e.g. all inboxes at once |
| `folder_create` | write | Create a folder inside another folder, or at the top of an account |
| `folder_rename` | write | Rename a folder, keeping its messages and subfolders |
| `folder_move` | write | Move a folder under a different parent, with its subfolders |
| `folder_copy` | write | Copy a folder and its contents under another parent, leaving the original |
| `folder_delete` | **destructive** | Delete a folder, its subfolders and every message in them |
| `folder_mark_read` | write | Mark every message in a folder as read |
| `folder_set_favorite` | write | Add or remove a folder from the user's favourites |
| `folder_empty_trash` | **destructive** | Permanently delete everything in one account's Trash |
| `folder_empty_junk` | **destructive** | Permanently delete everything in one account's Junk folder |
| `folder_sync_offline` | write | Fetch an IMAP folder's message bodies so they are available offline |
| `folder_compact` | write | Reclaim the disk space left behind by deleted messages |

</details>

<details>
<summary><b><code>compose</code></b> — 9 tools · on by default — Sending, replying, forwarding, drafts and templates.</summary>

| Tool | | What it does |
| --- | --- | --- |
| `mail_send` | **destructive** | Write a message. Saves a reviewable draft unless `mode="send"` |
| `mail_reply` | **destructive** | Reply to a message. Saves a reviewable draft unless `mode="send"` |
| `mail_forward` | **destructive** | Forward a message. Saves a reviewable draft unless `mode="send"` |
| `mail_draft_save` | write | Save a message without sending it, as a draft or a template |
| `mail_draft_get` | read | Read a saved draft and its revision without opening a composer |
| `mail_draft_update` | write | Update the saved draft only if its reviewed revision is still current |
| `mail_draft_send` | **destructive** | Send exactly the saved draft revision the user reviewed, or queue it |
| `mail_compose_open` | write | Open a populated compose window for the user to finish by hand |
| `mail_send_status` | read | List messages sitting in the Outbox, unsent |

</details>

<details>
<summary><b><code>search</code></b> — 7 tools · on by default — Ranked whole-corpus search, conversations, and saved searches.</summary>

| Tool | | What it does |
| --- | --- | --- |
| `search_global` | read | Ranked search of indexed mail, with stable snapshot pagination |
| `search_conversation` | read | Read a conversation oldest first, across indexed folders and accounts |
| `search_index_status` | read | Whether Thunderbird's global index is enabled, and how far along it is |
| `search_saved_list` | read | List the saved searches (virtual folders) and what each one matches |
| `search_saved_create` | write | Create a saved search that appears in the folder pane |
| `search_saved_update` | write | Redefine an existing saved search, by name or uri |
| `search_saved_delete` | **destructive** | Remove a saved search. The messages it listed are not touched |

</details>

<details>
<summary><b><code>contacts</code></b> — 13 tools — Address books, contacts and mailing lists.</summary>

| Tool | | What it does |
| --- | --- | --- |
| `contact_search` | read | Look someone up in the address book |
| `contact_list` | read | List contacts, across every address book unless one is named |
| `contact_get` | read | Read one contact in full, including its raw vCard |
| `contact_create` | write | Add a contact to an address book |
| `contact_update` | write | Change fields on an existing contact |
| `contact_delete` | **destructive** | Delete a contact. There is no Trash for contacts, so this cannot be undone |
| `addressbook_list` | read | List the address books, with how many contacts and lists each holds |
| `addressbook_create` | write | Create an empty local address book |
| `addressbook_delete` | **destructive** | Delete an address book together with all its contacts and mailing lists |
| `mailinglist_list` | read | List address book mailing lists, with member counts |
| `mailinglist_create` | write | Create an empty mailing list in an address book |
| `mailinglist_add_member` | write | Add an existing contact to a mailing list |
| `mailinglist_remove_member` | write | Take a contact off a mailing list. The contact itself is left alone |

</details>

<details>
<summary><b><code>calendar</code></b> — 12 tools — Calendars, events and tasks, including recurring series.</summary>

| Tool | | What it does |
| --- | --- | --- |
| `calendar_list` | read | List the user's calendars, with ids, types and whether each is writable |
| `calendar_create` | write | Create a calendar and register it with Thunderbird |
| `calendar_update` | write | Rename or recolour a calendar, or toggle read-only and disabled |
| `calendar_delete` | **destructive** | Remove a calendar. Deletes its events and tasks with it |
| `event_list` | read | List events in a time window, soonest first |
| `event_get` | read | Read one event or task in full, including attendees and recurrence |
| `event_create` | write | Create an event. Omit `end` for a one-hour meeting |
| `event_update` | write | Change an event. Only the fields you pass are touched |
| `event_delete` | **destructive** | Delete an event or a task. Calendars have no trash, so this is final |
| `task_list` | read | List tasks, soonest due first. Completed ones are hidden by default |
| `task_create` | write | Create a task. Everything but the title is optional |
| `task_update` | write | Change a task, or tick it off with `completed=true` |

</details>

<details>
<summary><b><code>filters</code></b> — 8 tools — Thunderbird's message filters, including running them on demand.</summary>

| Tool | | What it does |
| --- | --- | --- |
| `filter_list` | read | List filters in execution order, with their conditions and actions |
| `filter_get` | read | Read one filter in full, by account and index |
| `filter_create` | write | Create a filter. It is appended, so existing rules keep their order |
| `filter_update` | write | Change a filter in place. Only what you pass is touched |
| `filter_set_enabled` | write | Turn one filter on or off without changing its definition |
| `filter_reorder` | write | Move a filter to a different position in the execution order |
| `filter_delete` | **destructive** | Delete a filter. Thunderbird keeps no history, so the rule is gone |
| `filter_run` | write | Apply filters to folders on demand, as "Run Filters on Folder" does |

</details>

<details>
<summary><b><code>accounts</code></b> — 18 tools — Incoming servers, identities, signatures, SMTP servers, per-account junk.</summary>

| Tool | | What it does |
| --- | --- | --- |
| `account_list` | read | List the mail accounts and how each one is configured |
| `account_get_server` | read | Read one account's incoming server settings |
| `account_set_server` | write | Change one incoming server setting. Getting the connection wrong stops mail |
| `account_get_junk` | read | Read one account's junk-mail handling: level, whitelist, move and purge rules |
| `account_set_junk` | write | Change one junk-mail setting for an account |
| `account_get_folders` | read | Read where an identity files sent mail, drafts, templates and archives |
| `account_set_folders` | write | Change where an identity files sent mail, drafts, templates or archives |
| `account_get_sync` | read | Read an account's offline and synchronisation settings |
| `account_set_sync` | write | Change one offline or synchronisation setting for an account |
| `identity_list` | read | List the sending identities, across every account or just one |
| `identity_get` | read | Read one identity in full: addresses, signature, outgoing server, filing folders |
| `identity_set` | write | Change an identity's addresses and composition defaults |
| `identity_set_signature` | write | Replace an identity's signature text, or point it at a file |
| `smtp_list` | read | List the SMTP servers, and which one is the default |
| `smtp_create` | write | Add an SMTP server. Nothing sends through it until an identity points at it |
| `smtp_update` | write | Change an existing SMTP server. Only the fields you pass are touched |
| `smtp_delete` | **destructive** | Remove an SMTP server. Identities using it will be left unable to send |
| `smtp_set_default` | write | Make one SMTP server the default for identities that have none of their own |

</details>

<details>
<summary><b><code>settings</code></b> — 11 tools — Preferences, junk training and OpenPGP key listing.</summary>

| Tool | | What it does |
| --- | --- | --- |
| `pref_get` | read | Read one Thunderbird preference |
| `pref_get_many` | read | Read up to 100 preferences in one round trip |
| `pref_list` | read | List preferences under a branch, e.g. `prefix="mail.biff."` |
| `pref_user_set` | read | Everything the user has changed from the shipped defaults |
| `settings_describe` | read | Map a human request onto the preference that controls it |
| `pref_set` | write | Change one Thunderbird preference |
| `pref_reset` | write | Clear a user-set preference so the shipped default applies again |
| `junk_get` | read | Read the global junk (bayesian) filter settings |
| `junk_set` | write | Change the global junk filter settings. Only the fields you pass are touched |
| `junk_train` | write | Teach the junk filter that these messages are junk, or are not |
| `openpgp_list_keys` | read | List the OpenPGP keys in Thunderbird's keyring |

</details>

<details>
<summary><b><code>admin</code></b> — 10 tools · on by default — Connection status, events, diagnostics and the error console.</summary>

| Tool | | What it does |
| --- | --- | --- |
| `tb_operation_status` | read | Inspect a queued, completed or uncertain operation without repeating it |
| `tb_operation_list` | read | List this profile's local, body-free operation history for the last seven days |
| `tb_operation_cancel` | write | Stop steps not yet started. A send already dispatched cannot be recalled |
| `tb_status` | read | Whether Thunderbird is attached, and which halves of the add-on loaded |
| `tb_wait` | read | Block until Thunderbird attaches to the bridge, then report status |
| `tb_events` | read | Read buffered Thunderbird notifications: new mail, folder and account changes |
| `tb_diagnostics` | read | One report: versions, profile, which capabilities loaded, accounts, indexing |
| `tb_console` | read | Recent lines from Thunderbird's error console, newest last |
| `tb_addons` | read | List installed add-ons with their enabled and signature state |
| `tb_restart` | **destructive** | Restart Thunderbird. Every call in flight fails, including other clients' |

</details>

<!-- END GENERATED TOOL CATALOGUE -->

## How it works

```
  Claude Code ──stdio──▶ tbmcp serve ─┐
                                      ├─local RPC─▶ tbmcp daemon ◀══WebSocket══ add-on
  Codex CLI  ──stdio──▶ tbmcp serve ─┘              owns the socket,            inside
                                                    multiplexes clients      Thunderbird
```

Thunderbird has no external API, so an add-on runs inside it: the official
MailExtension API for mail and folders, plus a WebExtension Experiment for what that
API cannot reach (preferences, accounts, filters, calendar). Python listens on
loopback and the add-on dials out with a token from `<profile>/tbmcp-bridge.json`, so
nothing is bound inside Thunderbird and nothing leaves the machine. A small daemon
owns the one connection and shares it between clients.

More in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
[docs/PROTOCOL.md](docs/PROTOCOL.md).

## When something is wrong

Run `tbmcp doctor`. It checks every link — Python, the profile, the add-on that is
installed versus the one in this package, the daemon, the handshake from both sides —
and names the broken one with the command that fixes it.

| Symptom | What to do |
| --- | --- |
| "Thunderbird is not connected" | start Thunderbird; if it is running, `tbmcp doctor` says why the add-on has not attached |
| the add-on connects but never completes the handshake | restart Thunderbird; `doctor` shows both views and the daemon log's path |
| `doctor` warns the installed add-on is older than the package | `tbmcp install-addon` |
| settings tools fail, mail tools work | the privileged half did not load → `tbmcp install-addon` |
| full-text search finds nothing | the global indexer is off (Settings → General), or `search_global` names the words the index cannot match |
| a dependency fails to load on Windows | Windows Application Control blocked a wheel; `python bootstrap.py` repairs it |

`TBMCP_DEBUG=1` logs verbosely to stderr; `TBMCP_STATE_DIR` moves the daemon's
files; `tb_console` returns the add-on's `[tbmcp]` lines as a tool.

## Requirements

Thunderbird 128 or newer (verified on 155), Python 3.11–3.14, Windows, macOS or Linux
including Snap and Flatpak Thunderbird.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest                                  # no Thunderbird needed
node --test "tests/js/*.test.mjs"       # real add-on scripts under node:vm
ruff check . && ruff format --check .
python tools/check_consistency.py       # the three layers still agree
python tools/smoke_search.py            # live acceptance, against a running Thunderbird
```

The add-on's real scripts run under `node:vm` against fakes of the WebExtension and
XPCOM surfaces (`tests/js`), which is how the sandbox rules that broke 1.2.0's search
are caught before a release. Live workflow tests additionally use a separate profile
and local-only mail services; see the release gate in the workflow guide. Historical
measurements are in
[docs/VERIFIED-FINDINGS.md](docs/VERIFIED-FINDINGS.md).

## Author

GitHub [@U-C4N](https://github.com/U-C4N) · X [@UEdizaslan](https://x.com/UEdizaslan)

Related: [Autocad-MCP](https://github.com/U-C4N/Autocad-MCP) ·
[U-Pool](https://github.com/U-C4N/U-Pool) ·
[Deuz-SDK](https://github.com/Deuz-AI/Deuz-SDK)

Issues and pull requests are welcome. Report a security problem through GitHub
rather than a public issue.

## Licence

MIT — see [LICENSE](LICENSE). The add-on contains no Mozilla-licensed code.

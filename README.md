[![CI](https://github.com/U-C4N/Thunderbird-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/U-C4N/Thunderbird-MCP/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![MCP SDK](https://img.shields.io/badge/MCP%20SDK-2.0-6E56CF)](https://github.com/modelcontextprotocol/python-sdk)
[![Thunderbird](https://img.shields.io/badge/Thunderbird-128%20%E2%80%93%20155-0A84FF)](https://www.thunderbird.net/)
[![Tools](https://img.shields.io/badge/tools-112-brightgreen)](docs/TOOL-REFERENCE.md)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Stars](https://img.shields.io/github/stars/U-C4N/Thunderbird-MCP?style=flat)](https://github.com/U-C4N/Thunderbird-MCP/stargazers)

# Thunderbird MCP

Thunderbird MCP gives an AI agent real control of the Thunderbird already running on
your machine — your mail, your folders, your contacts, your calendar, your filters,
and your actual settings. Not a copy, not an IMAP re-implementation: the same
Thunderbird you have open, driven through its own internals.

Written in Python, built for **Claude Code** and **Codex CLI**, and able to drive one
Thunderbird from both at the same time.

### What you can ask for

- **Find things you half-remember.** "What did the accountant say about VAT in June?"
  runs a ranked search over the whole indexed corpus and can reconstruct a thread that
  spans Inbox, Sent and an archive folder in one call.
- **Triage a mailbox.** Mark, tag, move, archive and file in bulk — with the source
  folders reported back so a wrong move is reversible.
- **Write mail you get to read first.** `mail_send` produces a reviewable **draft** by
  default; sending is a separate, confirmed step, and needs no compose window.
- **Change settings, properly.** Server ports and connection security, identities and
  signatures, SMTP servers, junk handling, archive layout, message-pane layout,
  ~5,700 preferences — read the current value, write the new one, and get the old one
  back so you can undo it.
- **Automate the boring rules.** Create and reorder message filters, then run them
  over an existing folder to check they do what you meant.
- **Keep a calendar honest.** Events and tasks, with recurring items addressed as a
  series unless you name one occurrence.

> [!NOTE]
> Everything documented here was verified against a live **Thunderbird 155** on
> Windows 11, not inferred from documentation. The measurements, and the traps found
> the hard way, are in [docs/VERIFIED-FINDINGS.md](docs/VERIFIED-FINDINGS.md).

---

## Quick start

There is no PyPI package to install from yet — clone the repo and let it build its
own environment:

```bash
git clone https://github.com/U-C4N/Thunderbird-MCP
cd Thunderbird-MCP
python bootstrap.py
```

One command: it picks an interpreter that works, builds the environment, installs
the add-on, and verifies the whole chain before it returns. Re-running it is safe —
healthy steps are no-ops. Add `--clients claude-code,codex` to also register those
clients in the same run, or do it afterward from "Install into a client" below.

Installing the add-on closes Thunderbird, installs through Thunderbird's own
automation channel, and starts it again — no clicking through the Add-ons UI;
`bootstrap` does this for you as its `addon` step. Prefer to do it by hand, or on its
own? `tbmcp install-addon --manual` builds the package and prints the three clicks.

Already installed? `tbmcp bootstrap` does the same thing.

### For AI agents

```bash
python bootstrap.py --json
```

Emits one object: `ok`, `version`, `launcher`, `steps[]` (each with `name`,
`status`, `seconds`, `detail`), and `next_command` — null on success, otherwise the
single command that addresses the failure. `status` is one of `ok`, `repaired`,
`skipped`, `failed`. Parse this instead of the human output; the columns are not a
stable interface and the JSON is.

A healthy `doctor` looks like this:

```
thunderbird-mcp doctor

Python
  version                    3.14.6
  interpreter                C:\Users\VECTOR\Documents\GitHub\Thunderbird-MCP\.venv\Scripts\python.exe

Thunderbird
  executable                 C:\Program Files\Mozilla Thunderbird\thunderbird.exe
  running                    True
  add-on version (source)    1.3.0
  add-on version (installed) 1.3.0
  profile                    C:\Users\VECTOR\AppData\Roaming\Thunderbird\Profiles\81l4u5ba.default-release
  accounts (from prefs.js)   3
  outgoing servers           2
  global index db            True
  add-on startup report      2026-09-08T16:22:20.621Z
  privileged modules         12 loaded
  bridge methods             128

Bridge
  daemon                     pid 54640
  daemon log                 C:\Users\VECTOR\AppData\Local\tbmcp\daemon.log
  connected                  True
  add-on version (live)      1.3.0
  privileged half            True
  app                        Thunderbird 155.0
  add-on handshakes          1 attempt; last welcomed 0 s ago (Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:155.0) Gecko/20100101 Thunderbird/155.0)
  add-on transport           connected; 0 failed attempts
  tb_status tool call        connected

Tools
  toolsets                   mail,folders,compose,search,admin
  read-only                  False
  send mode                  draft
```

---

## Install into a client

<details>
<summary><b>Claude Code</b></summary>

```bash
tbmcp setup claude-code --toolsets all
```

Or by hand — note the absolute path, because Claude Code spawns without a shell and
has no `cwd` setting:

```bash
claude mcp add-json thunderbird '{
  "type": "stdio",
  "command": "C:\\Users\\you\\Thunderbird-MCP\\.venv\\Scripts\\python.exe",
  "args": ["-m", "tbmcp", "serve", "--toolsets", "all"],
  "env": { "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1" }
}' --scope user
```

`bootstrap` picks this for you and tests it first — write it by hand only if you know
the console script runs on your machine. Where Windows Application Control blocks
pip's console shims (`thunderbird-mcp.exe`), a config that points at it produces a
client that times out with nothing to point at; `python -m tbmcp` always works.

Verify with `claude mcp get thunderbird`, or `/mcp` inside a session.

> [!TIP]
> Run `claude mcp add` from PowerShell or CMD. Git Bash rewrites `/c` into `C:/` and
> corrupts the written config; if you must use it, prefix with `MSYS_NO_PATHCONV=1`.

</details>

<details>
<summary><b>Codex CLI</b></summary>

```bash
tbmcp setup codex --toolsets all
```

Or by hand in `~/.codex/config.toml`:

```toml
[mcp_servers.thunderbird]
command = 'C:\Users\you\Thunderbird-MCP\.venv\Scripts\python.exe'
args = ["-m", "tbmcp", "serve", "--toolsets", "all"]
env = { PYTHONUTF8 = "1", PYTHONUNBUFFERED = "1" }
startup_timeout_sec = 60
tool_timeout_sec = 120

# Codex ignores tool annotations, so safety has to be stated here.
default_tools_approval_mode = "writes"
[mcp_servers.thunderbird.tools.pref_set]
approval_mode = "approve"
[mcp_servers.thunderbird.tools.mail_send]
approval_mode = "approve"
```

`bootstrap` picks this for you and tests it first — write it by hand only if you know
the console script runs on your machine. Windows paths must be **single-quoted** TOML
literals — `"C:\Users\…"` is an invalid escape sequence. Codex also builds the child
environment from scratch, so anything your server needs has to be in `env`. Verify
with `codex mcp get thunderbird --json`.

</details>

<details>
<summary><b>Claude Desktop, Cursor, VS Code, Gemini CLI, Zed</b></summary>

```bash
tbmcp setup claude-desktop cursor vscode gemini zed
tbmcp setup --print-config all      # or just show the blocks and change nothing
```

`setup` prefers each client's own CLI when it is on PATH, falls back to editing the
config file, backs it up first, and reports *added / updated / unchanged* per client.
It never hand-edits `~/.claude.json`, which holds OAuth state and project trust
decisions.

</details>

---

## Toolsets

Tools are grouped so you only pay context for what you use. The default set is lean;
add the rest with `--toolsets`.

```bash
tbmcp serve --toolsets all              # everything
tbmcp serve --toolsets mail,settings    # exactly these
tbmcp serve --toolsets +calendar        # the default set plus one
tbmcp tools --toolsets all              # list what would be registered
```

<!-- BEGIN GENERATED TOOL CATALOGUE -->
112 tools across 10 toolsets; 48 of them read-only.
Full signatures in [docs/TOOL-REFERENCE.md](docs/TOOL-REFERENCE.md).

<details>
<summary><b><code>mail</code></b> — 15 tools · on by default — Search, read, triage and file messages, plus attachments and tags.</summary>

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
<summary><b><code>compose</code></b> — 6 tools · on by default — Sending, replying, forwarding, drafts and templates.</summary>

| Tool | | What it does |
| --- | --- | --- |
| `mail_send` | **destructive** | Write a message. Saves a reviewable draft unless `mode="send"` |
| `mail_reply` | **destructive** | Reply to a message. Saves a reviewable draft unless `mode="send"` |
| `mail_forward` | **destructive** | Forward a message. Saves a reviewable draft unless `mode="send"` |
| `mail_draft_save` | write | Save a message without sending it, as a draft or a template |
| `mail_compose_open` | write | Open a populated compose window for the user to finish by hand |
| `mail_send_status` | read | List messages sitting in the Outbox, unsent |

</details>

<details>
<summary><b><code>search</code></b> — 7 tools · on by default — Ranked whole-corpus search, conversations, and saved searches.</summary>

| Tool | | What it does |
| --- | --- | --- |
| `search_global` | read | Ranked full-corpus search across every indexed folder and account |
| `search_conversation` | read | Every message in one thread, oldest first, across folders and accounts |
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
<summary><b><code>admin</code></b> — 7 tools · on by default — Connection status, events, diagnostics and the error console.</summary>

| Tool | | What it does |
| --- | --- | --- |
| `tb_status` | read | Whether Thunderbird is attached, and which halves of the add-on loaded |
| `tb_wait` | read | Block until Thunderbird attaches to the bridge, then report status |
| `tb_events` | read | Read buffered Thunderbird notifications: new mail, folder and account changes |
| `tb_diagnostics` | read | One report: versions, profile, which capabilities loaded, accounts, indexing |
| `tb_console` | read | Recent lines from Thunderbird's error console, newest last |
| `tb_addons` | read | List installed add-ons with their enabled and signature state |
| `tb_restart` | **destructive** | Restart Thunderbird. Every call in flight fails, including other clients' |

</details>

<!-- END GENERATED TOOL CATALOGUE -->

---

## Safety

Reads are unrestricted. Anything that sends, deletes, or changes configuration is
gated four ways, because no single mechanism exists on every client:

| Layer | Effect | Present on |
| --- | --- | --- |
| `readOnlyHint` / `destructiveHint` annotations | lets the host decide when to ask | hosts that read annotations |
| `anthropic/requiresUserInteraction` | prompts even under `bypassPermissions` | Claude Code |
| an explicit `confirm=true` argument | the call is refused without it | everything, including Codex |
| an approval prompt via elicitation | a real question, and **invisible in the tool schema** so a model cannot fabricate the answer | clients with elicitation |

Beyond the gate:

- **`mail_send` drafts by default.** `--send` or `mode="send"` changes that.
- **`dry_run_only=true`** previews a write — including what it would replace — without
  asking for approval and without touching anything.
- **Every write reports the previous value**, which is what makes an undo possible
  without a transaction log.
- **Preference writes are allowlisted.** `--unsafe-prefs` widens the allowlist, but
  credentials, `network.proxy.*`, `security.*` and the add-on trust model are refused
  outright — at the Python layer *and* again in the privileged module, which is the
  only layer with real privilege.
- **Private keys never move.** OpenPGP keys can be listed and public keys exported;
  asking for secret key material is refused by design.
- **`--read-only`** registers no mutating tools at all, which makes a safe second
  registration easy.
- **`--yolo`** removes every gate. It exists for scripted use. Do not leave it on.

---

## How it works

Thunderbird has no external API, so anything that drives it has to run *inside* it.
The official MailExtension API is large — 250 functions on 153 — but it cannot touch
preferences, account or server configuration, message filters, junk training, virtual
folders, or the calendar. So the add-on pairs that API with a **WebExtension
Experiment API**, which runs with the system principal and therefore has full XPCOM
access. Release Thunderbird builds ship `MOZ_REQUIRE_SIGNING=false` and default
`extensions.experiments.enabled=true`, so the unsigned bridge installs and gets those
privileges on a stock install.

Python listens and the add-on dials out, rather than embedding an HTTP server in
Thunderbird. That needs no port bound inside Thunderbird and no firewall exception,
survives Thunderbird restarts, works unchanged under Snap and Flatpak, and vendors no
MPL-licensed Mozilla code. A small broker daemon owns the single connection, which is
what lets two clients share one Thunderbird.

```
  Claude Code ──stdio──▶ tbmcp serve ─┐
                                      ├─local RPC─▶ tbmcp daemon ◀══WebSocket══ add-on
  Codex CLI  ──stdio──▶ tbmcp serve ─┘              owns the socket,            inside
                                                    multiplexes clients      Thunderbird
```

The daemon picks a free port and writes `<profile>/tbmcp-bridge.json` with a token;
the add-on reads it with privileged file I/O and authenticates on connect. Nothing is
ever bound to a non-loopback interface.

Full detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
[docs/PROTOCOL.md](docs/PROTOCOL.md).

---

## Requirements

- Thunderbird 128 or newer — developed and verified against **155**
- Python 3.11 to 3.14
- Windows, macOS or Linux, including Snap and Flatpak Thunderbird

---

## Troubleshooting

`tbmcp doctor` checks each link in the chain and names the one that is broken. The
add-on also writes `<profile>/tbmcp-addon-status.json` at startup — which privileged
modules loaded, how many bridge methods exist, which capabilities Thunderbird actually
granted — and `doctor` reads it. Its absence, on an add-on that is installed and
active, is itself the diagnosis.

| Symptom | Cause |
| --- | --- |
| "Thunderbird is not connected" | Thunderbird is closed, or the add-on is not installed |
| the add-on connects but never completes the handshake (`tb_status` and `doctor` say so) | restart Thunderbird; `doctor` shows the daemon's view (`add-on handshakes`) and the add-on's (`add-on transport`), and `<state dir>/tbmcp/daemon.log` has the rest |
| `doctor` warns the installed add-on is older than the package | `tbmcp install-addon` and restart Thunderbird |
| "the add-on never wrote its startup report" | the privileged half did not load → `tbmcp install-addon` again |
| settings tools fail but mail tools work | same cause; check `privileged modules` in `doctor` |
| full-text search finds nothing | Thunderbird's global indexer is off (Settings → General) |
| raw message source unavailable on IMAP | the message is not stored offline → `folder_sync_offline` |
| the first tool call after a killed daemon fails | the add-on takes ~40-60 s to reattach after an *abnormal* daemon exit; retry, or `tbmcp doctor --wait 60`. A clean exit reattaches in about a second. [Details](docs/VERIFIED-FINDINGS.md) |
| Codex reports a startup timeout | raise `startup_timeout_sec`; Codex defaults to 10 s |
| Claude Code truncates a large result | raise `MAX_MCP_OUTPUT_TOKENS` (default 25,000) |
| A dependency fails with "DLL load failed" or "cannot open shared object file" | A binary your OS will not load — Windows Application Control blocks unsigned, low-reputation wheels. `bootstrap` detects this and downgrades the offending package automatically; run `python bootstrap.py` and read the `binaries` step. |

`TBMCP_DEBUG=1` turns on verbose logging to stderr. `TBMCP_STATE_DIR` moves the
daemon's advertisement, lock and log out of the platform default. The add-on logs to Thunderbird's
error console with a `[tbmcp]` prefix, and `tb_console` returns those lines as a tool.
`tb_console` also includes the add-on's own `console.*` output, which the error
console window does not show.

---

## Development

```bash
uv venv && uv pip install -e ".[dev]"

pytest                                  # 286 tests, no Thunderbird needed
node --test "tests/js/*.test.mjs"       # 111 add-on tests under node:vm, no Thunderbird needed
ruff check . && ruff format --check .
python tools/check_consistency.py       # do all three layers still agree?
python tools/build_xpi.py build         # build the add-on package
python tools/gen_tool_reference.py      # regenerate the tool docs from the code
python tools/smoke_live.py              # read-only checks against a live Thunderbird
python tools/smoke_search.py            # the search tools, paging and errors, against a live Thunderbird
python tools/smoke_write.py             # gated-write checks; leaves the profile unchanged
```

Three layers have to agree on method names — the Python tools, the add-on handlers,
and the privileged forwarding list — and nothing notices when they stop agreeing until
runtime. `check_consistency.py` compares them, validates every JavaScript file, and
checks the manifest lists exactly the scripts that exist. Run it before you commit.

| Document | |
| --- | --- |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | why it is built this way, and what was rejected |
| [PROTOCOL.md](docs/PROTOCOL.md) | the wire protocol between Python and the add-on |
| [TOOLS.md](docs/TOOLS.md) | the contract: tool → bridge method → implementation |
| [TOOL-REFERENCE.md](docs/TOOL-REFERENCE.md) | every tool and parameter, generated from the code |
| [VERIFIED-FINDINGS.md](docs/VERIFIED-FINDINGS.md) | measurements against a live Thunderbird, and the traps |

---

## Author

GitHub [@U-C4N](https://github.com/U-C4N) · X [@UEdizaslan](https://x.com/UEdizaslan)

Built from actually living in Thunderbird all day, then made model-agnostic through
MCP. Every capability was measured against a real install before it was documented —
see [VERIFIED-FINDINGS.md](docs/VERIFIED-FINDINGS.md) for what that turned up.

Related work: [Autocad-MCP](https://github.com/U-C4N/Autocad-MCP) ·
[U-Pool](https://github.com/U-C4N/U-Pool) ·
[Deuz-SDK](https://github.com/Deuz-AI/Deuz-SDK)

## Contributing

Issues and pull requests are welcome. Before opening one:

```bash
pytest && ruff check . && python tools/check_consistency.py
```

`check_consistency.py` is the important one — it catches the mismatches between the
three layers that nothing else notices until runtime. If you add a tool, run
`python tools/gen_tool_reference.py` so the docs follow the code.

Report a security problem through GitHub rather than a public issue.

## Licence

MIT — see [LICENSE](LICENSE). The add-on contains no Mozilla-licensed code: the
reverse-WebSocket design was chosen partly so that no MPL-2.0 HTTP server needed to be
vendored.

# Tool reference

All 112 tools, generated from the code by `tools/gen_tool_reference.py`.
Do not edit by hand.

`read` tools cannot change anything and are the only ones registered under
`--read-only`. `write` and `destructive` tools require confirmation — an explicit
`confirm=true`, or an approval prompt where the client supports one.

## `mail` — 15 tools *(in the default toolset)*

Search, read, triage and file messages, plus attachments and tags.

### `mail_search` · read

Search the user's mail. Combine `full_text` with any filters below.

`full_text` uses Thunderbird's global index and searches headers and bodies
of already-indexed messages; `subject`/`author`/`body` are substring matches
evaluated per folder. Dates are ISO-8601. Results are summaries — call
`mail_get` for a body. Continue with `cursor=nextCursor`.

Parameters: full_text, subject, author, recipients, body, folder_id, account_id, include_subfolders, unread, flagged, junk, has_attachment, tags, tag_mode, from_date, to_date, to_me, from_me, min_size, max_size, limit, cursor  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_list` · read

List messages in one folder, newest first by default.

Use `folder_list` to discover folder ids. For anything selective, prefer
`mail_search`.

Parameters: **folder_id**, limit, cursor, sort_by, descending  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_get` · read

Read one message. `text` gives headers plus the plain-text body.

`summary` skips the body entirely; `full` adds the MIME part tree and every
header. Encrypted mail is decrypted when Thunderbird can.

Parameters: **message_id**, detail, decrypt  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_get_many` · read

Read up to 50 messages in one round trip — for triaging a search result.

Parameters: **message_ids**, detail  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_get_source` · read

Fetch a message's raw RFC 5322 source, for header forensics.

On IMAP this needs the message to be available offline; the tool says so
rather than returning a partial.

Parameters: **message_id**, decrypt  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_attachments` · read

List a message's attachments with part names, sizes and content types.

Parameters: **message_id**  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_save_attachment` · write

Write one attachment to a directory on this machine.

`part_name` comes from `mail_attachments`. Refuses to clobber an existing
file unless `overwrite=true`.

Parameters: **message_id**, **part_name**, **directory**, filename, overwrite  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_mark` · write

Set read/flagged/junk state or adjust tags on one or more messages.

Cheap and reversible, so no confirmation is required. Tag keys come from
`mail_tags`.

Parameters: **message_ids**, read, flagged, junk, add_tags, remove_tags  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_move` · write

Move messages into another folder.

On IMAP the move is asynchronous — the tool waits for Thunderbird to confirm
before returning, so a following search reflects the change.

Parameters: **message_ids**, **destination_folder_id**, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_copy` · write

Copy messages into another folder, leaving the originals in place.

Parameters: **message_ids**, **destination_folder_id**, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_archive` · write

Archive messages using each account's configured archive layout.

Parameters: **message_ids**, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_delete` · **destructive**

Delete messages. Moves to Trash unless `permanent=true`.

A permanent delete cannot be undone from Thunderbird, so prefer the default
and let the user empty Trash themselves.

Parameters: **message_ids**, permanent, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_tags` · read

List the tags defined in Thunderbird, with keys, labels and colours.

Parameters: *none*

### `mail_tag_upsert` · write

Create a tag, or recolour/rename an existing one.

`color` is `#RRGGBB`. Omit `key` to create a new tag; pass an existing key to
update it.

Parameters: **label**, key, color, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_tag_delete` · **destructive**

Remove a tag definition. Messages keep the raw keyword but lose the label.

Parameters: **key**, confirm  
*(bold means required; `confirm` is the confirmation gate)*

## `folders` — 15 tools *(in the default toolset)*

The folder tree, its counts, and creating/renaming/emptying folders.

### `folder_list` · read

List mail folders with their ids and message counts.

With no filters this browses: every account's root folder plus one level
below it. Raise `depth` to walk further, or give a filter — `name` is a
case-insensitive substring — and the whole tree is searched instead. Pass
`tree=true` to get folders nested under `children` rather than flat.

Counts come from Thunderbird's own folder database. On IMAP that database
can lag until the folder has been selected once in Thunderbird, so treat
unread and total counts as close rather than exact.

Parameters: account_id, name, parent_id, special_use, is_virtual, is_tag, is_unified, is_favorite, is_root, has_unread_messages, depth, tree, include_counts, limit  
*(bold means required; `confirm` is the confirmation gate)*

### `folder_get` · read

Get one folder: counts, special use, flags and IMAP quota.

Use this to re-check a count after a move or a delete; `folder_list` is the
cheaper way to find the id in the first place.

Parameters: **folder_id**, include_subfolders  
*(bold means required; `confirm` is the confirmation gate)*

### `folder_capabilities` · read

Report what may be done to a folder before attempting it.

Answers whether the folder can hold messages, take subfolders, be renamed,
be deleted, or have messages deleted from it. Worth a call before offering
the user a plan that a server would refuse.

Parameters: **folder_id**  
*(bold means required; `confirm` is the confirmation gate)*

### `folder_get_unified` · read

Get the unified folder that spans every account, e.g. all inboxes at once.

Its id works anywhere a folder id is accepted, so `mail_list` on the unified
inbox lists new mail across all accounts in one call.

Parameters: **folder_type**, include_subfolders  
*(bold means required; `confirm` is the confirmation gate)*

### `folder_create` · write

Create a folder inside another folder, or at the top of an account.

Parameters: **name**, parent_id, account_id, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `folder_rename` · write

Rename a folder, keeping its messages and subfolders.

Parameters: **folder_id**, **new_name**, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `folder_move` · write

Move a folder under a different parent, with its subfolders.

`destination_id` is the new parent folder — use an account's root folder to
move it to the top level. Across accounts this copies then deletes, which on
IMAP can take a while.

Parameters: **folder_id**, **destination_id**, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `folder_copy` · write

Copy a folder and its contents under another parent, leaving the original.

Parameters: **folder_id**, **destination_id**, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `folder_delete` · **destructive**

Delete a folder, its subfolders and every message in them.

Thunderbird moves the folder to Trash unless it is already inside Trash, in
which case it goes for good. Check `folder_get` first if the count matters —
the reply reports what was removed, but cannot put it back.

Parameters: **folder_id**, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `folder_mark_read` · write

Mark every message in a folder as read.

There is no per-message undo for this, which is why it asks first. On IMAP
the flags are pushed to the server.

Parameters: **folder_id**, include_subfolders, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `folder_set_favorite` · write

Add or remove a folder from the user's favourites.

Cosmetic and reversible — it only affects the folder pane's Favourites view,
so it is not gated. Prompting for something this harmless would only train
the user to click through the prompts that do matter.

Parameters: **folder_id**, favorite  
*(bold means required; `confirm` is the confirmation gate)*

### `folder_empty_trash` · **destructive**

Permanently delete everything in one account's Trash.

This is not recoverable from Thunderbird. Give `account_id` and the account's
configured Trash is used; give `folder_id` to empty a specific one. Subfolders
of Trash are removed too unless `remove_subfolders=false`.

Parameters: account_id, folder_id, remove_subfolders, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `folder_empty_junk` · **destructive**

Permanently delete everything in one account's Junk folder.

Subfolders of Junk are emptied but kept, since they are usually filter
targets the user set up deliberately. Not recoverable.

Parameters: account_id, folder_id, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `folder_sync_offline` · write

Fetch an IMAP folder's message bodies so they are available offline.

This is what `mail_get_source` needs before it can return raw source for an
IMAP message. A large folder means a long download and real network traffic.

Parameters: **folder_id**, include_subfolders, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `folder_compact` · write

Reclaim the disk space left behind by deleted messages.

Deleted mail stays in the message store until the folder is compacted. Pass
`folder_id` for one folder or `account_id` for all of an account's. Nothing
readable is lost, but the store is rewritten, so do not interrupt it.

Parameters: folder_id, account_id, confirm  
*(bold means required; `confirm` is the confirmation gate)*

## `compose` — 6 tools *(in the default toolset)*

Sending, replying, forwarding, drafts and templates.

### `mail_send` · **destructive**

Write a message. Saves a reviewable draft unless `mode="send"`.

`mode="later"` queues it in the Outbox instead. Recipients are one address per
list entry. `attachments` are paths to files on this machine. A draft still
asks for confirmation, because the identical call with `mode="send"` would
deliver it. Set `reply_to_message_id` to thread the message under an existing
one — but `mail_reply` is usually what you want, since it also quotes.

Parameters: **to**, **subject**, **body**, cc, bcc, is_html, attachments, identity_id, mode, reply_to_message_id, priority, return_receipt, delivery_format, custom_headers, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_reply` · **destructive**

Reply to a message. Saves a reviewable draft unless `mode="send"`.

Thunderbird derives the recipients, the subject and the quoted original;
`body` goes above the quote. `reply_all` copies everyone, `reply_to_list`
answers the mailing list. Passing `cc` replaces the addresses Thunderbird
derived, so leave it unset unless that is the intent.

Parameters: **message_id**, **body**, reply_all, reply_to_list, quote_original, is_html, subject, cc, bcc, attachments, identity_id, mode, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_forward` · **destructive**

Forward a message. Saves a reviewable draft unless `mode="send"`.

`inline` quotes the original in the body; `attachment` attaches it as a
`.eml`, which preserves the headers a recipient may need. `body` is your
covering note and goes above the forwarded text.

Parameters: **message_id**, **to**, body, forward_as, cc, bcc, subject, is_html, attachments, identity_id, mode, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_draft_save` · write

Save a message without sending it, as a draft or a template.

Nothing leaves the machine, so this is not gated — a draft is exactly the
thing to produce when you want the user to review before anything is sent.
A template is the reusable kind: Thunderbird keeps it in Templates and opens
a copy when the user picks it. Recipients are optional here, unlike a send.

Parameters: subject, body, to, cc, bcc, kind, is_html, attachments, identity_id  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_compose_open` · write

Open a populated compose window for the user to finish by hand.

The right answer whenever the wording matters more than the automation, or
when the user declined a send: they get the draft in front of them with the
cursor in it. Nothing is sent or saved, and the user sees the window appear,
so this is not gated.

Parameters: to, subject, body, cc, bcc, is_html, attachments, identity_id, reply_to_message_id, forward_message_id, reply_all, quote_original  
*(bold means required; `confirm` is the confirmation gate)*

### `mail_send_status` · read

List messages sitting in the Outbox, unsent.

An empty list is the normal answer. Anything here was queued with
`mode="later"`, or written while Thunderbird was offline, and will go out on
the next 'Send Unsent Messages'.

Parameters: limit  
*(bold means required; `confirm` is the confirmation gate)*

## `search` — 7 tools *(in the default toolset)*

Ranked whole-corpus search, conversations, and saved searches.

### `search_global` · read

Ranked full-corpus search across every indexed folder and account.

Best for open questions — "what did we agree about the shipment", "anything
from the accountant about VAT". Results carry a relevance score and a
conversation id you can pass to `search_conversation`. For precise filters
(one folder, unread only, a date range) use `mail_search` instead.

If this returns nothing unexpectedly, call `search_index_status`: the global
indexer can be disabled or still catching up.

Parameters: **query**, limit, offset, folder_id  
*(bold means required; `confirm` is the confirmation gate)*

### `search_conversation` · read

Every message in one thread, oldest first, across folders and accounts.

Give either a `message_id` from `mail_search` or an RFC `header_message_id`.
This is how you reconstruct a discussion that spans Inbox, Sent and an
archive folder without three separate searches.

Parameters: message_id, header_message_id, limit  
*(bold means required; `confirm` is the confirmation gate)*

### `search_index_status` · read

Whether Thunderbird's global index is enabled, and how far along it is.

Call this to explain an empty `search_global` result. When indexing is off,
`mail_search` with `subject`/`author`/`body` filters still works.

Parameters: *none*

### `search_saved_list` · read

List the saved searches (virtual folders) and what each one matches.

Parameters: *none*

### `search_saved_create` · write

Create a saved search that appears in the folder pane.

`search_folder_ids` are folders to look in (from `folder_list`). Each term is
`{"attribute": "subject", "operator": "contains", "value": "invoice"}`;
attributes and operators are listed in the error message if you get one
wrong. `match_all=false` makes the terms OR together. `online_search` asks
the IMAP server to run the search instead of using the local database.

Nothing is copied or moved — a saved search is a stored query.

Parameters: **name**, **search_folder_ids**, **terms**, match_all, online_search, parent_folder_id, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `search_saved_update` · write

Redefine an existing saved search, by name or uri.

Only what you pass is replaced; `terms` replaces the whole condition list
rather than merging, because a partial merge has no sensible meaning for a
boolean query.

Parameters: **saved_search**, search_folder_ids, terms, online_search, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `search_saved_delete` · **destructive**

Remove a saved search. The messages it listed are not touched.

A saved search only stores a query, so deleting one loses the query and
nothing else — but the query itself is not recoverable, hence the prompt.

Parameters: **saved_search**, confirm  
*(bold means required; `confirm` is the confirmation gate)*

## `contacts` — 13 tools

Address books, contacts and mailing lists.

### `contact_search` · read

Look someone up in the address book.

This is the tool to reach for when the user says "what is X's email" — it
matches names, addresses, phone numbers, nicknames and organisations across
every address book, and `primaryEmail` on each result is the address
Thunderbird itself would use. Set `include_remote=false` to skip LDAP and
CardDAV books, which can be slow.

Parameters: **query**, address_book_id, include_remote, limit  
*(bold means required; `confirm` is the confirmation gate)*

### `contact_list` · read

List contacts, across every address book unless one is named.

For finding a particular person use `contact_search` instead. Continue with
`cursor=nextCursor`; a cursor is only valid while the set of address books
stays as it was.

Parameters: address_book_id, limit, cursor  
*(bold means required; `confirm` is the confirmation gate)*

### `contact_get` · read

Read one contact in full, including its raw vCard.

`include_photo` adds the picture as a data URL, which is large — leave it off
unless the picture is the point.

Parameters: **contact_id**, include_photo  
*(bold means required; `confirm` is the confirmation gate)*

### `contact_create` · write

Add a contact to an address book.

The first address in `emails` becomes the preferred one. `birthday` is
`YYYY-MM-DD`. For postal addresses or any property not named here, pass a
complete `vcard` instead of the fields. Address book ids come from
`addressbook_list`.

Parameters: **address_book_id**, display_name, first_name, last_name, emails, phones, organisation, job_title, nickname, birthday, notes, vcard, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `contact_update` · write

Change fields on an existing contact.

Fields you omit are left alone; `""` or an empty list clears one. Passing
`emails` or `phones` replaces the whole set, so include the addresses you
want to keep. A `vcard` replaces the entire contact.

Parameters: **contact_id**, display_name, first_name, last_name, emails, phones, organisation, job_title, nickname, birthday, notes, vcard, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `contact_delete` · **destructive**

Delete a contact. There is no Trash for contacts, so this cannot be undone.

The contact also leaves every mailing list it was on. The returned `previous`
holds its vCard, which is enough to recreate it with `contact_create`.

Parameters: **contact_id**, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `addressbook_list` · read

List the address books, with how many contacts and lists each holds.

`readOnly` books reject writes; `remote` ones are LDAP or CardDAV. Turn
`include_counts` off if a remote book makes this slow.

Parameters: include_counts  
*(bold means required; `confirm` is the confirmation gate)*

### `addressbook_create` · write

Create an empty local address book.

Parameters: **name**, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `addressbook_delete` · **destructive**

Delete an address book together with all its contacts and mailing lists.

Irreversible and usually not what the user meant — deleting one contact is
`contact_delete`. Run with `dry_run_only=true` first to see the size of it.

Parameters: **address_book_id**, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `mailinglist_list` · read

List address book mailing lists, with member counts.

These are Thunderbird's own distribution lists, not mailing lists you
subscribe to. `include_members` returns each roster in full.

Parameters: address_book_id, include_members  
*(bold means required; `confirm` is the confirmation gate)*

### `mailinglist_create` · write

Create an empty mailing list in an address book.

`nickname` is what the user types in a compose window to address the whole
list, so it is worth setting.

Parameters: **address_book_id**, **name**, nickname, description, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `mailinglist_add_member` · write

Add an existing contact to a mailing list.

If the contact lives in a different address book it is copied into the list's
one; the result says so via `copiedToAddressBook`.

Parameters: **mailing_list_id**, **contact_id**, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `mailinglist_remove_member` · write

Take a contact off a mailing list. The contact itself is left alone.

Parameters: **mailing_list_id**, **contact_id**, confirm  
*(bold means required; `confirm` is the confirmation gate)*

## `calendar` — 12 tools

Calendars, events and tasks, including recurring series.

### `calendar_list` · read

List the user's calendars, with ids, types and whether each is writable.

Every other tool here takes one of these ids. A calendar marked
`readOnly` refuses writes; `disabled` ones are skipped when listing items.

Parameters: *none*

### `calendar_create` · write

Create a calendar and register it with Thunderbird.

`storage` keeps events in the local profile and needs no `url`; `ics` and
`caldav` need the file or collection URL. `color` is `#RRGGBB`.

Parameters: **name**, type, url, color, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `calendar_update` · write

Rename or recolour a calendar, or toggle read-only and disabled.

Only the fields you pass are touched. Nothing here affects the events
inside the calendar.

Parameters: **calendar_id**, name, color, read_only, disabled, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `calendar_delete` · **destructive**

Remove a calendar. Deletes its events and tasks with it.

`unregister_only=true` is the reversible half: the calendar disappears
from Thunderbird but its data is left where it is, ready to be added
again. Prefer it unless the user asked for the data to go too.

Parameters: **calendar_id**, unregister_only, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `event_list` · read

List events in a time window, soonest first.

Defaults to the next 30 days across every enabled calendar; pass
`from_date`/`to_date` (ISO-8601) for anything else, or `days` to widen the
default window. `include_occurrences` expands recurring series into the
individual meetings that fall inside the window, which is what you want
for "what is on next week".

Parameters: calendar_id, from_date, to_date, days, include_occurrences, limit, timezone  
*(bold means required; `confirm` is the confirmation gate)*

### `event_get` · read

Read one event or task in full, including attendees and recurrence.

Searches every calendar unless `calendar_id` narrows it. For a recurring
series this returns the series plus its next few start times; pass
`occurrence_date` to read a single occurrence instead.

Parameters: **item_id**, calendar_id, occurrence_date, timezone  
*(bold means required; `confirm` is the confirmation gate)*

### `event_create` · write

Create an event. Omit `end` for a one-hour meeting.

`start`/`end` are ISO-8601; a time with no offset is read as wall-clock
time in `timezone` (the calendar's default if unset). For `all_day=true`
pass plain dates — the end is exclusive per iCalendar, so omit it for a
single day. `recurrence_rule` is an RRULE body such as
`FREQ=WEEKLY;BYDAY=TU;COUNT=10`. Attendees are stored but no invitations
are sent.

Parameters: **title**, **start**, end, all_day, duration_minutes, calendar_id, timezone, location, description, categories, attendees, organizer, recurrence_rule, url, status, privacy, priority, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `event_update` · write

Change an event. Only the fields you pass are touched.

For a recurring event this rewrites the **whole series**; pass
`occurrence_date` (the start of the one you mean) to change a single
occurrence and leave the rest alone. The result says which it did. Pass an
empty string to clear `location`, `description` or `url`.

Parameters: **item_id**, calendar_id, occurrence_date, title, start, end, all_day, timezone, location, description, categories, attendees, organizer, recurrence_rule, url, status, privacy, priority, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `event_delete` · **destructive**

Delete an event or a task. Calendars have no trash, so this is final.

For a recurring item this deletes the **entire series**. Pass
`occurrence_date` to cancel one occurrence and keep the rest; the result
says which it did. Read the item with `event_get` first if you are not
sure it recurs.

Parameters: **item_id**, calendar_id, occurrence_date, timezone, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `task_list` · read

List tasks, soonest due first. Completed ones are hidden by default.

Unlike `event_list` this is not windowed by default: a task with no due
date falls outside every range, and those are exactly the ones people
forget. Pass `from_date`/`to_date` to narrow it.

Parameters: calendar_id, include_completed, from_date, to_date, limit, timezone  
*(bold means required; `confirm` is the confirmation gate)*

### `task_create` · write

Create a task. Everything but the title is optional.

`start` is the entry date and `due` the deadline, both ISO-8601. A task
with neither still appears in Thunderbird's task list, which is where
undated to-dos belong.

Parameters: **title**, due, start, all_day, calendar_id, timezone, description, location, categories, percent_complete, completed, status, priority, recurrence_rule, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `task_update` · write

Change a task, or tick it off with `completed=true`.

Only the fields you pass are touched. `completed=true` also stamps the
completion time and sets progress to 100%. For a repeating task this
rewrites the series unless `occurrence_date` names one instance.

Parameters: **item_id**, calendar_id, occurrence_date, title, due, start, all_day, timezone, description, location, categories, percent_complete, completed, status, priority, recurrence_rule, confirm  
*(bold means required; `confirm` is the confirmation gate)*

## `filters` — 8 tools

Thunderbird's message filters, including running them on demand.

### `filter_list` · read

List filters in execution order, with their conditions and actions.

Omit `account_key` for every account. `index` is the position in the list and
is what the other `filter_*` tools take — it shifts when filters are added,
removed or reordered, so re-list before acting on a stale index.

Parameters: account_key  
*(bold means required; `confirm` is the confirmation gate)*

### `filter_get` · read

Read one filter in full, by account and index.

Parameters: **account_key**, **index**  
*(bold means required; `confirm` is the confirmation gate)*

### `filter_create` · write

Create a filter. It is appended, so existing rules keep their order.

Conditions: `[{"attribute": "from", "operator": "contains", "value": "bank"}]`.
Actions: `[{"type": "moveToFolder", "folderId": "account1://Bank"}]`.
`run_when` defaults to `["inbox", "manual"]` — on new mail, and when filters
are applied by hand. `match_all=false` makes the conditions OR together.

Filters act on real mail automatically from now on, so check the conditions
carefully; `dry_run_only=true` shows what would be created.

Parameters: **account_key**, **name**, **search_terms**, **actions**, run_when, match_all, enabled, position, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `filter_update` · write

Change a filter in place. Only what you pass is touched.

`search_terms` and `actions` each replace the whole list rather than merging —
a partial merge of a boolean condition set has no sensible meaning. Read the
current definition with `filter_get` first.

Parameters: **account_key**, **index**, name, search_terms, actions, run_when, enabled, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `filter_set_enabled` · write

Turn one filter on or off without changing its definition.

Safer than deleting when you only want to stop a rule for now.

Parameters: **account_key**, **index**, **enabled**, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `filter_reorder` · write

Move a filter to a different position in the execution order.

`to_index` is the position it should end up at. Order matters whenever a rule
uses `stopExecution`, or when two rules would both move the same message.

Parameters: **account_key**, **index**, **to_index**, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `filter_delete` · **destructive**

Delete a filter. Thunderbird keeps no history, so the rule is gone.

The returned `previous` block is the full definition — keep it if you might
want to recreate the rule. Consider `filter_set_enabled` instead.

Parameters: **account_key**, **index**, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `filter_run` · write

Apply filters to folders on demand, as "Run Filters on Folder" does.

Defaults to every enabled filter on the account's Inbox. This really moves,
tags and deletes mail according to the rules, so it is gated — and it is the
right way to check a new filter against existing messages.

Parameters: **account_key**, folder_ids, filter_indexes, confirm  
*(bold means required; `confirm` is the confirmation gate)*

## `accounts` — 18 tools

Incoming servers, identities, signatures, SMTP servers, per-account junk.

### `account_list` · read

List the mail accounts and how each one is configured.

The official API supplies names, types and identities; the privileged half
adds host, port, security and check-for-mail settings. The `key` of each
account (`account1`) is what every other tool in this toolset takes. No
password is read.

Parameters: include_settings  
*(bold means required; `confirm` is the confirmation gate)*

### `account_get_server` · read

Read one account's incoming server settings.

Host, port, connection security, authentication method and the
check-for-new-mail settings. Passwords are never read; the login manager is
out of reach of this bridge.

Parameters: **account_key**  
*(bold means required; `confirm` is the confirmation gate)*

### `account_set_server` · write

Change one incoming server setting. Getting the connection wrong stops mail.

`setting` is one of: `hostname`, `port`, `username`, `socketType`,
`authMethod`, `prettyName` (the account's display name), `doBiff`,
`biffMinutes`, `downloadOnBiff`, `loginAtStartUp`,
`limitOfflineMessageSize`, `maxMessageSize`, `emptyTrashOnExit`, and for IMAP
`useIdle`, `maximumConnectionsNumber`, `forceSelect`, `cleanupInboxOnExit`.

`socketType` takes `plain`, `starttls` or `tls`; `authMethod` takes `none`,
`password-cleartext`, `password-encrypted`, `gssapi`, `ntlm` or `oauth2`. The
account type itself cannot be changed. Passwords are never written: after a
hostname or username change Thunderbird will ask the user to sign in again.

Parameters: **account_key**, **setting**, **value**, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `account_get_junk` · read

Read one account's junk-mail handling: level, whitelist, move and purge rules.

Parameters: **account_key**  
*(bold means required; `confirm` is the confirmation gate)*

### `account_set_junk` · write

Change one junk-mail setting for an account.

`setting` is one of: `level`, `moveOnSpam`, `moveTargetMode`,
`actionTargetAccount`, `actionTargetFolder`, `purge`, `purgeInterval`,
`useWhiteList`, `whiteListAbURI`, `manualMark`, `markAsReadOnSpam`.
Folder settings take a folder URI, which `account_get_junk` reports.

Parameters: **account_key**, **setting**, **value**, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `account_get_folders` · read

Read where an identity files sent mail, drafts, templates and archives.

These live on the identity, not the server, so an account with two identities
can file its mail in two places. Omit `identity_key` for the account's default
identity.

Parameters: **account_key**, identity_key  
*(bold means required; `confirm` is the confirmation gate)*

### `account_set_folders` · write

Change where an identity files sent mail, drafts, templates or archives.

`setting` is one of: `fccFolder` (sent), `doFcc`, `draftFolder`,
`stationeryFolder` (templates), `archiveFolder`, `archiveEnabled`,
`archiveGranularity`, `archiveKeepFolderStructure`. Folder settings take a
folder URI as reported by `account_get_folders`, not a folder id.

Parameters: **account_key**, **setting**, **value**, identity_key, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `account_get_sync` · read

Read an account's offline and synchronisation settings.

Parameters: **account_key**  
*(bold means required; `confirm` is the confirmation gate)*

### `account_set_sync` · write

Change one offline or synchronisation setting for an account.

`setting` is one of: `offlineDownload`, `autoSyncOfflineStores`,
`downloadBodiesOnGetNewMail`, `limitOfflineMessageSize`, `maxMessageSize`,
`offlineSupportLevel`, `doBiff`, `biffMinutes`, `downloadOnBiff`, `useIdle`,
and for POP3 `leaveMessagesOnServer`, `deleteMailLeftOnServer`,
`numDaysToLeaveOnServer`, `headersOnly`.

Turning offline storage on does not download anything by itself — use
`folder_sync_offline` for that.

Parameters: **account_key**, **setting**, **value**, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `identity_list` · read

List the sending identities, across every account or just one.

An identity is a from-address with its own signature, outgoing server and
filing folders. Its `key` is what `identity_get` and `identity_set` take.

Parameters: account_key  
*(bold means required; `confirm` is the confirmation gate)*

### `identity_get` · read

Read one identity in full: addresses, signature, outgoing server, filing folders.

Credentials are not part of this: the outgoing server is reported by key, and
its password stays in the login manager.

Parameters: **identity_key**  
*(bold means required; `confirm` is the confirmation gate)*

### `identity_set` · write

Change an identity's addresses and composition defaults.

Only the fields you pass are touched; pass an empty string to clear one.
`email` is the from-address the recipient sees, so changing it can break
replies and any filter that matches on it. `smtp_server_key` comes from
`smtp_list`. Signatures go through `identity_set_signature`, and the sent or
drafts folders through `account_set_folders`. Passwords are never written.

Parameters: **identity_key**, full_name, email, reply_to, organization, compose_html, attach_vcard, smtp_server_key, do_bcc, bcc_list, catch_all, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `identity_set_signature` · write

Replace an identity's signature text, or point it at a file.

Pass `signature` for inline text (set `is_html` when it contains markup), or
`file_path` for a signature file on this machine — the two are mutually
exclusive in Thunderbird. `attach=false` keeps the text but stops appending
it; `below_quote` controls whether the signature sits under a quoted reply.

Parameters: **identity_key**, signature, is_html, file_path, attach, below_quote, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `smtp_list` · read

List the SMTP servers, and which one is the default.

Each server's `key` is what the other `smtp_*` tools take. Passwords are
never read — only whether Thunderbird has one stored.

Parameters: *none*

### `smtp_create` · write

Add an SMTP server. Nothing sends through it until an identity points at it.

Omit `port` to take the default for the chosen security (587 for STARTTLS,
465 for TLS). No password is stored: Thunderbird prompts the user the first
time the server is used, and this bridge never writes to the login manager.

Parameters: **hostname**, port, username, socket_type, auth_method, description, make_default, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `smtp_update` · write

Change an existing SMTP server. Only the fields you pass are touched.

Get `server_key` from `smtp_list`. Changing the hostname or username usually
invalidates the stored password, and the user will be asked for it on the next
send — this tool neither reads nor writes it.

Parameters: **server_key**, hostname, port, username, socket_type, auth_method, description, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `smtp_delete` · **destructive**

Remove an SMTP server. Identities using it will be left unable to send.

Thunderbird has no undo for this, and the settings are not recoverable from
the UI, so the reply repeats them — check `smtp_list` for identities pointing
at this key first.

Parameters: **server_key**, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `smtp_set_default` · write

Make one SMTP server the default for identities that have none of their own.

Parameters: **server_key**, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

## `settings` — 11 tools

Preferences, junk training and OpenPGP key listing.

### `pref_get` · read

Read one Thunderbird preference.

Reports the value, its type, whether the user has changed it from the default,
and whether an enterprise policy has locked it. `writable` says whether this
server would let you change it. Use `settings_describe` to find the name.

Parameters: **name**  
*(bold means required; `confirm` is the confirmation gate)*

### `pref_get_many` · read

Read up to 100 preferences in one round trip.

Preferences that do not exist come back with `exists: false` rather than
failing the whole call, and are listed again under `missing`.

Parameters: **names**  
*(bold means required; `confirm` is the confirmation gate)*

### `pref_list` · read

List preferences under a branch, e.g. `prefix="mail.biff."`.

A bare prefix matches over five thousand preferences on a normal profile, so
pass a branch and keep `limit` modest. `only_user_set=true` narrows it to what
differs from the shipped defaults.

Parameters: prefix, only_user_set, limit  
*(bold means required; `confirm` is the confirmation gate)*

### `pref_user_set` · read

Everything the user has changed from the shipped defaults.

The honest answer to "what is my Thunderbird configured like": a normal profile
has a few hundred of these, dominated by per-account `mail.server.*` and
`mail.identity.*` entries. Anything that looks like a credential is redacted
before it leaves Thunderbird.

Parameters: prefix  
*(bold means required; `confirm` is the confirmation gate)*

### `settings_describe` · read

Map a human request onto the preference that controls it.

Consult this instead of guessing a preference name. It costs no round trip to
Thunderbird, so it is cheap to call first. Value meanings are for Thunderbird
153; current values are not included because they are per-profile — read one
with `pref_get`. `writable` reflects this server's allowlist, not Thunderbird's
own locking.

Parameters: area, search  
*(bold means required; `confirm` is the confirmation gate)*

### `pref_set` · write

Change one Thunderbird preference.

`type` must match the preference's existing type — pass `"int"` for a number
even when it reads like a boolean (`calendar.alarms.onforevents` is the classic
trap), and the call is refused with the right type if you get it wrong. Use
`settings_describe` for the name and the allowed values, and `dry_run_only=true`
to see the current value alongside what you propose.

Parameters: **name**, **type**, **value**, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `pref_reset` · write

Clear a user-set preference so the shipped default applies again.

The cleanest undo for a `pref_set` the user did not like — it removes the entry
from `prefs.js` rather than writing the old value back. A preference already at
its default is reported as unchanged instead of failing.

Parameters: **name**, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `junk_get` · read

Read the global junk (bayesian) filter settings.

`userHasClassified` is the one to check first: an untrained filter scores
nothing usefully, however the rest is configured. Per-account junk behaviour —
which folder spam moves to, whitelists, purge age — lives in `account_get_junk`.

Parameters: *none*

### `junk_set` · write

Change the global junk filter settings. Only the fields you pass are touched.

`manual_mark_mode="delete"` makes marking a message as junk delete it outright,
which is a real data-loss setting — prefer `"move"` unless the user asks for
deletion by name.

Parameters: manual_mark, manual_mark_mode, mark_as_read_on_spam, logging_enabled, confirm  
*(bold means required; `confirm` is the confirmation gate)*

### `junk_train` · write

Teach the junk filter that these messages are junk, or are not.

Ids are RFC 5322 Message-ID header values (`headerMessageId` from `mail_get`),
not the integer ids from `mail_search`: those are only valid while Thunderbird
stays open, and training data outlives the session. Training is cumulative and
biased corpora are hard to undo, so train on messages the user has actually
judged rather than on a whole search result.

Parameters: **header_message_ids**, **classification**, confirm, dry_run_only  
*(bold means required; `confirm` is the confirmation gate)*

### `openpgp_list_keys` · read

List the OpenPGP keys in Thunderbird's keyring.

Metadata only — fingerprints, user ids, validity, expiry, and whether a secret
key is present. Private key material is never exported through this server, and
no tool here can export it; a user who wants a backup should use Thunderbird's
own End-to-End Encryption settings. `search` matches an email address, key id or
fingerprint; `only_secret=true` narrows to keys this user can sign with.

Parameters: search, only_secret  
*(bold means required; `confirm` is the confirmation gate)*

## `admin` — 7 tools *(in the default toolset)*

Connection status, events, diagnostics and the error console.

### `tb_status` · read

Whether Thunderbird is attached, and which halves of the add-on loaded.

Answered by the local daemon, so it works when Thunderbird is closed. Call it
first whenever another tool reports that it cannot reach Thunderbird.

Parameters: *none*

### `tb_wait` · read

Block until Thunderbird attaches to the bridge, then report status.

Use it after `tb_restart`, or after asking the user to start Thunderbird.
Fails with a message naming what is missing if nothing attaches in time.

Parameters: timeout_seconds  
*(bold means required; `confirm` is the confirmation gate)*

### `tb_events` · read

Read buffered Thunderbird notifications: new mail, folder and account changes.

Poll with `since=latestSeq` from the previous call to see only what is new.
The daemon keeps a few hundred events, so a long gap between polls can drop
some — `latestSeq` jumping by more than you received is how you tell.

Parameters: since, limit  
*(bold means required; `confirm` is the confirmation gate)*

### `tb_diagnostics` · read

One report: versions, profile, which capabilities loaded, accounts, indexing.

The first thing to fetch when anything behaves oddly. It includes whether this
build permits unsigned add-ons and experiment APIs, which is what explains a
half-installed bridge, and the message store type per account.

Parameters: *none*

### `tb_console` · read

Recent lines from Thunderbird's error console, newest last.

Narrow it with `contains` — `tbmcp` shows this bridge's own complaints, and an
add-on id or a source filename shows someone else's. Anything shaped like a
password or token is redacted inside Thunderbird before it is sent.

Parameters: contains, limit  
*(bold means required; `confirm` is the confirmation gate)*

### `tb_addons` · read

List installed add-ons with their enabled and signature state.

`isBridge` marks this server's own add-on. A `signedState` of 0 is expected
for it: the bridge is installed unsigned, which this Thunderbird permits.

Parameters: kind  
*(bold means required; `confirm` is the confirmation gate)*

### `tb_restart` · **destructive**

Restart Thunderbird. Every call in flight fails, including other clients'.

The bridge connection drops, so this returns before the restart happens and
the result says nothing about whether it succeeded — wait with `tb_wait`
afterwards. Unsent compose windows and unsaved drafts are lost, so ask the
user before you do it; a stuck sync usually does not need it.

Parameters: confirm  
*(bold means required; `confirm` is the confirmation gate)*

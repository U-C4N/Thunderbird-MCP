# 1.4.0 workflows and recovery

## Operation history

The daemon owns a SQLite journal beneath
`<TBMCP_STATE_DIR or platform state root>/tbmcp/profiles/<profile-hash>/operations.sqlite`.
It stores plans, references, content hashes, changed flag/tag values and receipts.
It does not store mail subjects, address lists, bodies or attachment bytes. Paths
and attachment filenames are present for export recovery. The profile directory
restricts access to the current user (and SYSTEM on Windows).

History is retained for seven days. Duplicate detection ends with retention;
never interpret the journal as a permanent exactly-once guarantee. The database
schema is versioned; a newer schema is refused rather than overwritten. There is
no migration of Thunderbird's own mailbox databases.

`tb_operation_status(operation_id)` returns a status, counts and paginated item
receipts. `tb_operation_list()` finds prior operations after reconnecting.
`tb_operation_cancel()` prevents remaining steps; it cannot recall a send or undo
an already completed native action. Toolsets `mail` and `compose` automatically
include these helpers; read-only mode excludes cancellation and mutations.

| Status | Meaning |
| --- | --- |
| queued / running | Accepted / executing; query the same operation. |
| succeeded | The local API returned a verified result. SMTP success is not delivery or read confirmation. |
| partial / failed | Some / no applicable steps succeeded; inspect each receipt. |
| cancelled | Remaining steps were stopped; successful steps remain applied. |
| interrupted | Work stopped and will not automatically resume. |
| unknown | A dispatched effect cannot be established. Do not repeat it blindly. |

If one batch step is uncertain, the operation is `unknown` and remaining unstarted
items are `interrupted`. Once a read-only receipt proves the first step, the
operation remains `interrupted` if there is still unstarted work; it never resumes
those items automatically.

Reconnect does not mean a native call stopped. The daemon serialises writes and
tracks late responses, runtime receipts and native process identity. Native work
known to continue after a timeout requires a proven process restart before new
writes. `tb_status` and `tbmcp doctor` report the barrier. Restart is an explicit,
gated action, never automatic recovery. Reads remain available.

`tbmcp daemon --force` negotiates shutdown with the current owner and still takes
the startup lock. Active/unresolved writes or an unresponsive live owner block
replacement. A live startup that has not advertised is not silently superseded;
finish or explicitly stop that startup before retrying. Dead-process locks remain
automatically recoverable. Disk/journal worker failures latch write protection and
remain visible in diagnostics even if later reads succeed.
An older daemon without operation-state support cannot certify safe retirement;
finish its work and explicitly restart Thunderbird and the daemon for that upgrade.

## Saved drafts

1. `mail_draft_save` creates a draft; its result includes `draftRef` and `revision`
   when the installed add-on can resolve it. Existing `draftId` remains.
2. `mail_draft_get(message_id=...)` or `mail_draft_get(draft_ref=...)` reads the
   saved version without opening a composer. An open window's unsaved edits are
   not represented as saved content.
3. `mail_draft_update(draft_ref, expected_revision, request_key, changes)` applies
   only explicitly supplied fields. Omitted fields are preserved; an empty
   recipient list clears that field. Body replacement uses the existing format
   and includes any signature/quoted text the caller wants to retain.
4. Read and review the final revision. `mail_draft_send` requires that revision,
   a stable request key and the existing send confirmation gate.

On older Thunderbird builds affected by
[the native compose startup bug](https://bugzilla.mozilla.org/show_bug.cgi?id=1925747),
reading still works. The tool reports `editable: false` and `sendable: false`;
a requested edit or send returns `UNSUPPORTED_CAPABILITY` before opening any
window. The fixed branches start at **128.4.3 ESR** and **133.0b3**.

The native Draft edit path preserves the original draft relationship, identity,
references and attachments. Edit-as-new is not a fallback. The helper closes only
its own successfully saved window. An already open user editor returns
`DRAFT_BUSY`; a changed saved revision returns `DRAFT_CONFLICT`. If a reference
cannot be uniquely resolved after an independent user save, locate the current
draft and read it by its new message id.
After a failed save, a recoverable helper window stays open with autosave paused;
the operation reports its tab and directs the user to review/save or close it.

Identity changes and format conversion are not supported. Protected drafts or
active encryption/signing are refused without disabling protection. Inline CID
images are preserved, but replacing their body or removing inline parts is
refused. Bodies above 200,000 characters return a limited preview with automation
disabled rather than being silently truncated and saved.

`mode="later"` means the Outbox, not scheduled delivery. Old send/reply/forward
tools also accept an optional request key, including when creating a draft.
Callers must reuse that key on retry;
a server-generated key cannot identify a separate new request. Uncertain sends
are not resent or turned into additional rescue drafts automatically.
Existing send/reply/forward calls without a request key also work with a 1.3
add-on. They are journalled, but the older runtime cannot reconcile a lost reply
with a receipt and retains its own native failure handling. Keyed delivery and
new saved-draft workflows require 1.4 support.

## Batch preview, apply and undo

`mail_batch_preview` freezes up to 500 unique messages for 15 minutes. It supports
one mark operation (read, flagged, tag additions/removals) or a same-account move
between real folders. Invalid items make the plan inapplicable; there is no silent
subset. Apply uses the frozen set, not a fresh search.

`mail_batch_apply(plan_id, confirm=true)` creates one background operation.
Repeated calls return that operation. Every step revalidates identity and touched
fields. Unrelated tag changes are retained. A move is correlated with native
events and verified in the destination; an unexpected copy is not reported as a
successful move.
Native moves await the copy service with Thunderbird's optimistic undo queue
disabled. The MCP journal owns undo. This avoids temporary IMAP identifiers on
Thunderbird 128; offline/pending native headers are refused before a new move.
Move preview, apply and undo require the `x.batch.move` capability.

`mail_batch_undo(operation_id, confirm=true)` creates one reverse operation for
verified successful steps. It changes only fields still matching the expected
post-state. Missing/ambiguous messages, unavailable folders or later user changes
are reported as conflicts. Unknown effects are not reversed speculatively.
Thunderbird/UI/IMAP does not offer atomic compare-and-swap across all actors;
these checks do not claim database-level isolation from external edits.

## Search and follow-up candidates

`search_global` and `search_conversation` support snapshot cursors. Continue with
only `cursor` and the desired page size; do not change the query or scope. Limits
are 1,000 items per snapshot, 16 snapshots, a 32 MiB estimated serialised-data
budget and five-minute expiry. Old sessions/evicted snapshots return an explicit
cursor error. Legacy offset is retained.

Coverage is explicit: indexed results are not proof of complete mailbox coverage.
Requested folders are not asserted to have been fully scanned. A candidate limit,
unavailable index, unmatchable term or incomplete conversation is reported.
`alternativeSearch` is a suggested call with changed semantics, not an automatic
fallback that silently rewrites the query.

`mail_followups` defaults to 30 days of outgoing mail and a seven-day wait. It
examines at most 500 outgoing candidates / 100 conversations within 60 seconds,
with at most four concurrent readers. Identities determine self addresses; real
reply headers determine relationships. Any human response closes a group
conversation, not each recipient independently. Known unsent mail, automatic
responses and list traffic are excluded. Results are candidates with evidence
and coverage, not a semantic assertion that the recipient owes a reply.

## Attachment exports

`mail_attachments_plan` takes explicit message ids and an existing absolute
directory. Filename-substring and exact-MIME filters select at most 200 files from
100 messages, at most 25 MiB each and 250 MiB total. A 15-minute plan fixes safe
filenames and collision suffixes. PDF contents/OCR are not searched.

`mail_attachments_save` returns one operation. Before dispatch the worker durably
records the validated attachment hash. Each save checks the source again and
atomically creates the frozen target without overwriting. A file appearing after
preview is a conflict, even if it happens to contain identical bytes. Only an
already dispatched uncertain item's reconciliation may accept a matching durable
hash. Cancellation retains completed files; there is no file-deleting undo.

## Validation and release gate

Run pytest, all Node tests, Ruff lint/format checks, the cross-layer consistency
check, XPI build, generated documentation consistency and complete toolset
registration. Live acceptance uses a separate profile and loopback-only mail
services. Never use a user's production account as test data.

Before publishing, record results for the supported minimum Thunderbird,
selected ESR and current release, and the existing platform/Python CI matrix.
Mixed server/add-on versions must fail new workflows before mutation while
preserving older supported tools. This source version is not itself a published
release; unresolved live acceptance failures block publication.

See [the validation record](VALIDATION-1.4.md) for the isolated fixtures and
commands used to reproduce the checks.

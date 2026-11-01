# Thunderbird MCP 1.4.0 validation

The native checks use fresh Thunderbird profiles and synthetic mail. Draft sends
go to a loopback SMTP receiver; move/undo uses a separate in-memory loopback IMAP
account. No production mailbox or external recipient is part of the test.

## Recorded results

All **453 Python tests** passed on Windows with Python **3.11.15, 3.13.15
and 3.14.6** (452 together, plus the final 500-message batch test on each).
On Ubuntu 26.04.1 under WSL, all 453 passed together with Python 3.14.4.
All **210 Node tests** passed. The catalogue contains 124 distinct tools, 54
available read-only. All 72 background methods and 77 privileged methods across
14 native modules matched their registrations.

| Thunderbird on Windows | Draft checks | Batch/events/export/IMAP checks | Search/follow-up checks |
| --- | ---: | ---: | ---: |
| 128.0 (minimum) | 9/9 guarded refusal | 30/30 | 26/26 |
| 128.4.3 ESR (first fixed 128 ESR) | 18/18 | — | — |
| 128.14.0 ESR | 18/18 | 30/30 | 26/26 |
| 153.4.0 ESR | 18/18 | 30/30 | 26/26 |
| 159.0 | 18/18 | 30/30 | 26/26 |

On exact 128.0 the native editor could stall during OpenPGP startup
([Mozilla bug 1925747](https://bugzilla.mozilla.org/show_bug.cgi?id=1925747#c20)).
The nine guard checks verified saved-draft reads, the advertised restriction,
and refusal of update/send/Outbox/native open with no new compose windows,
no changed folder counts or draft revision, and zero SMTP acceptances.
On each of the four fixed builds the full draft workflow delivered exactly one
message to the local SMTP receiver after replaying the send request. Dashes mean
that suite was not run on that particular build.

The IMAP fixture checks the real contents of both server folders **before undo**
and again after undo, as well as the native copy/store/expunge commands. This
caught an apparent local success on Thunderbird 128 that had attempted undo
using a temporary UID. The fix observes the native notification before dispatch,
awaits native completion, and verifies both source absence and the unique target
message after folder synchronization. Versions 153.4 and 159 also exercised
concurrent folder refresh while fetching destination headers.

## Reproduce the live checks

From an editable development installation, run each suite against an explicit
Thunderbird executable. The launcher creates a fresh directory under `build/`,
verifies the profile before privileged calls, installs this checkout's XPI and
closes only the fixture it started. Each invocation retains its JSON results.

```powershell
$tb = 'C:\Program Files\Mozilla Thunderbird\thunderbird.exe'
uv run python tools/live_profile.py --binary $tb --suite drafts
uv run python tools/live_profile.py --binary $tb --suite workflows
uv run python tools/live_profile.py --binary $tb --suite followups
```

| Suite | What it verifies | Report in the test directory |
| --- | --- | --- |
| [Drafts](../tools/smoke_drafts.py) | Real native editing; saved revision/body, HTML, signature, normal and inline attachments, protected/oversized drafts, busy window, Outbox, one SMTP acceptance after replay | `native-results.json` |
| [Workflows](../tools/smoke_workflows.py) | Mark/tag changes and undo, conflicts, local and IMAP moves, independent event cursors, attachment collisions/checksums and replay. The IMAP server verifies message contents after both the forward move and undo. | `workflow-results.json` |
| [Search/follow-ups](../tools/smoke_followups.py) | Actual indexed fixture messages, immutable pages across a new arrival, numeric term/disabled index explanations, related human/automatic replies, lists, groups, self mail and unsent messages | `search-followups-results.json` |

Fault injection in the Python and JavaScript suites covers outcomes that a
successful mail-server smoke test cannot establish: lost responses, late native
completion, restarting with running rows, duplicate keys, journal write failures,
stale/ambiguous references and a target file created between preview and save.
One subprocess test exits the daemon after a send reaches a synthetic receiver
and before a receipt is recorded: after restart the same key returns the original
unknown operation without another dispatch. Another applies the maximum
500-message plan once, replays it, then reads all 500 results in ten pages.
SMTP acceptance is evidence of submission to that test server, not delivery to an
arbitrary recipient.

## Package and repository checks

Run `pytest -q`, `node --test "tests/js/*.test.mjs"`, both Ruff checks and
`python tools/check_consistency.py`. `python tools/gen_tool_reference.py` must
reproduce the checked-in catalogue. Build the add-on with
`python tools/build_xpi.py build` and the source distribution/wheel with `uv build`.
The wheel carries the add-on source; live profiles, mail fixtures and downloaded
Thunderbird binaries are excluded from all distributions.

The platform/Python CI matrix remains in [ci.yml](../.github/workflows/ci.yml).
macOS and hosted CI were not run as part of this local validation. This is an
unreleased source version; local acceptance does not publish a release.

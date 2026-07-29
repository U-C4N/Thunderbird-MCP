"""Preferences, junk filtering and OpenPGP keys.

These are the widest-blast-radius tools in the server: a preference write reaches
`Services.prefs` with the system principal behind it. Three things narrow it —
`config.PREF_ALLOWLIST` decides what an agent may touch at all, `config.PREF_DENYLIST`
refuses credentials and transport security even under `--unsafe-prefs`, and the
privileged module re-checks its own hard denylist because that is the only layer with
real privilege. Checking here as well is not redundant: it turns a bridge round trip
into an immediate, explainable refusal.
"""

# NOTE: no `from __future__ import annotations` in toolset modules. Tool signatures
# must evaluate at definition time so `Gate(...)` produces a real
# `Annotated[Consent, Resolve(...)]` rather than a string the SDK has to re-evaluate.

from typing import Any, Literal

from ..errors import BlockedError, UsageError
from ..safety import (
    IDEMPOTENT_WRITE,
    MUTATING,
    Gate,
    current_settings,
    guard_write,
    large_output,
    require,
)
from ..server import Registrar
from ._common import call, changed, clamp, dry_run, one_of, page

# The curated intent → preference map behind `settings_describe`, kept as data so the
# tool body stays readable and so a wrong entry is a one-line fix.
#
# Rules for anything added here: only preferences confirmed to exist on Thunderbird
# 153, only value meanings that have been checked, and no defaults quoted as fact —
# defaults move between builds and locales, and `pref_get` is one call away. A wrong
# name sends the model off writing a preference nothing reads, which is worse than
# leaving the setting out.
_SETTINGS_MAP: tuple[dict[str, Any], ...] = (
    {
        "area": "Message list and layout",
        "settings": (
            {
                "pref": "mail.pane_config.dynamic",
                "type": "int",
                "what": "Where the message pane sits relative to the message list.",
                "values": "0 = classic (list on top, message below), 1 = wide (message "
                "below, full width), 2 = vertical (message to the right)",
                "restartNeeded": False,
                "note": "Existing windows may need reopening; new ones pick it up at once.",
            },
            {
                "pref": "mail.threadpane.listview",
                "type": "int",
                "what": "Whether the message list draws as cards or as a table.",
                "values": "0 = cards, 1 = table",
                "restartNeeded": False,
                "note": "Thunderbird 115 and later only.",
            },
            {
                "pref": "mail.uidensity",
                "type": "int",
                "what": "Spacing of the whole interface.",
                "values": "0 = compact, 1 = normal, 2 = relaxed",
                "restartNeeded": False,
            },
            {
                "pref": "mail.uifontsize",
                "type": "int",
                "what": "Interface font size, independent of message text.",
                "values": "0 = follow the operating system, otherwise a size in points "
                "(roughly 9 to 30)",
                "restartNeeded": False,
            },
            {
                "pref": "mail.showCondensedAddresses",
                "type": "bool",
                "what": "Show only the display name for people who are in an address book.",
                "values": "true or false",
                "restartNeeded": False,
            },
        ),
    },
    {
        "area": "Threading and sort defaults",
        "note": "These are the defaults for folders that have no saved view state of "
        "their own. A folder the user has already sorted keeps its own setting, so "
        "changing these will look like nothing happened on familiar folders.",
        "settings": (
            {
                "pref": "mailnews.default_sort_type",
                "type": "int",
                "what": "Column newly-opened folders sort by.",
                "values": "18 = date, 19 = subject, 20 = sender, 22 = thread, 24 = status, "
                "25 = size, 26 = starred, 30 = tags, 35 = received, 36 = correspondent",
                "restartNeeded": False,
            },
            {
                "pref": "mailnews.default_sort_order",
                "type": "int",
                "what": "Direction of that sort.",
                "values": "1 = ascending, 2 = descending",
                "restartNeeded": False,
            },
            {
                "pref": "mailnews.default_view_flags",
                "type": "int",
                "what": "Whether newly-opened folders are threaded.",
                "values": "0 = unthreaded, 1 = threaded",
                "restartNeeded": False,
                "note": "A bitfield: higher bits carry grouped-by-sort and unread-only. "
                "Read the current value first and leave bits you do not recognise alone.",
            },
        ),
    },
    {
        "area": "Reading",
        "settings": (
            {
                "pref": "mailnews.mark_message_read.auto",
                "type": "bool",
                "what": "Mark a message read when it is displayed at all.",
                "values": "true or false",
                "restartNeeded": False,
                "note": "Set false when the user wants reading state to be manual only; "
                "the delay preferences below then have no effect.",
            },
            {
                "pref": "mailnews.mark_message_read.delay",
                "type": "bool",
                "what": "Wait before marking a displayed message read.",
                "values": "true or false",
                "restartNeeded": False,
            },
            {
                "pref": "mailnews.mark_message_read.delay.interval",
                "type": "int",
                "what": "How long that wait is, in seconds.",
                "values": "whole seconds; only consulted when the delay preference is true",
                "restartNeeded": False,
            },
            {
                "pref": "mailnews.message_display.disable_remote_image",
                "type": "bool",
                "what": "Block remote images and other remote content in messages.",
                "values": "true blocks (the privacy-preserving setting), false loads",
                "restartNeeded": False,
            },
            {
                "pref": "font.size.variable.x-western",
                "type": "int",
                "what": "Message text size for proportional fonts, in points.",
                "values": "roughly 9 to 72",
                "restartNeeded": False,
            },
            {
                "pref": "font.size.fixed.x-western",
                "type": "int",
                "what": "Message text size for monospace fonts, in points.",
                "values": "roughly 9 to 72",
                "restartNeeded": False,
                "note": "Only visible where plain-text mail is shown in a fixed-width font.",
            },
        ),
    },
    {
        "area": "Composing",
        "settings": (
            {
                "pref": "mail.default_html_action",
                "type": "int",
                "what": "What to do when a recipient may not accept HTML.",
                "values": "0 = ask each time, 1 = send plain text only, 2 = send HTML only, "
                "3 = send both",
                "restartNeeded": False,
            },
            {
                "pref": "mail.identity.default.compose_html",
                "type": "bool",
                "what": "Whether new messages start in HTML rather than plain text.",
                "values": "true or false",
                "restartNeeded": False,
            },
            {
                "pref": "mail.forward_message_mode",
                "type": "int",
                "what": "Whether forwards quote the original inline or attach it.",
                "values": "0 = as attachment, 2 = inline",
                "restartNeeded": False,
                "note": "1 is a legacy value; do not set it.",
            },
            {
                "pref": "mail.compose.attachment_reminder",
                "type": "bool",
                "what": "Warn when a message mentions an attachment but has none.",
                "values": "true or false",
                "restartNeeded": False,
            },
            {
                "pref": "mail.compose.attachment_reminder_keywords",
                "type": "string",
                "what": "Comma-separated words that trigger that warning.",
                "values": 'e.g. "attachment,attached,enclosed,CV" — the shipped default '
                "is localised, so read it before replacing it",
                "restartNeeded": False,
            },
        ),
    },
    {
        "area": "Quoting and replies",
        "note": "mail.identity.default.* is only the fallback. A configured identity "
        "stores its own copy as mail.identity.id<N>.<key>, so use identity_set to change "
        "one account and expect the default to look ignored.",
        "settings": (
            {
                "pref": "mail.identity.default.auto_quote",
                "type": "bool",
                "what": "Quote the original message when replying.",
                "values": "true or false",
                "restartNeeded": False,
            },
            {
                "pref": "mail.identity.default.reply_on_top",
                "type": "int",
                "what": "Where the cursor starts relative to the quote.",
                "values": "0 = reply below the quote, 1 = reply above the quote, "
                "2 = start with the quote selected",
                "restartNeeded": False,
            },
            {
                "pref": "mail.identity.default.sig_bottom",
                "type": "bool",
                "what": "Whether the signature goes below the quoted text or above it.",
                "values": "true = below the quote, false = above it",
                "restartNeeded": False,
            },
        ),
    },
    {
        "area": "Spelling",
        "settings": (
            {
                "pref": "mail.spellcheck.inline",
                "type": "bool",
                "what": "Check spelling as the user types.",
                "values": "true or false",
                "restartNeeded": False,
            },
            {
                "pref": "spellchecker.dictionary",
                "type": "string",
                "what": "Which dictionary to spell-check against.",
                "values": 'a locale code such as "en-GB"; recent builds accept a '
                "comma-separated list",
                "restartNeeded": False,
                "note": "Only installed dictionaries take effect — an unknown locale "
                "silently disables checking.",
            },
        ),
    },
    {
        "area": "New-mail notification",
        "note": "How often each account checks is per-server, not global: "
        "mail.server.<key>.check_time (minutes) and mail.server.<key>.check_new_mail. "
        "Use account_get_server / account_set_server for those.",
        "settings": (
            {
                "pref": "mail.biff.show_alert",
                "type": "bool",
                "what": "Show an alert when new mail arrives.",
                "values": "true or false",
                "restartNeeded": False,
            },
            {
                "pref": "mail.biff.use_system_alert",
                "type": "bool",
                "what": "Use the operating system's notifications instead of Thunderbird's "
                "own alert window.",
                "values": "true or false",
                "restartNeeded": False,
                "note": "On Windows this routes new-mail alerts through Action Centre, "
                "so Focus Assist can then suppress them.",
            },
            {
                "pref": "mail.biff.play_sound",
                "type": "bool",
                "what": "Play a sound for new mail.",
                "values": "true or false",
                "restartNeeded": False,
            },
            {
                "pref": "mail.biff.play_sound.type",
                "type": "int",
                "what": "Which sound that is.",
                "values": "0 = system default, 1 = the file named by mail.biff.play_sound.url",
                "restartNeeded": False,
            },
            {
                "pref": "mail.biff.alert.show_preview",
                "type": "bool",
                "what": "Include a snippet of the message body in the alert.",
                "values": "true or false",
                "restartNeeded": False,
            },
        ),
    },
    {
        "area": "Return receipts",
        "note": "The per-case response policy lives under mail.mdn.report.* — list it "
        'with pref_list(prefix="mail.mdn.") rather than guessing the sub-names.',
        "settings": (
            {
                "pref": "mail.mdn.report.enabled",
                "type": "bool",
                "what": "Whether Thunderbird responds to return-receipt (MDN) requests.",
                "values": "true or false",
                "restartNeeded": False,
            },
            {
                "pref": "mail.identity.default.request_return_receipt",
                "type": "bool",
                "what": "Ask for a receipt on messages this user sends.",
                "values": "true or false",
                "restartNeeded": False,
            },
        ),
    },
    {
        "area": "Calendar",
        "settings": (
            {
                "pref": "calendar.timezone.local",
                "type": "string",
                "what": "Time zone every event is displayed in.",
                "values": 'an IANA zone id, e.g. "Europe/Istanbul" or "UTC"',
                "restartNeeded": True,
                "note": "Existing views keep the old zone until Thunderbird restarts.",
            },
            {
                "pref": "calendar.week.start",
                "type": "int",
                "what": "First day of the week in calendar views.",
                "values": "0 = Sunday through 6 = Saturday",
                "restartNeeded": False,
            },
            {
                "pref": "calendar.event.defaultlength",
                "type": "int",
                "what": "Length of a new event, in minutes.",
                "values": "whole minutes",
                "restartNeeded": False,
            },
            {
                "pref": "calendar.alarms.onforevents",
                "type": "int",
                "what": "Whether new events get a reminder by default.",
                "values": "0 = no reminder, 1 = add one",
                "restartNeeded": False,
                "note": 'An int, not a bool — writing type="bool" is refused.',
            },
            {
                "pref": "calendar.alarms.eventalarmlen",
                "type": "int",
                "what": "How long before an event that default reminder fires.",
                "values": "a count of the unit in calendar.alarms.eventalarmunit",
                "restartNeeded": False,
            },
            {
                "pref": "calendar.alarms.eventalarmunit",
                "type": "string",
                "what": "Unit for the default event reminder.",
                "values": '"minutes", "hours" or "days"',
                "restartNeeded": False,
            },
            {
                "pref": "calendar.alarms.onfortodos",
                "type": "int",
                "what": "Same default reminder, for tasks.",
                "values": "0 = no reminder, 1 = add one (length in calendar.alarms.todoalarmlen)",
                "restartNeeded": False,
            },
            {
                "pref": "calendar.view.daystarthour",
                "type": "int",
                "what": "First hour shown in day and week views.",
                "values": "0 to 23",
                "restartNeeded": False,
            },
            {
                "pref": "calendar.view.dayendhour",
                "type": "int",
                "what": "Last hour shown in day and week views.",
                "values": "0 to 23, above daystarthour",
                "restartNeeded": False,
            },
        ),
    },
    {
        "area": "Updates",
        "settings": (
            {
                "pref": "app.update.auto",
                "type": "bool",
                "what": "Install updates without asking.",
                "values": "true installs automatically, false asks first",
                "restartNeeded": False,
                "note": "Applies at the next update check, not immediately. A managed "
                "install can lock it — pref_get reports locked:true when that is the case.",
            },
        ),
    },
    {
        "area": "Startup",
        "note": "There is no preference that pins a folder to open at startup: "
        "Thunderbird restores whatever was selected when it last closed. Whether an "
        "account fetches mail at launch is per-server — "
        "mail.server.<key>.login_at_startup, via account_get_server.",
        "settings": (
            {
                "pref": "mailnews.start_page.enabled",
                "type": "bool",
                "what": "Show the start page in the message pane at launch.",
                "values": "true or false",
                "restartNeeded": False,
            },
            {
                "pref": "mailnews.start_page.url",
                "type": "string",
                "what": "Which page that is.",
                "values": "an absolute URL",
                "restartNeeded": False,
            },
        ),
    },
)


def register(reg: Registrar) -> None:
    # ---------------------------------------------------------------- reading prefs

    @reg.read_tool(title="Read a preference")
    async def pref_get(name: str) -> dict[str, Any]:
        """Read one Thunderbird preference.

        Reports the value, its type, whether the user has changed it from the default,
        and whether an enterprise policy has locked it. `writable` says whether this
        server would let you change it. Use `settings_describe` to find the name.
        """
        result = await call("x.prefs.get", {"name": _clean_name(name)})
        allowed, reason = current_settings().pref_writable(name)
        payload: dict[str, Any] = {**result, "writable": allowed}
        if not allowed:
            payload["whyNotWritable"] = reason
        return payload

    @reg.read_tool(title="Read several preferences")
    async def pref_get_many(names: list[str]) -> dict[str, Any]:
        """Read up to 100 preferences in one round trip.

        Preferences that do not exist come back with `exists: false` rather than
        failing the whole call, and are listed again under `missing`.
        """
        if not names:
            raise UsageError("names was empty — pass at least one preference name.")
        if len(names) > 100:
            raise UsageError(
                "pref_get_many takes at most 100 names; use pref_list with a prefix "
                "for anything broader."
            )
        result = await call("x.prefs.getMany", {"names": [_clean_name(n) for n in names]})
        prefs = result.get("prefs", [])
        return page(prefs, missing=[p.get("name") for p in prefs if not p.get("exists")])

    @reg.read_tool(title="List preferences by prefix", meta=large_output())
    async def pref_list(
        prefix: str = "",
        only_user_set: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        """List preferences under a branch, e.g. `prefix="mail.biff."`.

        A bare prefix matches over five thousand preferences on a normal profile, so
        pass a branch and keep `limit` modest. `only_user_set=true` narrows it to what
        differs from the shipped defaults.
        """
        result = await call(
            "x.prefs.list",
            {
                "prefix": prefix,
                "onlyUserSet": only_user_set,
                "limit": clamp(limit, default=200, minimum=1, maximum=2000, field="limit"),
            },
            timeout=60.0,
        )
        return page(
            result.get("prefs", []),
            total=result.get("matched"),
            truncated=bool(result.get("omitted")),
            prefix=result.get("prefix", prefix),
            omitted=result.get("omitted", 0),
        )

    @reg.read_tool(title="List changed preferences", meta=large_output())
    async def pref_user_set(prefix: str = "") -> dict[str, Any]:
        """Everything the user has changed from the shipped defaults.

        The honest answer to "what is my Thunderbird configured like": a normal profile
        has a few hundred of these, dominated by per-account `mail.server.*` and
        `mail.identity.*` entries. Anything that looks like a credential is redacted
        before it leaves Thunderbird.
        """
        result = await call("x.prefs.userSet", {"prefix": prefix}, timeout=60.0)
        return page(result.get("prefs", []), total=result.get("count"), prefix=prefix)

    @reg.read_tool(title="Describe common settings", meta=large_output())
    async def settings_describe(
        area: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        """Map a human request onto the preference that controls it.

        Consult this instead of guessing a preference name. It costs no round trip to
        Thunderbird, so it is cheap to call first. Value meanings are for Thunderbird
        153; current values are not included because they are per-profile — read one
        with `pref_get`. `writable` reflects this server's allowlist, not Thunderbird's
        own locking.
        """
        settings = current_settings()
        needle = (search or "").strip().lower()
        wanted_area = (area or "").strip().lower()

        groups: list[dict[str, Any]] = []
        for group in _SETTINGS_MAP:
            if wanted_area and wanted_area not in group["area"].lower():
                continue
            entries: list[dict[str, Any]] = []
            for entry in group["settings"]:
                if needle and needle not in f"{entry['pref']} {entry['what']}".lower():
                    continue
                allowed, reason = settings.pref_writable(entry["pref"])
                described: dict[str, Any] = {**entry, "writable": allowed}
                if not allowed:
                    described["whyNotWritable"] = reason
                entries.append(described)
            if entries:
                groups.append({**group, "settings": entries})

        known = [group["area"] for group in _SETTINGS_MAP]
        if not groups:
            raise UsageError(
                "Nothing matched. Areas are: "
                + "; ".join(known)
                + ". Drop `area`/`search` for the whole map, or use pref_list with a "
                "prefix to explore preferences this map does not cover."
            )
        return page(
            groups,
            settingsCount=sum(len(group["settings"]) for group in groups),
            areas=known,
            note="Curated, not exhaustive. pref_get(name) is the way to see a current "
            "value; pref_list(prefix=...) finds names this map omits.",
        )

    # ---------------------------------------------------------------- writing prefs

    @reg.write_tool(title="Change a preference", annotations=IDEMPOTENT_WRITE)
    async def pref_set(
        name: str,
        type: Literal["bool", "int", "string"],  # the wire contract's name; shadows a builtin
        value: bool | int | str,
        confirm: bool = False,
        consent: Gate("change a Thunderbird preference") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Change one Thunderbird preference.

        `type` must match the preference's existing type — pass `"int"` for a number
        even when it reads like a boolean (`calendar.alarms.onforevents` is the classic
        trap), and the call is refused with the right type if you get it wrong. Use
        `settings_describe` for the name and the allowed values, and `dry_run_only=true`
        to see the current value alongside what you propose.
        """
        guard_write("change preferences")
        name = _clean_name(name)
        _check_value_type(type, value)

        # Ask the local policy before spending a round trip: the refusal is the same
        # one the privileged module would give, and it can say which flag would help.
        allowed, reason = current_settings().pref_writable(name)
        if not allowed:
            raise BlockedError(
                reason,
                code="PREF_NOT_WRITABLE",
                # The allowlist is a policy the user can widen; the denylist is not, so
                # do not offer a flag that will still refuse.
                needs="--unsafe-prefs"
                if "allowlist" in reason
                else "a change made by hand in Thunderbird's settings",
            )

        if dry_run_only:
            before = await call("x.prefs.get", {"name": name})
            return dry_run(
                f"pref:{name}",
                {
                    "name": name,
                    "type": type,
                    "currentValue": before.get("value"),
                    "currentType": before.get("type"),
                    "proposedValue": value,
                },
            )

        require(consent, "change this preference")
        result = await call(
            "x.prefs.set", {"name": name, "type": type, "value": value}, timeout=30.0
        )
        return changed(
            f"pref:{name}",
            before=result.get("previous"),
            after=result.get("current"),
            name=name,
            restartRequired=result.get("restartRequired", False),
            note="Restart Thunderbird for this to take effect."
            if result.get("restartRequired")
            else "Applied live.",
        )

    @reg.write_tool(title="Reset a preference", annotations=IDEMPOTENT_WRITE)
    async def pref_reset(
        name: str,
        confirm: bool = False,
        consent: Gate("reset a Thunderbird preference to its default") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Clear a user-set preference so the shipped default applies again.

        The cleanest undo for a `pref_set` the user did not like — it removes the entry
        from `prefs.js` rather than writing the old value back. A preference already at
        its default is reported as unchanged instead of failing.
        """
        guard_write("reset preferences")
        name = _clean_name(name)
        allowed, reason = current_settings().pref_writable(name)
        if not allowed:
            raise BlockedError(reason, code="PREF_NOT_WRITABLE")
        require(consent, "reset this preference")
        result = await call("x.prefs.reset", {"name": name}, timeout=30.0)
        if not result.get("changed"):
            return {
                "changed": False,
                "name": name,
                "reason": result.get("reason", "already at its default value"),
            }
        return changed(
            f"pref:{name}",
            before=result.get("previous"),
            after=result.get("current"),
            name=name,
        )

    # ------------------------------------------------------------------------- junk

    @reg.read_tool(title="Get junk filter settings")
    async def junk_get() -> dict[str, Any]:
        """Read the global junk (bayesian) filter settings.

        `userHasClassified` is the one to check first: an untrained filter scores
        nothing usefully, however the rest is configured. Per-account junk behaviour —
        which folder spam moves to, whitelists, purge age — lives in `account_get_junk`.
        """
        result = await call("x.junk.getSettings", timeout=30.0)
        return {
            **result,
            "note": "Per-account junk handling is separate; see account_get_junk.",
        }

    @reg.write_tool(title="Change junk filter settings", annotations=IDEMPOTENT_WRITE)
    async def junk_set(
        manual_mark: bool | None = None,
        manual_mark_mode: Literal["move", "delete"] | None = None,
        mark_as_read_on_spam: bool | None = None,
        logging_enabled: bool | None = None,
        confirm: bool = False,
        consent: Gate("change Thunderbird's junk filter settings") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Change the global junk filter settings. Only the fields you pass are touched.

        `manual_mark_mode="delete"` makes marking a message as junk delete it outright,
        which is a real data-loss setting — prefer `"move"` unless the user asks for
        deletion by name.
        """
        guard_write("change junk settings")
        payload: dict[str, Any] = {}
        for key, value in (
            ("manualMark", manual_mark),
            ("markAsReadOnSpam", mark_as_read_on_spam),
            ("loggingEnabled", logging_enabled),
        ):
            if value is not None:
                payload[key] = value
        if manual_mark_mode is not None:
            payload["manualMarkMode"] = one_of(
                manual_mark_mode, ("move", "delete"), field="manual_mark_mode", default="move"
            )
        if not payload:
            raise UsageError(
                "Nothing to change — pass manual_mark, manual_mark_mode, "
                "mark_as_read_on_spam or logging_enabled."
            )
        require(consent, "change junk settings")
        result = await call("x.junk.setSettings", payload, timeout=30.0)
        return changed("junk.settings", before=result.get("previous"), after=result.get("current"))

    @reg.write_tool(title="Train the junk filter", annotations=MUTATING)
    async def junk_train(
        header_message_ids: list[str],
        classification: Literal["junk", "good"],
        confirm: bool = False,
        consent: Gate("train Thunderbird's junk filter on these messages") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Teach the junk filter that these messages are junk, or are not.

        Ids are RFC 5322 Message-ID header values (`headerMessageId` from `mail_get`),
        not the integer ids from `mail_search`: those are only valid while Thunderbird
        stays open, and training data outlives the session. Training is cumulative and
        biased corpora are hard to undo, so train on messages the user has actually
        judged rather than on a whole search result.
        """
        guard_write("train the junk filter")
        ids = [str(i).strip() for i in (header_message_ids or []) if str(i).strip()]
        if not ids:
            raise UsageError(
                "header_message_ids was empty — pass Message-ID header values, which "
                "mail_get returns as headerMessageId."
            )
        if len(ids) > 200:
            raise UsageError("junk_train takes at most 200 ids per call; batch them.")
        kind = one_of(classification, ("junk", "good"), field="classification", default="junk")
        if dry_run_only:
            return dry_run("junk.train", {"headerMessageIds": ids, "classification": kind})
        require(consent, "train the junk filter")
        result = await call(
            "x.junk.train",
            {"headerMessageIds": ids, "classification": kind},
            timeout=180.0,
        )
        return {
            "trained": result.get("trained", len(ids)),
            "classification": kind,
            "failures": result.get("failures") or [],
            "userHasClassified": result.get("userHasClassified"),
        }

    # ---------------------------------------------------------------------- openpgp

    @reg.read_tool(title="List OpenPGP keys")
    async def openpgp_list_keys(
        search: str | None = None,
        only_secret: bool = False,
    ) -> dict[str, Any]:
        """List the OpenPGP keys in Thunderbird's keyring.

        Metadata only — fingerprints, user ids, validity, expiry, and whether a secret
        key is present. Private key material is never exported through this server, and
        no tool here can export it; a user who wants a backup should use Thunderbird's
        own End-to-End Encryption settings. `search` matches an email address, key id or
        fingerprint; `only_secret=true` narrows to keys this user can sign with.
        """
        result = await call(
            "x.openpgp.listKeys",
            {"search": search, "onlySecret": only_secret},
            timeout=60.0,
        )
        return page(
            result.get("keys", []),
            total=result.get("totalAvailable"),
            secretKeysNote="Private key material is not exported.",
        )


# -------------------------------------------------------------------------- helpers


def _clean_name(name: str) -> str:
    """Reject the shapes that would otherwise reach Thunderbird as a silent no-op."""
    text = str(name or "").strip()
    if not text:
        raise UsageError("A preference name is required, e.g. mail.pane_config.dynamic.")
    if any(character.isspace() for character in text):
        raise UsageError(
            f"Preference names contain no whitespace; got {name!r}. Use the exact name "
            "from settings_describe or pref_list."
        )
    return text


def _check_value_type(declared: str, value: Any) -> None:
    """Catch a value that does not match its declared type before the bridge does.

    `bool` is a subclass of `int` in Python, so the order of these checks matters:
    accepting `true` for an int preference would write 1 and confuse everyone later.
    """
    if declared == "bool":
        if not isinstance(value, bool):
            raise UsageError(
                f'type="bool" needs true or false; got {value!r}. Pass '
                f'type="{"int" if isinstance(value, int) else "string"}" if that is '
                "really the preference's type."
            )
    elif declared == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise UsageError(f'type="int" needs a whole number, unquoted; got {value!r}.')
    elif declared == "string":
        if not isinstance(value, str):
            raise UsageError(f'type="string" needs a quoted string; got {value!r}.')
    else:
        raise UsageError(f'type must be "bool", "int" or "string"; got {declared!r}.')

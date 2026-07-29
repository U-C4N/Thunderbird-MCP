"""Calendars, events and tasks.

Thunderbird exposes no WebExtension calendar API, so every tool here talks to the
privileged half through `x.calendar.*`.

Two conventions run through the whole toolset. Dates are ISO-8601 in and out, and
every date that comes back carries the timezone it is stored in plus an `allDay`
flag. Recurring items are addressed as a whole series unless a tool is given
`occurrence_date`, and the result always says which of the two it acted on.
"""

# NOTE: no `from __future__ import annotations` in toolset modules — see mail.py.

import datetime as _dt
import re
from typing import Any, Literal

from ..errors import UsageError
from ..safety import DESTRUCTIVE, IDEMPOTENT_WRITE, MUTATING, Gate, guard_write, require
from ..server import Registrar
from ._common import call, changed, clamp, coerce_date, dry_run, page

CalendarType = Literal["storage", "memory", "ics", "caldav"]
EventStatus = Literal["TENTATIVE", "CONFIRMED", "CANCELLED"]
TaskStatus = Literal["NEEDS-ACTION", "IN-PROCESS", "COMPLETED", "CANCELLED"]
Privacy = Literal["PUBLIC", "PRIVATE", "CONFIDENTIAL"]

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NAMED_ADDRESS = re.compile(r"^(?P<name>.*?)\s*<(?P<email>[^<>]+)>$")


def _when(value: str | None, *, field: str, all_day: bool = False) -> str | None:
    """Normalise one calendar date, keeping all-day values date-only.

    `coerce_date` would expand `2026-07-29` to midnight, which the add-on cannot
    tell apart from a timed event that happens to start at 00:00.
    """
    if value is None or not str(value).strip():
        return None
    normalised = coerce_date(value, field=field)
    if normalised is None:
        return None
    if all_day or _DATE_ONLY.match(str(value).strip()):
        return normalised[:10]
    return normalised


def _attendees(values: list[str] | None, *, field: str = "attendees") -> list[dict[str, Any]]:
    """Accept `a@b.com` or `Alice <a@b.com>` and hand the bridge one shape."""
    out: list[dict[str, Any]] = []
    for entry in values or []:
        text = str(entry).strip()
        match = _NAMED_ADDRESS.match(text)
        email = (match.group("email") if match else text).strip()
        name = (match.group("name") or "").strip() if match else ""
        if "@" not in email or " " in email:
            raise UsageError(
                f"{field} entries must be email addresses, optionally as "
                f"'Alice <alice@example.com>'; got {entry!r}."
            )
        out.append({"email": email, "name": name or None})
    return out


def _priority(value: int | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or not 0 <= value <= 9:
        raise UsageError(
            "priority must be an integer from 0 (undefined) to 9; iCalendar treats "
            "1-4 as high, 5 as normal and 6-9 as low."
        )
    return value


def _window(from_date: str | None, to_date: str | None, days: int) -> tuple[str | None, str | None]:
    """Default a listing window to now → +`days`.

    Asking a calendar for everything it has is both slow and useless — the answer
    is dominated by last year's meetings — so an omitted window becomes a month
    from now rather than unbounded.
    """
    start = _when(from_date, field="from_date")
    end = _when(to_date, field="to_date")
    if start is None:
        start = _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()
    if end is None:
        try:
            anchor = _dt.datetime.fromisoformat(start)
        except ValueError:  # pragma: no cover - _when already validated it
            anchor = _dt.datetime.now().astimezone()
        end = (anchor + _dt.timedelta(days=days)).replace(microsecond=0).isoformat()
    return start, end


def _present(**fields: Any) -> dict[str, Any]:
    """Drop unsupplied fields: the add-on reads an absent key as "leave alone"."""
    return {key: value for key, value in fields.items() if value is not None}


def register(reg: Registrar) -> None:
    # ------------------------------------------------------------------ calendars

    @reg.read_tool(title="List calendars")
    async def calendar_list() -> dict[str, Any]:
        """List the user's calendars, with ids, types and whether each is writable.

        Every other tool here takes one of these ids. A calendar marked
        `readOnly` refuses writes; `disabled` ones are skipped when listing items.
        """
        result = await call("x.calendar.listCalendars", timeout=30.0)
        return page(
            result.get("calendars", []),
            defaultTimezone=result.get("defaultTimezone"),
        )

    @reg.write_tool(title="Create a calendar", annotations=MUTATING)
    async def calendar_create(
        name: str,
        type: CalendarType = "storage",
        url: str | None = None,
        color: str | None = None,
        confirm: bool = False,
        consent: Gate("create a calendar") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Create a calendar and register it with Thunderbird.

        `storage` keeps events in the local profile and needs no `url`; `ics` and
        `caldav` need the file or collection URL. `color` is `#RRGGBB`.
        """
        guard_write("create calendars")
        if type in ("ics", "caldav") and not url:
            raise UsageError(f"a {type} calendar needs url — the ICS file or CalDAV collection.")
        require(consent, "create this calendar")
        result = await call(
            "x.calendar.createCalendar",
            {"name": name, "type": type, **_present(uri=url, color=color)},
            timeout=60.0,
        )
        return changed("calendar", before=None, after=result.get("calendar"))

    @reg.write_tool(title="Change a calendar", annotations=IDEMPOTENT_WRITE)
    async def calendar_update(
        calendar_id: str,
        name: str | None = None,
        color: str | None = None,
        read_only: bool | None = None,
        disabled: bool | None = None,
        confirm: bool = False,
        consent: Gate("change a calendar's settings") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Rename or recolour a calendar, or toggle read-only and disabled.

        Only the fields you pass are touched. Nothing here affects the events
        inside the calendar.
        """
        guard_write("change calendars")
        fields = _present(name=name, color=color, readOnly=read_only, disabled=disabled)
        if not fields:
            raise UsageError("Nothing to change — pass name, color, read_only or disabled.")
        require(consent, "change this calendar")
        result = await call(
            "x.calendar.updateCalendar", {"calendarId": calendar_id, **fields}, timeout=45.0
        )
        return changed("calendar", before=result.get("previous"), after=result.get("current"))

    @reg.write_tool(title="Delete a calendar", annotations=DESTRUCTIVE)
    async def calendar_delete(
        calendar_id: str,
        unregister_only: bool = False,
        confirm: bool = False,
        consent: Gate("delete a calendar and everything in it") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Remove a calendar. Deletes its events and tasks with it.

        `unregister_only=true` is the reversible half: the calendar disappears
        from Thunderbird but its data is left where it is, ready to be added
        again. Prefer it unless the user asked for the data to go too.
        """
        guard_write("delete calendars")
        if dry_run_only:
            return dry_run(
                "x.calendar.deleteCalendar",
                {"calendarId": calendar_id, "unregisterOnly": unregister_only},
            )
        require(consent, "delete this calendar")
        result = await call(
            "x.calendar.deleteCalendar",
            {"calendarId": calendar_id, "unregisterOnly": unregister_only},
            timeout=90.0,
        )
        return changed(
            "calendar",
            before=result.get("previous"),
            after=None,
            dataDeleted=result.get("dataDeleted"),
            recoverable=not result.get("dataDeleted", True),
        )

    # --------------------------------------------------------------------- events

    @reg.read_tool(title="List calendar events")
    async def event_list(
        calendar_id: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        days: int = 30,
        include_occurrences: bool = True,
        limit: int = 50,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        """List events in a time window, soonest first.

        Defaults to the next 30 days across every enabled calendar; pass
        `from_date`/`to_date` (ISO-8601) for anything else, or `days` to widen the
        default window. `include_occurrences` expands recurring series into the
        individual meetings that fall inside the window, which is what you want
        for "what is on next week".
        """
        start, end = _window(from_date, to_date, clamp(days, default=30, minimum=1, maximum=1095))
        result = await call(
            "x.calendar.listItems",
            {
                "kind": "event",
                "from": start,
                "to": end,
                "limit": clamp(limit, default=50, minimum=1, maximum=500, field="limit"),
                "expandOccurrences": include_occurrences,
                **_present(calendarId=calendar_id, timezone=timezone),
            },
            timeout=90.0,
        )
        return page(
            result.get("items", []),
            total=result.get("matched"),
            truncated=bool(result.get("truncated")),
            window=result.get("window"),
            failures=result.get("failures") or [],
        )

    @reg.read_tool(title="Read one event or task")
    async def event_get(
        item_id: str,
        calendar_id: str | None = None,
        occurrence_date: str | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        """Read one event or task in full, including attendees and recurrence.

        Searches every calendar unless `calendar_id` narrows it. For a recurring
        series this returns the series plus its next few start times; pass
        `occurrence_date` to read a single occurrence instead.
        """
        result = await call(
            "x.calendar.getItem",
            {
                "itemId": item_id,
                **_present(
                    calendarId=calendar_id,
                    occurrenceDate=_when(occurrence_date, field="occurrence_date"),
                    timezone=timezone,
                ),
            },
            timeout=60.0,
        )
        return {
            "item": result.get("item"),
            "addressed": result.get("addressed"),
            "series": result.get("series"),
            "nextOccurrences": result.get("nextOccurrences"),
            "note": result.get("note"),
        }

    @reg.write_tool(title="Create an event", annotations=MUTATING)
    async def event_create(
        title: str,
        start: str,
        end: str | None = None,
        all_day: bool = False,
        duration_minutes: int | None = None,
        calendar_id: str | None = None,
        timezone: str | None = None,
        location: str | None = None,
        description: str | None = None,
        categories: list[str] | None = None,
        attendees: list[str] | None = None,
        organizer: str | None = None,
        recurrence_rule: str | None = None,
        url: str | None = None,
        status: EventStatus | None = None,
        privacy: Privacy | None = None,
        priority: int | None = None,
        confirm: bool = False,
        consent: Gate("create a calendar event") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Create an event. Omit `end` for a one-hour meeting.

        `start`/`end` are ISO-8601; a time with no offset is read as wall-clock
        time in `timezone` (the calendar's default if unset). For `all_day=true`
        pass plain dates — the end is exclusive per iCalendar, so omit it for a
        single day. `recurrence_rule` is an RRULE body such as
        `FREQ=WEEKLY;BYDAY=TU;COUNT=10`. Attendees are stored but no invitations
        are sent.
        """
        guard_write("create calendar events")
        if not str(title).strip():
            raise UsageError("title is required — an untitled event is invisible in the UI.")
        require(consent, "create this event")
        result = await call(
            "x.calendar.createEvent",
            {
                "title": title,
                "start": _when(start, field="start", all_day=all_day),
                "allDay": all_day,
                **_present(
                    end=_when(end, field="end", all_day=all_day),
                    durationMinutes=duration_minutes,
                    calendarId=calendar_id,
                    timezone=timezone,
                    location=location,
                    description=description,
                    categories=categories,
                    attendees=_attendees(attendees) or None,
                    organizer=(
                        _attendees([organizer], field="organizer")[0] if organizer else None
                    ),
                    recurrenceRule=recurrence_rule,
                    url=url,
                    status=status,
                    privacy=privacy,
                    priority=_priority(priority),
                ),
            },
            timeout=90.0,
        )
        return changed(
            "event",
            before=None,
            after=result.get("item"),
            calendarId=result.get("calendarId"),
            allDayEndIsExclusive=result.get("allDayEndIsExclusive"),
            note=result.get("note"),
        )

    @reg.write_tool(title="Change an event", annotations=MUTATING)
    async def event_update(
        item_id: str,
        calendar_id: str | None = None,
        occurrence_date: str | None = None,
        title: str | None = None,
        start: str | None = None,
        end: str | None = None,
        all_day: bool | None = None,
        timezone: str | None = None,
        location: str | None = None,
        description: str | None = None,
        categories: list[str] | None = None,
        attendees: list[str] | None = None,
        organizer: str | None = None,
        recurrence_rule: str | None = None,
        url: str | None = None,
        status: EventStatus | None = None,
        privacy: Privacy | None = None,
        priority: int | None = None,
        confirm: bool = False,
        consent: Gate("change a calendar event") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Change an event. Only the fields you pass are touched.

        For a recurring event this rewrites the **whole series**; pass
        `occurrence_date` (the start of the one you mean) to change a single
        occurrence and leave the rest alone. The result says which it did. Pass an
        empty string to clear `location`, `description` or `url`.
        """
        guard_write("change calendar events")
        fields = _present(
            title=title,
            start=_when(start, field="start", all_day=bool(all_day)),
            end=_when(end, field="end", all_day=bool(all_day)),
            allDay=all_day,
            calendarId=calendar_id,
            timezone=timezone,
            location=location,
            description=description,
            categories=categories,
            attendees=_attendees(attendees) if attendees is not None else None,
            organizer=(_attendees([organizer], field="organizer")[0] if organizer else None),
            recurrenceRule=recurrence_rule,
            url=url,
            status=status,
            privacy=privacy,
            priority=_priority(priority),
        )
        if not {k: v for k, v in fields.items() if k not in ("calendarId", "timezone")}:
            raise UsageError(
                "Nothing to change — pass at least one of title, start, end, all_day, "
                "location, description, categories, attendees, recurrence_rule, status, "
                "privacy or priority."
            )
        require(consent, "change this event")
        result = await call(
            "x.calendar.updateEvent",
            {
                "itemId": item_id,
                **fields,
                **_present(occurrenceDate=_when(occurrence_date, field="occurrence_date")),
            },
            timeout=90.0,
        )
        return changed(
            "event",
            before=result.get("previous"),
            after=result.get("item"),
            addressed=result.get("addressed"),
            note=result.get("note"),
        )

    @reg.write_tool(title="Delete an event or task", annotations=DESTRUCTIVE)
    async def event_delete(
        item_id: str,
        calendar_id: str | None = None,
        occurrence_date: str | None = None,
        timezone: str | None = None,
        confirm: bool = False,
        consent: Gate("delete a calendar item") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Delete an event or a task. Calendars have no trash, so this is final.

        For a recurring item this deletes the **entire series**. Pass
        `occurrence_date` to cancel one occurrence and keep the rest; the result
        says which it did. Read the item with `event_get` first if you are not
        sure it recurs.
        """
        guard_write("delete calendar items")
        occurrence = _when(occurrence_date, field="occurrence_date")
        if dry_run_only:
            return dry_run(
                "x.calendar.deleteItem",
                {"itemId": item_id, "calendarId": calendar_id, "occurrenceDate": occurrence},
            )
        require(consent, "delete this calendar item")
        result = await call(
            "x.calendar.deleteItem",
            {
                "itemId": item_id,
                **_present(calendarId=calendar_id, occurrenceDate=occurrence, timezone=timezone),
            },
            timeout=90.0,
        )
        return changed(
            "calendarItem",
            before=result.get("item") or result.get("occurrence"),
            after=result.get("series"),
            addressed=result.get("addressed"),
            recoverable=False,
            note=result.get("note"),
        )

    # ---------------------------------------------------------------------- tasks

    @reg.read_tool(title="List tasks")
    async def task_list(
        calendar_id: str | None = None,
        include_completed: bool = False,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 50,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        """List tasks, soonest due first. Completed ones are hidden by default.

        Unlike `event_list` this is not windowed by default: a task with no due
        date falls outside every range, and those are exactly the ones people
        forget. Pass `from_date`/`to_date` to narrow it.
        """
        result = await call(
            "x.calendar.listItems",
            {
                "kind": "task",
                "includeCompleted": include_completed,
                "limit": clamp(limit, default=50, minimum=1, maximum=500, field="limit"),
                **_present(
                    calendarId=calendar_id,
                    timezone=timezone,
                    **{
                        "from": _when(from_date, field="from_date"),
                        "to": _when(to_date, field="to_date"),
                    },
                ),
            },
            timeout=90.0,
        )
        return page(
            result.get("items", []),
            total=result.get("matched"),
            truncated=bool(result.get("truncated")),
            failures=result.get("failures") or [],
        )

    @reg.write_tool(title="Create a task", annotations=MUTATING)
    async def task_create(
        title: str,
        due: str | None = None,
        start: str | None = None,
        all_day: bool = False,
        calendar_id: str | None = None,
        timezone: str | None = None,
        description: str | None = None,
        location: str | None = None,
        categories: list[str] | None = None,
        percent_complete: int | None = None,
        completed: bool | None = None,
        status: TaskStatus | None = None,
        priority: int | None = None,
        recurrence_rule: str | None = None,
        confirm: bool = False,
        consent: Gate("create a task") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Create a task. Everything but the title is optional.

        `start` is the entry date and `due` the deadline, both ISO-8601. A task
        with neither still appears in Thunderbird's task list, which is where
        undated to-dos belong.
        """
        guard_write("create tasks")
        if not str(title).strip():
            raise UsageError("title is required.")
        require(consent, "create this task")
        result = await call(
            "x.calendar.createTask",
            {
                "title": title,
                "allDay": all_day,
                **_present(
                    due=_when(due, field="due", all_day=all_day),
                    start=_when(start, field="start", all_day=all_day),
                    calendarId=calendar_id,
                    timezone=timezone,
                    description=description,
                    location=location,
                    categories=categories,
                    percentComplete=percent_complete,
                    completed=completed,
                    status=status,
                    priority=_priority(priority),
                    recurrenceRule=recurrence_rule,
                ),
            },
            timeout=90.0,
        )
        return changed(
            "task", before=None, after=result.get("item"), calendarId=result.get("calendarId")
        )

    @reg.write_tool(title="Change a task", annotations=MUTATING)
    async def task_update(
        item_id: str,
        calendar_id: str | None = None,
        occurrence_date: str | None = None,
        title: str | None = None,
        due: str | None = None,
        start: str | None = None,
        all_day: bool | None = None,
        timezone: str | None = None,
        description: str | None = None,
        location: str | None = None,
        categories: list[str] | None = None,
        percent_complete: int | None = None,
        completed: bool | None = None,
        status: TaskStatus | None = None,
        priority: int | None = None,
        recurrence_rule: str | None = None,
        confirm: bool = False,
        consent: Gate("change a task") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Change a task, or tick it off with `completed=true`.

        Only the fields you pass are touched. `completed=true` also stamps the
        completion time and sets progress to 100%. For a repeating task this
        rewrites the series unless `occurrence_date` names one instance.
        """
        guard_write("change tasks")
        fields = _present(
            title=title,
            due=_when(due, field="due", all_day=bool(all_day)),
            start=_when(start, field="start", all_day=bool(all_day)),
            allDay=all_day,
            calendarId=calendar_id,
            timezone=timezone,
            description=description,
            location=location,
            categories=categories,
            percentComplete=percent_complete,
            completed=completed,
            status=status,
            priority=_priority(priority),
            recurrenceRule=recurrence_rule,
        )
        if not {k: v for k, v in fields.items() if k not in ("calendarId", "timezone")}:
            raise UsageError(
                "Nothing to change — pass at least one of title, due, start, completed, "
                "percent_complete, status, priority, description or categories."
            )
        if percent_complete is not None and not (
            isinstance(percent_complete, int) and 0 <= percent_complete <= 100
        ):
            raise UsageError("percent_complete must be an integer from 0 to 100.")
        require(consent, "change this task")
        result = await call(
            "x.calendar.updateTask",
            {
                "itemId": item_id,
                **fields,
                **_present(occurrenceDate=_when(occurrence_date, field="occurrence_date")),
            },
            timeout=90.0,
        )
        return changed(
            "task",
            before=result.get("previous"),
            after=result.get("item"),
            addressed=result.get("addressed"),
            note=result.get("note"),
        )

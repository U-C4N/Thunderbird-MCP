/* Calendar — calendars, events and tasks.
 *
 * Thunderbird ships no WebExtension calendar API, so every method here is
 * privileged. `cal` is calUtils (resource:///modules/calendar/calUtils.sys.mjs);
 * items are built with `new CalEvent()` / `new CalTodo()` because
 * `cal.createEvent` no longer exists on 153.
 *
 * Two rules run through the whole file:
 *   - every operation addresses the *series* unless the caller names an
 *     occurrence, and the result says which one it touched;
 *   - no calICalendar call is made without a deadline, because a provider that
 *     never answers would hold a bridge request open until the daemon gives up.
 */

TBX_MODULE_NAMES.push("calendar");

{
  /** Enough for a CalDAV round trip on a slow link, short enough that a wedged
   *  provider surfaces as an error rather than a hang. */
  const OP_TIMEOUT = 30000;

  const CALENDAR_TYPES = ["storage", "memory", "ics", "caldav"];
  const KINDS = ["event", "task", "all"];
  const MAILTO = /^mailto:/i;

  /** calUtils, looked up per call. `mod()` caches, and resolving lazily means a
   *  build without the calendar code fails one method instead of the add-on. */
  function cal() {
    return needMod("cal");
  }

  function manager() {
    const found = cal().manager;
    if (!found) {
      throw H.unsupported("this Thunderbird has no calendar manager, so calendars are unavailable");
    }
    return found;
  }

  /** Treat null like absent: the Python layer sends null for "leave alone", so an
   *  update must never blank a field the caller did not mention. */
  function has(params, name) {
    return params[name] !== undefined && params[name] !== null;
  }

  function arrayOf(value) {
    if (value === undefined || value === null) {
      return [];
    }
    return Array.isArray(value) ? value : [value];
  }

  function pad(value, width) {
    return String(Math.abs(value)).padStart(width, "0");
  }

  /* ------------------------------------------------------------------- dates */

  const ISO =
    /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$/;

  function timezoneFor(tzid) {
    const c = cal();
    if (!tzid) {
      return c.dtz.defaultTimezone;
    }
    const wanted = String(tzid).trim();
    if (wanted.toUpperCase() === "UTC") {
      return c.dtz.UTC;
    }
    let found = null;
    try {
      found = c.timezoneService.getTimezone(wanted);
    } catch (ex) {
      found = null;
    }
    if (!found) {
      throw H.usage(
        `unknown timezone ${wanted} — pass an IANA id such as Europe/Istanbul or UTC, ` +
          "or omit it to use Thunderbird's default"
      );
    }
    return found;
  }

  /** A mutable calIDateTime to reset. An explicit iCalendar value is passed
   *  because the zero-argument form of createDateTime is not part of the
   *  interface we want to depend on. */
  function blankDateTime() {
    return cal().createDateTime("19700101T000000Z");
  }

  function icalUtc(ms) {
    return new Date(ms).toISOString().replace(/[-:]/g, "").replace(/\.\d+/, "");
  }

  /**
   * ISO-8601 in, calIDateTime out. Three shapes matter and they mean different
   * things: a bare date is an all-day value (an iCalendar DATE, no zone), a
   * datetime with `Z` or an offset is an instant that we re-express in `tzid`,
   * and a naive datetime is wall-clock time *in* `tzid` — which is what someone
   * who writes "2026-07-29T09:00" means.
   */
  function toDateTime(value, tzid, allDay, field) {
    const text = String(value).trim();
    const parts = ISO.exec(text);
    if (!parts) {
      throw H.usage(
        `${field} must be ISO-8601 — 2026-07-29 for an all-day value, or ` +
          `2026-07-29T09:00:00 / 2026-07-29T09:00:00+03:00 for a time; got ${JSON.stringify(value)}`
      );
    }
    const [, year, month, day, hour, minute, second, offset] = parts;
    const zone = timezoneFor(tzid);
    if (hour === undefined || allDay) {
      const date = blankDateTime();
      date.resetTo(Number(year), Number(month) - 1, Number(day), 0, 0, 0, zone);
      date.isDate = true;
      return date;
    }
    if (offset) {
      const canonical =
        `${year}-${month}-${day}T${hour}:${minute}:${second || "00"}` +
        (offset === "Z" ? "Z" : offset.replace(/^([+-]\d{2}):?(\d{2})$/, "$1:$2"));
      const ms = Date.parse(canonical);
      if (Number.isNaN(ms)) {
        throw H.usage(`${field} has an offset this build cannot parse: ${JSON.stringify(value)}`);
      }
      return cal().createDateTime(icalUtc(ms)).getInTimezone(zone);
    }
    const local = blankDateTime();
    local.resetTo(
      Number(year),
      Number(month) - 1,
      Number(day),
      Number(hour),
      Number(minute),
      Number(second || 0),
      zone
    );
    return local;
  }

  function offsetSuffix(dt) {
    let zone = null;
    try {
      zone = dt.timezone;
    } catch (ex) {
      zone = null;
    }
    if (!zone || zone.isFloating) {
      return "";
    }
    if (zone.isUTC) {
      return "Z";
    }
    // Derive the offset from the instant rather than reading timezoneOffset, so
    // zones with historical or odd offsets still round-trip exactly.
    let seconds = 0;
    try {
      const wall = Date.UTC(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second) / 1000;
      seconds = Math.round(wall - dt.nativeTime / 1e6);
    } catch (ex) {
      seconds = 0;
    }
    const minutes = Math.abs(Math.round(seconds / 60));
    return `${seconds < 0 ? "-" : "+"}${pad(Math.floor(minutes / 60), 2)}:${pad(minutes % 60, 2)}`;
  }

  /** Every date leaves as ISO-8601 plus the zone it was stored in. All-day values
   *  keep the date-only form, because that is the difference the caller cares
   *  about. */
  function fromDateTime(dt) {
    if (!dt) {
      return null;
    }
    let tzid = null;
    try {
      tzid = dt.timezone ? dt.timezone.tzid : null;
    } catch (ex) {
      tzid = null;
    }
    const date = `${pad(dt.year, 4)}-${pad(dt.month + 1, 2)}-${pad(dt.day, 2)}`;
    if (dt.isDate) {
      return { dateTime: date, timezone: tzid, allDay: true };
    }
    const clock = `${pad(dt.hour, 2)}:${pad(dt.minute, 2)}:${pad(dt.second, 2)}`;
    return { dateTime: `${date}T${clock}${offsetSuffix(dt)}`, timezone: tzid, allDay: false };
  }

  function withAllDay(dt, allDay) {
    if (!dt) {
      return dt;
    }
    const copy = dt.clone();
    copy.isDate = Boolean(allDay);
    return copy;
  }

  /* --------------------------------------------------------- the async contract
   *
   * calICalendar has been migrating from calIOperationListener to promises over
   * several releases. On 153 the bundled providers return a promise from
   * addItem/modifyItem/deleteItem/getItem and a ReadableStream from getItems,
   * while a provider that has not been converted still returns a calIOperation
   * and reports through a listener. So we hand every call a listener *and* look
   * at what came back: whichever channel answers first settles the promise, and
   * H.withTimeout guarantees that one of them does. A listener that is never
   * invoked — because the provider was promise-based all along — is the single
   * easiest way to wedge a request in this file, which is why nothing below
   * calls calICalendar directly.
   */

  function succeeded(status) {
    // nsresult failures have the high bit set. Components.isSuccessCode is not
    // among the symbols injected into the experiment sandbox.
    return status === undefined || status === null || (status >>> 0) < 0x80000000;
  }

  function operate(what, invoke, timeoutMs) {
    return H.withTimeout(
      new Promise((resolve, reject) => {
        let settled = false;
        const collected = [];
        const done = (value) => {
          if (!settled) {
            settled = true;
            resolve(value);
          }
        };
        const failed = (error) => {
          if (!settled) {
            settled = true;
            reject(error instanceof Error ? error : new Error(String(error)));
          }
        };
        const listener = {
          QueryInterface: ChromeUtils.generateQI(["calIOperationListener"]),
          onGetResult(calendar, status, itemType, detail, items) {
            for (const item of items || []) {
              collected.push(item);
            }
          },
          onOperationComplete(calendar, status, opType, id, detail) {
            if (!succeeded(status)) {
              failed(new Error(`${what} failed with status 0x${(status >>> 0).toString(16)}`));
              return;
            }
            done(collected.length ? collected : detail);
          },
        };
        let returned;
        try {
          returned = invoke(listener);
        } catch (ex) {
          failed(ex);
          return;
        }
        if (returned && typeof returned.then === "function") {
          returned.then(done, failed);
        }
        // Anything else is a calIOperation: the listener above will settle us.
      }),
      timeoutMs || OP_TIMEOUT,
      what
    );
  }

  function flatten(value) {
    const out = [];
    const push = (item) => {
      if (item === null || item === undefined) {
        return;
      }
      if (Array.isArray(item)) {
        item.forEach(push);
        return;
      }
      out.push(item);
    };
    push(value);
    return out;
  }

  async function drain(stream) {
    const reader = stream.getReader();
    const chunks = [];
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      chunks.push(value);
    }
    return chunks;
  }

  /** Read items from one calendar, whichever shape it answers in. */
  async function fetchItems(calendar, filter, count, rangeStart, rangeEnd) {
    const what = `listing items in ${calendar.name || calendar.id}`;
    if (typeof calendar.getItemsAsArray === "function") {
      return flatten(
        await H.withTimeout(
          Promise.resolve(calendar.getItemsAsArray(filter, count, rangeStart, rangeEnd)),
          OP_TIMEOUT,
          what
        )
      );
    }
    return flatten(
      await operate(what, (listener) => {
        const returned = calendar.getItems(filter, count, rangeStart, rangeEnd, listener);
        // A converted provider hands back a stream of item batches; drain it so
        // the promise settles from the stream instead of from a listener that
        // this provider will never call.
        return returned && typeof returned.getReader === "function" ? drain(returned) : returned;
      })
    );
  }

  /* --------------------------------------------------------------- calendars */

  function calendars() {
    return manager().getCalendars() || [];
  }

  function property(calendar, name) {
    try {
      const value = calendar.getProperty(name);
      return value === undefined ? null : value;
    } catch (ex) {
      return null;
    }
  }

  function calendarInfo(calendar) {
    const c = cal();
    const info = {
      id: calendar.id,
      name: calendar.name,
      type: calendar.type,
      uri: calendar.uri ? calendar.uri.spec : null,
      readOnly: Boolean(calendar.readOnly),
      disabled: Boolean(property(calendar, "disabled")),
      color: property(calendar, "color"),
    };
    try {
      info.supportsEvents = c.item.isEventCalendar(calendar);
      info.supportsTasks = c.item.isTaskCalendar(calendar);
    } catch (ex) {
      // A provider without capability flags supports both, which is the default.
      info.supportsEvents = true;
      info.supportsTasks = true;
    }
    return info;
  }

  function calendarById(id) {
    const found = manager().getCalendarById(String(id));
    if (found) {
      return found;
    }
    const known = calendars()
      .map((calendar) => `${calendar.id} (${calendar.name})`)
      .join(", ");
    throw H.usage(`no calendar with id ${id} (known: ${known || "none"})`);
  }

  function assertWritable(calendar) {
    if (calendar.readOnly) {
      throw H.blocked(
        `calendar ${calendar.name} is read-only`,
        "a writable calendar, or calendar_update with read_only=false"
      );
    }
    return calendar;
  }

  /** The calendar to write to. Refuses to guess when more than one would do:
   *  filing an event in the wrong calendar is invisible until someone misses it. */
  function targetCalendar(id) {
    if (id) {
      return assertWritable(calendarById(id));
    }
    const writable = calendars().filter(
      (calendar) => !calendar.readOnly && !property(calendar, "disabled")
    );
    if (writable.length === 1) {
      return writable[0];
    }
    if (!writable.length) {
      throw H.usage("there is no writable calendar — create one with calendar_create first");
    }
    throw H.usage(
      `calendarId is required: ${writable.length} calendars could take this item (` +
        writable.map((calendar) => `${calendar.id} (${calendar.name})`).join(", ") +
        ")"
    );
  }

  /* ------------------------------------------------------------------- items */

  function startOf(item) {
    return item.startDate || item.entryDate || null;
  }

  function endOf(item) {
    return item.endDate || item.dueDate || null;
  }

  /** Events and tasks share calIItemBase, so tell them apart by the date fields
   *  only calITodo has. */
  function kindOf(item) {
    if (typeof item.isTodo === "function") {
      try {
        return item.isTodo() ? "task" : "event";
      } catch (ex) {
        /* fall through to duck typing */
      }
    }
    return "dueDate" in item || "entryDate" in item ? "task" : "event";
  }

  function text(item, name) {
    try {
      const value = item.getProperty(name);
      return value === undefined || value === null || value === "" ? null : String(value);
    } catch (ex) {
      return null;
    }
  }

  function setText(item, name, value) {
    const wanted = value === undefined || value === null ? "" : String(value);
    if (!wanted) {
      // An empty string is how a caller clears a field; drop the property rather
      // than storing a blank one.
      try {
        item.deleteProperty(name);
      } catch (ex) {
        item.setProperty(name, null);
      }
      return;
    }
    item.setProperty(name, wanted);
  }

  function categoriesOf(item) {
    try {
      return [...(item.getCategories() || [])];
    } catch (ex) {
      try {
        return [...(item.getCategories({}) || [])];
      } catch (inner) {
        return [];
      }
    }
  }

  function setCategories(item, values) {
    const list = arrayOf(values).map((value) => String(value));
    try {
      item.setCategories(list);
    } catch (ex) {
      item.setCategories(list.length, list);
    }
  }

  function attendeeOut(attendee) {
    if (!attendee) {
      return null;
    }
    const id = String(attendee.id || "");
    return {
      email: id.replace(MAILTO, ""),
      name: attendee.commonName || null,
      role: attendee.role || null,
      status: attendee.participationStatus || null,
      isOrganizer: Boolean(attendee.isOrganizer),
    };
  }

  function attendeesOf(item) {
    try {
      return [...(item.getAttendees() || [])];
    } catch (ex) {
      try {
        return [...(item.getAttendees({}) || [])];
      } catch (inner) {
        return [];
      }
    }
  }

  function newAttendee() {
    try {
      return Cc["@mozilla.org/calendar/attendee;1"].createInstance(Ci.calIAttendee);
    } catch (ex) {
      throw H.unsupported(
        "this build does not expose calIAttendee, so attendees cannot be set through the bridge"
      );
    }
  }

  function attendeeFrom(spec, isOrganizer) {
    const value = typeof spec === "string" ? { email: spec } : spec || {};
    const email = String(value.email || "").trim();
    if (!email) {
      throw H.usage("every attendee needs an email address");
    }
    const attendee = newAttendee();
    attendee.id = MAILTO.test(email) ? email : `mailto:${email}`;
    if (value.name) {
      attendee.commonName = String(value.name);
    }
    attendee.role = String(value.role || (isOrganizer ? "CHAIR" : "REQ-PARTICIPANT")).toUpperCase();
    attendee.participationStatus = String(value.status || "NEEDS-ACTION").toUpperCase();
    attendee.rsvp = value.rsvp === false ? "FALSE" : "TRUE";
    attendee.isOrganizer = Boolean(isOrganizer);
    return attendee;
  }

  function replaceAttendees(item, values) {
    try {
      item.removeAllAttendees();
    } catch (ex) {
      for (const attendee of attendeesOf(item)) {
        item.removeAttendee(attendee);
      }
    }
    for (const spec of arrayOf(values)) {
      item.addAttendee(attendeeFrom(spec, false));
    }
  }

  function newRecurrenceInfo() {
    try {
      return Cc["@mozilla.org/calendar/recurrence-info;1"].createInstance(Ci.calIRecurrenceInfo);
    } catch (ex) {
      throw H.unsupported(
        "this build does not expose calIRecurrenceInfo, so recurrence cannot be set through the bridge"
      );
    }
  }

  function applyRecurrence(item, rule) {
    const wanted = String(rule === undefined || rule === null ? "" : rule).trim();
    if (!wanted) {
      item.recurrenceInfo = null;
      return;
    }
    const info = newRecurrenceInfo();
    info.item = item;
    const recurrenceRule = cal().createRecurrenceRule();
    try {
      recurrenceRule.icalString = /^rrule[:;]/i.test(wanted) ? wanted : `RRULE:${wanted}`;
    } catch (ex) {
      throw H.usage(
        `recurrenceRule is not a valid RRULE: ${wanted} — for example ` +
          "FREQ=WEEKLY;BYDAY=TU;COUNT=10"
      );
    }
    info.appendRecurrenceItem(recurrenceRule);
    item.recurrenceInfo = info;
  }

  function rulesOf(item) {
    if (!item.recurrenceInfo) {
      return [];
    }
    try {
      return [...(item.recurrenceInfo.getRecurrenceItems() || [])]
        .map((rule) => {
          try {
            return String(rule.icalString).trim();
          } catch (ex) {
            return null;
          }
        })
        .filter(Boolean);
    } catch (ex) {
      return [];
    }
  }

  function nextOccurrences(item, count) {
    if (!item.recurrenceInfo) {
      return [];
    }
    try {
      const from = cal().dtz.now();
      const to = from.clone();
      to.year += 1;
      return [...(item.recurrenceInfo.getOccurrences(from, to, count) || [])].map((occurrence) =>
        fromDateTime(startOf(occurrence))
      );
    } catch (ex) {
      return [];
    }
  }

  function stamp(prTime) {
    try {
      return prTime ? new Date(prTime / 1000).toISOString() : null;
    } catch (ex) {
      return null;
    }
  }

  function describe(item) {
    const kind = kindOf(item);
    const start = startOf(item);
    const payload = {
      id: item.id,
      kind,
      calendarId: item.calendar ? item.calendar.id : null,
      calendarName: item.calendar ? item.calendar.name : null,
      title: item.title || null,
      start: fromDateTime(start),
      end: fromDateTime(endOf(item)),
      allDay: Boolean(start && start.isDate),
      location: text(item, "LOCATION"),
      description: text(item, "DESCRIPTION"),
      url: text(item, "URL"),
      categories: categoriesOf(item),
      status: item.status || null,
      privacy: item.privacy || null,
      priority: Number.isInteger(item.priority) ? item.priority : null,
      organizer: attendeeOut(item.organizer),
      attendees: attendeesOf(item).map(attendeeOut),
      recurring: Boolean(item.recurrenceInfo),
      recurrenceRules: rulesOf(item),
      isOccurrence: Boolean(item.recurrenceId),
      occurrenceDate: fromDateTime(item.recurrenceId),
      lastModified: stamp(item.lastModifiedTime),
    };
    if (kind === "task") {
      payload.percentComplete = Number.isInteger(item.percentComplete) ? item.percentComplete : 0;
      payload.completed = fromDateTime(item.completedDate);
      payload.isCompleted = Boolean(item.isCompleted);
    }
    return payload;
  }

  /** Fields events and tasks share. Only keys the caller sent are touched. */
  function applyCommon(item, params) {
    if (has(params, "title")) {
      item.title = String(params.title);
    }
    if (params.description !== undefined) {
      setText(item, "DESCRIPTION", params.description);
    }
    if (params.location !== undefined) {
      setText(item, "LOCATION", params.location);
    }
    if (params.url !== undefined) {
      setText(item, "URL", params.url);
    }
    if (has(params, "categories")) {
      setCategories(item, params.categories);
    }
    if (has(params, "status")) {
      item.status = String(params.status).toUpperCase();
    }
    if (has(params, "privacy")) {
      item.privacy = String(params.privacy).toUpperCase();
    }
    if (has(params, "priority")) {
      if (!Number.isInteger(params.priority) || params.priority < 0 || params.priority > 9) {
        throw H.usage("priority must be an integer from 0 (undefined) to 9 (lowest)");
      }
      item.priority = params.priority;
    }
    if (has(params, "organizer")) {
      item.organizer = attendeeFrom(params.organizer, true);
    }
    if (params.attendees !== undefined) {
      replaceAttendees(item, params.attendees);
    }
    if (params.recurrenceRule !== undefined) {
      applyRecurrence(item, params.recurrenceRule);
    }
  }

  /**
   * Narrow a series to one occurrence, or refuse to guess. Silently rewriting a
   * whole series because the caller meant next Tuesday is the worst thing this
   * module could do, so every path that accepts occurrenceDate comes through
   * here and every result says what it addressed.
   */
  function occurrenceOf(item, value, tzid) {
    if (!item.recurrenceInfo) {
      throw H.usage(
        `item ${item.id} does not recur, so occurrenceDate does not apply — omit it`
      );
    }
    const when = toDateTime(value, tzid, false, "occurrenceDate");
    let exact = null;
    try {
      exact = item.recurrenceInfo.getOccurrenceFor(when);
    } catch (ex) {
      // A DATE value against a timed series throws; the day scan below covers it.
      exact = null;
    }
    if (exact) {
      return exact;
    }
    const from = when.clone();
    from.isDate = false;
    from.hour = 0;
    from.minute = 0;
    from.second = 0;
    const to = from.clone();
    to.day += 1;
    const matches = [...(item.recurrenceInfo.getOccurrences(from, to, 3) || [])];
    if (matches.length === 1) {
      return matches[0];
    }
    if (matches.length > 1) {
      throw H.usage(
        `${value} has ${matches.length} occurrences of ${item.id} — pass the exact start time, ` +
          `for example ${(fromDateTime(startOf(matches[0])) || {}).dateTime}`
      );
    }
    throw H.usage(
      `${value} is not an occurrence of ${item.id} — list the series with ` +
        "event_list to see its real start times"
    );
  }

  /** Find one item by id, in a named calendar or across all of them. */
  async function findItem(calendarId, itemId) {
    const targets = calendarId ? [calendarById(calendarId)] : calendars();
    const problems = [];
    for (const calendar of targets) {
      let found = null;
      try {
        found = flatten(
          await operate(`reading ${itemId} from ${calendar.name || calendar.id}`, (listener) =>
            calendar.getItem(String(itemId), listener)
          )
        )[0];
      } catch (ex) {
        // One unreachable calendar must not hide an item that lives in another.
        problems.push(`${calendar.name}: ${ex.message || ex}`);
        continue;
      }
      if (found) {
        return found;
      }
    }
    throw H.usage(
      `no calendar item with id ${itemId}` +
        (calendarId ? ` in calendar ${calendarId}` : " in any calendar") +
        (problems.length ? ` (${problems.join("; ")})` : "")
    );
  }

  function itemFilter(kind, params) {
    const flags = Ci.calICalendar;
    if (!flags) {
      throw H.unsupported("calICalendar is unavailable, so items cannot be queried");
    }
    let filter = 0;
    if (kind === "event") {
      filter |= flags.ITEM_FILTER_TYPE_EVENT;
    } else if (kind === "task") {
      filter |= flags.ITEM_FILTER_TYPE_TODO;
    } else {
      // Deliberately not ITEM_FILTER_TYPE_ALL: that also asks for journals,
      // which we have no shape for.
      filter |= flags.ITEM_FILTER_TYPE_EVENT | flags.ITEM_FILTER_TYPE_TODO;
    }
    if (kind !== "event") {
      filter |= params.includeCompleted
        ? flags.ITEM_FILTER_COMPLETED_ALL
        : flags.ITEM_FILTER_COMPLETED_NO;
    }
    // Without CLASS_OCCURRENCES a weekly meeting answers once, at its first
    // start, so a window that does not contain that start looks empty.
    if (params.expandOccurrences !== false) {
      filter |= flags.ITEM_FILTER_CLASS_OCCURRENCES;
    }
    return filter;
  }

  function startKey(item) {
    const start = startOf(item);
    if (!start) {
      return Number.MAX_SAFE_INTEGER; // undated tasks sort last
    }
    try {
      return start.nativeTime;
    } catch (ex) {
      return Number.MAX_SAFE_INTEGER;
    }
  }

  /* ---------------------------------------------------------------- handlers */

  TBX_MODULES["calendar.listCalendars"] = async () => {
    const defaultZone = cal().dtz.defaultTimezone;
    return {
      defaultTimezone: defaultZone ? defaultZone.tzid : null,
      calendars: calendars().map(calendarInfo),
    };
  };

  TBX_MODULES["calendar.createCalendar"] = async (params) => {
    const name = H.need(params, "name");
    const type = String(params.type || "storage").toLowerCase();
    if (!CALENDAR_TYPES.includes(type)) {
      throw H.usage(`type must be one of ${CALENDAR_TYPES.join(", ")}; got ${type}`);
    }
    let target = params.uri ? String(params.uri) : null;
    if (!target && type === "storage") {
      target = "moz-storage-calendar://";
    }
    if (!target && type === "memory") {
      target = "moz-memory-calendar://";
    }
    if (!target) {
      throw H.usage(`a ${type} calendar needs uri — the ICS file or CalDAV collection URL`);
    }
    let uri;
    try {
      uri = Services.io.newURI(target);
    } catch (ex) {
      throw H.usage(`uri is not a valid URL: ${target}`);
    }
    const registry = manager();
    const calendar = registry.createCalendar(type, uri);
    if (!calendar) {
      throw H.unsupported(`this build has no ${type} calendar provider`);
    }
    calendar.name = String(name);
    registry.registerCalendar(calendar);
    if (has(params, "color")) {
      calendar.setProperty("color", String(params.color));
    }
    // Report what the manager actually stored rather than what we asked for: a
    // provider is free to normalise the URI or refuse a name.
    return { created: true, calendar: calendarInfo(calendar) };
  };

  TBX_MODULES["calendar.updateCalendar"] = async (params) => {
    const calendar = calendarById(H.need(params, "calendarId"));
    const before = calendarInfo(calendar);
    if (has(params, "name")) {
      calendar.name = String(params.name);
    }
    if (has(params, "color")) {
      calendar.setProperty("color", String(params.color));
    }
    if (has(params, "readOnly")) {
      calendar.readOnly = Boolean(params.readOnly);
    }
    if (has(params, "disabled")) {
      calendar.setProperty("disabled", Boolean(params.disabled));
    }
    return { previous: before, current: calendarInfo(calendar) };
  };

  TBX_MODULES["calendar.deleteCalendar"] = async (params) => {
    const calendar = calendarById(H.need(params, "calendarId"));
    const before = calendarInfo(calendar);
    const registry = manager();
    if (params.unregisterOnly) {
      // Unregistering hides the calendar and leaves its data where it is, which
      // is the only reversible half of this operation.
      registry.unregisterCalendar(calendar);
      return { removed: true, dataDeleted: false, previous: before };
    }
    await H.withTimeout(
      Promise.resolve(registry.removeCalendar(calendar)),
      OP_TIMEOUT,
      `removing calendar ${before.name}`
    );
    return { removed: true, dataDeleted: true, previous: before };
  };

  TBX_MODULES["calendar.listItems"] = async (params) => {
    const kind = String(params.kind || "event").toLowerCase();
    if (!KINDS.includes(kind)) {
      throw H.usage(`kind must be one of ${KINDS.join(", ")}; got ${kind}`);
    }
    const tzid = params.timezone;
    const from = has(params, "from") ? toDateTime(params.from, tzid, false, "from") : null;
    const to = has(params, "to") ? toDateTime(params.to, tzid, false, "to") : null;
    const limit = Number.isInteger(params.limit) && params.limit > 0 ? params.limit : 100;
    const filter = itemFilter(kind, params);
    const targets = params.calendarId
      ? [calendarById(params.calendarId)]
      : calendars().filter((calendar) => !property(calendar, "disabled"));

    const collected = [];
    const failures = [];
    for (const calendar of targets) {
      try {
        // Ask each calendar for one more than we need, so `truncated` is honest.
        const found = await fetchItems(calendar, filter, limit + 1, from, to);
        for (const item of found) {
          collected.push(item);
        }
      } catch (ex) {
        failures.push({
          calendarId: calendar.id,
          calendarName: calendar.name,
          error: String(ex.message || ex),
        });
      }
    }
    collected.sort((a, b) => startKey(a) - startKey(b));
    return {
      items: collected.slice(0, limit).map(describe),
      truncated: collected.length > limit,
      matched: collected.length,
      failures,
      kind,
      window: { from: fromDateTime(from), to: fromDateTime(to) },
      calendarsSearched: targets.map((calendar) => calendar.id),
    };
  };

  TBX_MODULES["calendar.getItem"] = async (params) => {
    const itemId = H.need(params, "itemId");
    const item = await findItem(params.calendarId, itemId);
    if (has(params, "occurrenceDate")) {
      const occurrence = occurrenceOf(item, params.occurrenceDate, params.timezone);
      return { addressed: "occurrence", item: describe(occurrence), series: describe(item) };
    }
    const payload = { addressed: item.recurrenceInfo ? "series" : "item", item: describe(item) };
    if (item.recurrenceInfo) {
      payload.nextOccurrences = nextOccurrences(item, 5);
      payload.note =
        "This is a recurring series. Pass occurrenceDate to read or change a single occurrence.";
    }
    return payload;
  };

  TBX_MODULES["calendar.createEvent"] = async (params) => {
    const CalEvent = needMod("CalEvent");
    const calendar = targetCalendar(params.calendarId);
    const tzid = params.timezone;
    const allDay = Boolean(params.allDay);
    const event = new CalEvent();
    event.id = cal().getUUID();
    event.calendar = calendar;
    event.title = String(H.need(params, "title"));

    const start = toDateTime(H.need(params, "start"), tzid, allDay, "start");
    let end;
    if (has(params, "end")) {
      end = toDateTime(params.end, tzid, allDay, "end");
    } else {
      end = start.clone();
      if (allDay) {
        end.day += 1;
      } else {
        end.minute += Number.isInteger(params.durationMinutes) ? params.durationMinutes : 60;
      }
    }
    const order = end.compare(start);
    if (order < 0) {
      throw H.usage("end must not be before start");
    }
    if (order === 0) {
      if (!allDay) {
        throw H.usage("end must be after start, or omit it for a one-hour event");
      }
      // An all-day DTEND is exclusive, so end == start would cover no days at
      // all. Someone passing the same date for both means that one day.
      end.day += 1;
    }
    event.startDate = start;
    event.endDate = end;
    applyCommon(event, params);

    const stored = flatten(
      await operate(`adding an event to ${calendar.name}`, (listener) =>
        calendar.addItem(event, listener)
      )
    )[0];
    const result = {
      created: true,
      calendarId: calendar.id,
      item: describe(stored || event),
    };
    if (allDay) {
      // iCalendar all-day ends are exclusive; say so rather than let a caller
      // conclude the event lost a day.
      result.allDayEndIsExclusive = true;
    }
    if (attendeesOf(stored || event).length) {
      result.note =
        "Attendees were stored on the event, but no invitations were sent — " +
        "open the event in Thunderbird to send them.";
    }
    return result;
  };

  TBX_MODULES["calendar.createTask"] = async (params) => {
    const CalTodo = needMod("CalTodo");
    const calendar = targetCalendar(params.calendarId);
    const tzid = params.timezone;
    const allDay = Boolean(params.allDay);
    const todo = new CalTodo();
    todo.id = cal().getUUID();
    todo.calendar = calendar;
    todo.title = String(H.need(params, "title"));
    if (has(params, "start")) {
      todo.entryDate = toDateTime(params.start, tzid, allDay, "start");
    }
    if (has(params, "due")) {
      todo.dueDate = toDateTime(params.due, tzid, allDay, "due");
    }
    applyTaskState(todo, params);
    applyCommon(todo, params);

    const stored = flatten(
      await operate(`adding a task to ${calendar.name}`, (listener) =>
        calendar.addItem(todo, listener)
      )
    )[0];
    return { created: true, calendarId: calendar.id, item: describe(stored || todo) };
  };

  function applyTaskState(todo, params) {
    if (has(params, "percentComplete")) {
      const percent = params.percentComplete;
      if (!Number.isInteger(percent) || percent < 0 || percent > 100) {
        throw H.usage("percentComplete must be an integer from 0 to 100");
      }
      todo.percentComplete = percent;
    }
    if (has(params, "completed")) {
      // isCompleted is what Thunderbird's task list reads; its setter also
      // stamps COMPLETED and moves percentComplete, so set it last.
      todo.isCompleted = Boolean(params.completed);
    }
  }

  /** Shared body of updateEvent / updateTask: read, clone, mutate, modifyItem. */
  async function updateItem(params, kind) {
    const itemId = H.need(params, "itemId");
    const existing = await findItem(params.calendarId, itemId);
    const calendar = existing.calendar || calendarById(H.need(params, "calendarId"));
    assertWritable(calendar);

    const addressed = has(params, "occurrenceDate")
      ? "occurrence"
      : existing.recurrenceInfo
        ? "series"
        : "item";
    const target =
      addressed === "occurrence"
        ? occurrenceOf(existing, params.occurrenceDate, params.timezone)
        : existing;
    const clone = target.clone();
    const tzid = params.timezone;
    const currentStart = startOf(target);
    const allDay = has(params, "allDay")
      ? Boolean(params.allDay)
      : Boolean(currentStart && currentStart.isDate);

    if (kind === "event") {
      if (has(params, "start")) {
        clone.startDate = toDateTime(params.start, tzid, allDay, "start");
      } else if (has(params, "allDay")) {
        clone.startDate = withAllDay(clone.startDate, allDay);
      }
      if (has(params, "end")) {
        clone.endDate = toDateTime(params.end, tzid, allDay, "end");
      } else if (has(params, "allDay")) {
        clone.endDate = withAllDay(clone.endDate, allDay);
      }
      if (clone.startDate && clone.endDate && clone.endDate.compare(clone.startDate) < 0) {
        throw H.usage("end must not be before start");
      }
    } else {
      if (has(params, "start")) {
        clone.entryDate = toDateTime(params.start, tzid, allDay, "start");
      }
      if (has(params, "due")) {
        clone.dueDate = toDateTime(params.due, tzid, allDay, "due");
      }
      applyTaskState(clone, params);
    }
    applyCommon(clone, params);

    const stored = flatten(
      await operate(`updating ${itemId} in ${calendar.name}`, (listener) =>
        calendar.modifyItem(clone, target, listener)
      )
    )[0];
    const result = {
      updated: true,
      addressed,
      calendarId: calendar.id,
      previous: describe(target),
      item: describe(stored || clone),
    };
    if (addressed === "series") {
      result.note =
        "The change applies to every occurrence of this series. Pass occurrence_date " +
        "to change a single one.";
    } else if (addressed === "occurrence") {
      result.note = "Only this occurrence changed; the rest of the series is unaffected.";
    }
    return result;
  }

  TBX_MODULES["calendar.updateEvent"] = async (params) => updateItem(params, "event");
  TBX_MODULES["calendar.updateTask"] = async (params) => updateItem(params, "task");

  TBX_MODULES["calendar.deleteItem"] = async (params) => {
    const itemId = H.need(params, "itemId");
    const existing = await findItem(params.calendarId, itemId);
    const calendar = existing.calendar || calendarById(H.need(params, "calendarId"));
    assertWritable(calendar);

    if (has(params, "occurrenceDate")) {
      const occurrence = occurrenceOf(existing, params.occurrenceDate, params.timezone);
      // Deleting one occurrence is a modification of the series: drop that date
      // from the parent's recurrence and write the parent back.
      const parent = occurrence.parentItem || existing;
      const revised = parent.clone();
      revised.recurrenceInfo.removeOccurrenceAt(occurrence.recurrenceId);
      await operate(`removing one occurrence of ${itemId} in ${calendar.name}`, (listener) =>
        calendar.modifyItem(revised, parent, listener)
      );
      return {
        deleted: true,
        addressed: "occurrence",
        calendarId: calendar.id,
        occurrence: describe(occurrence),
        series: describe(revised),
        note: "Only that occurrence was removed; the series continues.",
      };
    }

    const addressed = existing.recurrenceInfo ? "series" : "item";
    await operate(`deleting ${itemId} from ${calendar.name}`, (listener) =>
      calendar.deleteItem(existing, listener)
    );
    return {
      deleted: true,
      addressed,
      calendarId: calendar.id,
      item: describe(existing),
      note:
        addressed === "series"
          ? "The whole series was deleted, every occurrence with it."
          : undefined,
    };
  };
}

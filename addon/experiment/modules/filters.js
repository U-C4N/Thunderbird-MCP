/* Message filters — nsIMsgFilterList, rendered as JSON a model can author.
 *
 * A filter is three enums in a trenchcoat: nsMsgSearchAttrib for what to look
 * at, nsMsgSearchOp for how to compare it, nsMsgFilterAction for what to do.
 * The XPCOM objects expose them as bare integers, which is useless to a caller
 * that has never seen msgFilterRules.dat, so the tables below are the point of
 * this module: they translate in both directions and refuse unknown names with
 * the valid set spelled out.
 *
 * Splices into experiment/implementation.js at build time, so H, mod, needMod,
 * Services, Cc/Ci/Cr and console are already in scope.
 */

TBX_MODULE_NAMES.push("filters");

{
  /* nsIMsgFilterService. MailServices.filters is the same object; the contract
   * ID is what the capability probe verified, so it is what we ask for. */
  const FILTER_SERVICE = "@mozilla.org/messenger/services/filters;1";

  /* ------------------------------------------------------------- vocabulary */

  /* `wire name: XPCOM constant name`.
   *
   * Constant names come from comm/mailnews/base/public/nsMsgSearchCore.idl
   * (nsMsgSearchAttrib, nsMsgSearchOp) and nsIMsgFilter.idl (nsMsgFilterAction,
   * nsMsgFilterType). The wire names are ours, picked to be the first thing a
   * model would guess; ALIASES catch the near misses so a plausible spelling
   * costs nobody a round trip.
   *
   * The *integers* are deliberately not written down here — see enumTable().
   */

  const ATTRIBUTES = {
    subject: "Subject",
    from: "Sender",
    to: "To",
    cc: "CC",
    toOrCc: "ToOrCC",
    allAddresses: "AllAddresses",
    body: "Body",
    anyText: "AnyText",
    date: "Date",
    ageInDays: "AgeInDays",
    size: "Size",
    priority: "Priority",
    status: "MsgStatus",
    hasAttachment: "HasAttachmentStatus",
    tag: "Keywords",
    junkStatus: "JunkStatus",
    junkPercent: "JunkPercent",
    junkScoreOrigin: "JunkScoreOrigin",
    otherHeader: "OtherHeader",
    headerProperty: "HdrProperty",
    // Location and MessageKey are marked search-only in the IDL: a filter may
    // hold them (a saved search converted by hand, say) but will never match on
    // them. Kept so such a filter decodes to something readable.
    folder: "Location",
    messageKey: "MessageKey",
    custom: "Custom",
  };

  const ATTRIBUTE_ALIASES = {
    sender: "from",
    author: "from",
    keywords: "tag",
    tags: "tag",
    label: "tag",
    msgstatus: "status",
    messagestatus: "status",
    hasattachmentstatus: "hasAttachment",
    attachment: "hasAttachment",
    age: "ageInDays",
    header: "otherHeader",
    arbitraryheader: "otherHeader",
    location: "folder",
  };

  const OPERATORS = {
    contains: "Contains",
    doesntContain: "DoesntContain",
    is: "Is",
    isnt: "Isnt",
    isEmpty: "IsEmpty",
    isntEmpty: "IsntEmpty",
    beginsWith: "BeginsWith",
    endsWith: "EndsWith",
    isBefore: "IsBefore",
    isAfter: "IsAfter",
    isHigherThan: "IsHigherThan",
    isLowerThan: "IsLowerThan",
    isGreaterThan: "IsGreaterThan",
    isLessThan: "IsLessThan",
    matches: "Matches",
    doesntMatch: "DoesntMatch",
    soundsLike: "SoundsLike",
    isInAddressBook: "IsInAB",
    isntInAddressBook: "IsntInAB",
    nameCompletion: "NameCompletion",
    ldapDwim: "LdapDwim",
  };

  const OPERATOR_ALIASES = {
    doesnotcontain: "doesntContain",
    notcontains: "doesntContain",
    isnot: "isnt",
    equals: "is",
    notempty: "isntEmpty",
    isnotempty: "isntEmpty",
    empty: "isEmpty",
    startswith: "beginsWith",
    before: "isBefore",
    after: "isAfter",
    greaterthan: "isGreaterThan",
    lessthan: "isLessThan",
    isinab: "isInAddressBook",
    isntinab: "isntInAddressBook",
    isnotinab: "isntInAddressBook",
    doesnotmatch: "doesntMatch",
    regex: "matches",
  };

  const ACTIONS = {
    moveToFolder: "MoveToFolder",
    copyToFolder: "CopyToFolder",
    delete: "Delete",
    markRead: "MarkRead",
    markUnread: "MarkUnread",
    markFlagged: "MarkFlagged",
    addTag: "AddTag",
    setJunkScore: "JunkScore",
    changePriority: "ChangePriority",
    ignoreThread: "KillThread",
    ignoreSubthread: "KillSubthread",
    watchThread: "WatchThread",
    reply: "Reply",
    forward: "Forward",
    stopExecution: "StopExecution",
    deleteFromPop3Server: "DeleteFromPop3Server",
    leaveOnPop3Server: "LeaveOnPop3Server",
    fetchBodyFromPop3Server: "FetchBodyFromPop3Server",
    label: "Label",
    custom: "Custom",
    none: "None",
  };

  const ACTION_ALIASES = {
    move: "moveToFolder",
    copy: "copyToFolder",
    markasread: "markRead",
    markasunread: "markUnread",
    markasflagged: "markFlagged",
    star: "markFlagged",
    flag: "markFlagged",
    tag: "addTag",
    addtag: "addTag",
    junkscore: "setJunkScore",
    setpriority: "changePriority",
    priority: "changePriority",
    killthread: "ignoreThread",
    killsubthread: "ignoreSubthread",
    stop: "stopExecution",
  };

  /* nsMsgFilterType is a bitmask of *when* a filter runs. The composite
   * constants (Inbox, News, Incoming, All) are skipped: decoding against single
   * bits is what makes the round trip lossless. InboxJavaScript and
   * NewsJavaScript are a Netscape-era leftover and decode as unknown bits, which
   * is honest — nothing in Thunderbird sets them any more. */
  const FILTER_TYPES = {
    inbox: "InboxRule",
    news: "NewsRule",
    manual: "Manual",
    postPlugin: "PostPlugin",
    postOutgoing: "PostOutgoing",
    archive: "Archive",
    periodic: "Periodic",
  };

  const FILTER_TYPE_ALIASES = {
    incoming: "inbox",
    inboxrule: "inbox",
    newsrule: "news",
    afterjunk: "postPlugin",
    afterclassification: "postPlugin",
    outgoing: "postOutgoing",
    sent: "postOutgoing",
    archiving: "archive",
  };

  /* The five nsMsgMessageFlags the status widget offers, and nothing else: a
   * filter comparing against Expunged or IMAPDeleted is not something a caller
   * should be able to author by accident. */
  const STATUS_FLAGS = {
    read: "Read",
    replied: "Replied",
    forwarded: "Forwarded",
    new: "New",
    flagged: "Marked",
  };

  const STATUS_ALIASES = { starred: "flagged", marked: "flagged" };

  const PRIORITIES = {
    highest: "highest",
    high: "high",
    normal: "normal",
    low: "low",
    lowest: "lowest",
    none: "none",
  };

  /* -------------------------------------------------------- enum plumbing */

  /**
   * Two-way name/integer table over an XPCOM constants interface.
   *
   * The integers are read out of `Ci.<interface>` on first use rather than
   * hard-coded, because the numbering has shifted between releases (actions grew
   * MarkUnread at the end; the junk attributes were renumbered) while the
   * constant *names* have not moved once. A constant this build does not define
   * is dropped from the table, so such a filter decodes to `unknown(<n>)`
   * instead of throwing and taking the whole list with it.
   *
   * Lazy because `Ci` lookups at splice time would run before the rest of the
   * sandbox is assembled, and a table is cheap to build once on demand.
   */
  function enumTable(kind, interfaceName, mapping, aliases) {
    let built = null;

    function table() {
      if (built) {
        return built;
      }
      const iface = Ci[interfaceName];
      const byName = new Map();
      const byValue = new Map();
      const names = [];
      for (const [wire, constant] of Object.entries(mapping)) {
        const value = iface ? iface[constant] : undefined;
        if (typeof value !== "number") {
          continue;
        }
        byName.set(wire.toLowerCase(), value);
        names.push(wire);
        if (!byValue.has(value)) {
          byValue.set(value, wire);
        }
      }
      for (const [alias, wire] of Object.entries(aliases || {})) {
        const value = byName.get(wire.toLowerCase());
        if (value !== undefined) {
          byName.set(alias.toLowerCase(), value);
        }
      }
      if (!names.length) {
        throw H.unsupported(
          `this Thunderbird build does not expose ${interfaceName}, so filters cannot be read or written`
        );
      }
      built = { byName, byValue, names };
      return built;
    }

    return {
      names() {
        return table().names;
      },
      value(given) {
        const found = table().byName.get(String(given).trim().toLowerCase());
        if (found === undefined) {
          throw H.usage(
            `unknown ${kind} ${JSON.stringify(given)} — use one of: ${table().names.join(", ")}`
          );
        }
        return found;
      },
      name(value) {
        const found = table().byValue.get(value);
        return found === undefined ? `unknown(${value})` : found;
      },
    };
  }

  const ATTR = enumTable("search attribute", "nsMsgSearchAttrib", ATTRIBUTES, ATTRIBUTE_ALIASES);
  const OP = enumTable("search operator", "nsMsgSearchOp", OPERATORS, OPERATOR_ALIASES);
  const ACTION = enumTable("filter action", "nsMsgFilterAction", ACTIONS, ACTION_ALIASES);
  const TYPE = enumTable("filter type", "nsMsgFilterType", FILTER_TYPES, FILTER_TYPE_ALIASES);
  const STATUS = enumTable("message status flag", "nsMsgMessageFlags", STATUS_FLAGS, STATUS_ALIASES);
  const PRIORITY = enumTable("priority", "nsMsgPriority", PRIORITIES, {});

  function vocabulary() {
    return {
      attributes: ATTR.names(),
      operators: OP.names(),
      actions: ACTION.names(),
      runWhen: TYPE.names(),
      statusFlags: STATUS.names(),
      priorities: PRIORITY.names(),
    };
  }

  /** Decode a nsMsgFilterType bitmask, reporting bits we have no name for. */
  function decodeFilterType(raw) {
    const names = [];
    let matched = 0;
    for (const name of TYPE.names()) {
      const bit = TYPE.value(name);
      if (bit && (raw & bit) === bit) {
        names.push(name);
        matched |= bit;
      }
    }
    const leftover = raw & ~matched;
    if (leftover) {
      names.push(`unknown(${leftover})`);
    }
    return { raw, names };
  }

  function encodeFilterType(given) {
    const list = Array.isArray(given) ? given : [given];
    if (!list.length) {
      throw H.usage(
        `runWhen must name at least one moment: ${TYPE.names().join(", ")}`
      );
    }
    let raw = 0;
    for (const name of list) {
      raw |= TYPE.value(name);
    }
    return raw;
  }

  function decodeStatus(raw) {
    const names = [];
    let matched = 0;
    for (const name of STATUS.names()) {
      const bit = STATUS.value(name);
      if (bit && (raw & bit) === bit) {
        names.push(name);
        matched |= bit;
      }
    }
    const leftover = raw & ~matched;
    if (leftover) {
      names.push(`unknown(${leftover})`);
    }
    return names;
  }

  function encodeStatus(given) {
    if (Number.isInteger(given)) {
      return given;
    }
    const names = Array.isArray(given)
      ? given
      : String(given === undefined || given === null ? "" : given)
          .split(/[,|\s]+/)
          .filter(Boolean);
    if (!names.length) {
      throw H.usage(
        `a status term needs a flag name: ${STATUS.names().join(", ")}`
      );
    }
    let flags = 0;
    for (const name of names) {
      flags |= STATUS.value(name);
    }
    return flags;
  }

  /* ------------------------------------------------------------- accounts */

  function serverFor(accountKey) {
    const server = H.account(String(accountKey)).incomingServer;
    if (!server) {
      throw H.usage(
        `account ${accountKey} has no incoming server, so it cannot hold filters`
      );
    }
    if (!server.canHaveFilters) {
      throw H.unsupported(
        `${server.prettyName} (${server.type}) does not support message filters`
      );
    }
    return server;
  }

  /** The msgFilterRules.dat-backed list. `null` msgWindow means "do not prompt
   *  me about a corrupt rules file", which is the only sane choice headless. */
  function listFor(server) {
    return server.getFilterList(null);
  }

  function filterableAccounts() {
    const out = [];
    for (const account of H.accounts.accounts) {
      const server = account.incomingServer;
      if (!server || !server.canHaveFilters) {
        continue;
      }
      out.push({ accountKey: account.key, server });
    }
    return out;
  }

  /**
   * Persist the list.
   *
   * Every nsIMsgFilterList mutation is in-memory only; Thunderbird writes the
   * file when the Message Filters dialog closes, and we have no dialog to close.
   * Worse, the dialog shares this very cached list object, so an unsaved change
   * shows up in the UI and looks stored — right up until the next restart drops
   * it. Save after every mutation, not at the end of a batch.
   */
  function persist(list) {
    list.saveToDefaultFile();
  }

  function filterAt(list, index) {
    if (!Number.isInteger(index) || index < 0 || index >= list.filterCount) {
      throw H.usage(
        `index must be 0..${Math.max(list.filterCount - 1, 0)}; this account has ` +
          `${list.filterCount} filter(s). Call filters.list for current indexes.`
      );
    }
    return list.getFilterAt(index);
  }

  /* -------------------------------------------------------------- folders */

  function existingFolder(uri) {
    const utils = mod("MailUtils");
    if (!utils) {
      return null;
    }
    try {
      return utils.getExistingFolder(uri);
    } catch (ex) {
      return null;
    }
  }

  function folderPath(folder) {
    const parts = [];
    let current = folder;
    try {
      while (current && !current.isServer) {
        parts.unshift(current.name);
        current = current.parent;
      }
    } catch (ex) {
      return null;
    }
    return `/${parts.join("/")}`;
  }

  function childNames(folder) {
    try {
      return folder.subFolders.map((child) => child.name);
    } catch (ex) {
      return [];
    }
  }

  /**
   * Resolve a folder reference inside one account.
   *
   * Accepts a full folder URI — what nsIMsgFolder.URI returns and what a
   * move/copy action stores — or an account-relative path like "/Lists/dev",
   * which is the `path` the official folders.* API reports and therefore the
   * only folder handle a caller reliably has.
   */
  function resolveFolder(server, ref) {
    const text = String(ref === undefined || ref === null ? "" : ref).trim();
    if (!text) {
      throw H.usage("a folder reference must not be empty");
    }
    if (text.includes("://")) {
      const folder = existingFolder(text);
      if (!folder) {
        throw H.usage(
          `no folder with URI ${text} — pass an account-relative path such as "/INBOX" instead`
        );
      }
      return folder;
    }
    let folder = server.rootFolder;
    for (const part of text.split("/")) {
      if (!part) {
        continue;
      }
      let child = null;
      try {
        child = folder.getChildNamed(part);
      } catch (ex) {
        child = null;
      }
      if (!child) {
        throw H.usage(
          `${server.prettyName} has no folder "${part}" under ${folderPath(folder)} ` +
            `(children: ${childNames(folder).join(", ") || "none"})`
        );
      }
      folder = child;
    }
    return folder;
  }

  /* ------------------------------------------------------ reading a filter */

  function readTermValue(term, attribute) {
    let value;
    try {
      value = term.value;
    } catch (ex) {
      return null;
    }
    if (!value) {
      return null;
    }
    try {
      switch (attribute) {
        case "date":
          // nsIMsgSearchValue.date is PRTime, i.e. microseconds.
          return value.date ? new Date(value.date / 1000).toISOString() : null;
        case "priority":
          return PRIORITY.name(value.priority);
        case "status":
          return decodeStatus(value.status);
        case "hasAttachment":
          // The one value the UI offers, stored as the Attachment message flag.
          return true;
        case "ageInDays":
          return value.age;
        case "size":
          return value.size;
        case "junkPercent":
          return value.junkPercent;
        case "junkStatus":
          return value.junkStatus === junkValue() ? "junk" : value.junkStatus;
        case "messageKey":
          return value.msgKey;
        case "folder":
          return value.folder ? value.folder.URI : null;
        default:
          return value.str;
      }
    } catch (ex) {
      return null;
    }
  }

  function junkValue() {
    const plugin = Ci.nsIJunkMailPlugin;
    return plugin && typeof plugin.JUNK === "number" ? plugin.JUNK : 2;
  }

  function describeTerm(term) {
    const attribute = ATTR.name(term.attrib);
    const out = {
      attribute,
      operator: OP.name(term.op),
      value: readTermValue(term, attribute),
      booleanAnd: Boolean(term.booleanAnd),
    };
    if (attribute === "custom") {
      out.customId = H.safeGet(term, "customId") || null;
    }
    if (term.matchAll) {
      out.matchAll = true;
    }
    const header = H.safeGet(term, "arbitraryHeader");
    if (typeof header === "string" && header) {
      out.arbitraryHeader = header;
    }
    return out;
  }

  function describeAction(action) {
    const type = ACTION.name(action.type);
    const out = { type };
    switch (type) {
      case "moveToFolder":
      case "copyToFolder": {
        const uri = H.safeGet(action, "targetFolderUri");
        out.targetFolderUri = typeof uri === "string" ? uri : null;
        const folder = out.targetFolderUri ? existingFolder(out.targetFolderUri) : null;
        if (folder) {
          out.targetFolderPath = folderPath(folder);
        } else if (out.targetFolderUri) {
          // A filter pointing at a folder that no longer exists silently stops
          // working, and that is worth saying out loud.
          out.targetFolderMissing = true;
        }
        break;
      }
      case "changePriority":
        out.priority = PRIORITY.name(Number(H.safeGet(action, "priority")));
        break;
      case "setJunkScore":
        out.junkScore = Number(H.safeGet(action, "junkScore"));
        break;
      case "addTag":
        out.tag = H.safeGet(action, "strValue") || null;
        break;
      case "custom":
        out.customId = H.safeGet(action, "customId") || null;
        out.strValue = H.safeGet(action, "strValue") || null;
        break;
      case "reply":
      case "forward":
      case "label":
        out.strValue = H.safeGet(action, "strValue") || null;
        break;
      default:
        break;
    }
    return out;
  }

  function describeFilter(filter, index) {
    const out = {
      index,
      name: filter.filterName,
      enabled: Boolean(filter.enabled),
      filterType: decodeFilterType(filter.filterType),
      temporary: Boolean(filter.temporary),
      searchTerms: [],
      actions: [],
    };
    for (const term of filter.searchTerms) {
      out.searchTerms.push(describeTerm(term));
    }
    for (let i = 0; i < filter.actionCount; i++) {
      out.actions.push(describeAction(filter.getActionAt(i)));
    }
    if (H.safeGet(filter, "unparseable") === true) {
      // Thunderbird could not parse this rule, which is why it looks empty.
      out.unparseable = true;
    }
    return out;
  }

  /* ------------------------------------------------------ writing a filter */

  function toPRTime(raw) {
    // Anything that large is already microseconds, e.g. a value we handed out.
    if (typeof raw === "number" && raw > 1e12) {
      return Math.trunc(raw);
    }
    const parsed = Date.parse(String(raw));
    if (Number.isNaN(parsed)) {
      throw H.usage(
        `a date term needs an ISO-8601 value such as "2026-07-01"; got ${JSON.stringify(raw)}`
      );
    }
    return parsed * 1000;
  }

  function toInteger(raw, what) {
    const parsed = typeof raw === "number" ? raw : parseInt(String(raw), 10);
    if (!Number.isFinite(parsed)) {
      throw H.usage(`${what} must be a number; got ${JSON.stringify(raw)}`);
    }
    return Math.trunc(parsed);
  }

  function setTermValue(term, server, attribute, raw) {
    // The value object carries its own copy of the attribute, and the setter
    // reads it to decide which field it trusts — hence the assignment dance
    // (read, stamp attrib, fill, write back) that the filter editor also does.
    const value = term.value;
    value.attrib = term.attrib;
    switch (attribute) {
      case "date":
        value.date = toPRTime(raw);
        break;
      case "priority":
        value.priority = PRIORITY.value(raw);
        break;
      case "status":
        value.status = encodeStatus(raw);
        break;
      case "hasAttachment":
        // Fixed: the operator (is / isnt) is what carries the meaning here.
        value.status = Ci.nsMsgMessageFlags.Attachment;
        break;
      case "ageInDays":
        value.age = toInteger(raw, "an ageInDays value");
        break;
      case "size":
        value.size = toInteger(raw, "a size value (in KiB)");
        break;
      case "junkPercent":
        value.junkPercent = toInteger(raw, "a junkPercent value");
        break;
      case "junkStatus":
        // The UI offers exactly one junk status ("Junk"), so anything that is
        // not already an integer means that.
        value.junkStatus = Number.isInteger(raw) ? raw : junkValue();
        break;
      case "messageKey":
        value.msgKey = toInteger(raw, "a messageKey value");
        break;
      case "folder":
        value.folder = resolveFolder(server, raw);
        break;
      default:
        value.str = raw === undefined || raw === null ? "" : String(raw);
        break;
    }
    term.value = value;
  }

  /**
   * Replace a filter's search terms.
   *
   * Terms are built and validated before anything is cleared, so a bad spec
   * leaves the stored filter exactly as it was.
   */
  function applyTerms(filter, server, specs) {
    if (!Array.isArray(specs) || !specs.length) {
      throw H.usage(
        'searchTerms must be a non-empty array, e.g. [{"attribute":"subject",' +
          '"operator":"contains","value":"invoice"}]'
      );
    }
    const built = [];
    for (const spec of specs) {
      if (!spec || typeof spec !== "object") {
        throw H.usage("each entry in searchTerms must be an object");
      }
      const matchAll = Boolean(spec.matchAll);
      // A "match all messages" filter still needs a syntactically valid term;
      // Thunderbird ignores the attribute once matchAll is set.
      const attribute = spec.attribute === undefined && matchAll ? "subject" : H.need(spec, "attribute");
      const operator = spec.operator === undefined && matchAll ? "contains" : H.need(spec, "operator");
      const term = filter.createTerm();
      term.attrib = ATTR.value(attribute);
      const canonical = ATTR.name(term.attrib);
      if (canonical === "custom") {
        term.customId = String(H.need(spec, "customId"));
      }
      if (canonical === "otherHeader") {
        // Without the header name the term matches nothing at all.
        term.arbitraryHeader = String(H.need(spec, "arbitraryHeader"));
      }
      term.op = OP.value(operator);
      setTermValue(term, server, canonical, spec.value);
      term.booleanAnd = spec.booleanAnd === undefined ? true : Boolean(spec.booleanAnd);
      term.matchAll = matchAll;
      built.push(term);
    }
    filter.searchTerms = [];
    for (const term of built) {
      filter.appendTerm(term);
    }
  }

  /** Fill one nsIMsgRuleAction from a spec, or say what the action still needs. */
  function fillAction(action, server, spec, type) {
    switch (type) {
      case "moveToFolder":
      case "copyToFolder": {
        const ref = spec.targetFolderUri || spec.targetFolder || spec.targetFolderPath;
        if (!ref) {
          throw H.usage(
            `a ${type} action needs targetFolderUri (or targetFolder, an ` +
              'account-relative path such as "/Archive/2026")'
          );
        }
        action.targetFolderUri = resolveFolder(server, ref).URI;
        break;
      }
      case "changePriority":
        action.priority = PRIORITY.value(H.need(spec, "priority"));
        break;
      case "setJunkScore": {
        const score = toInteger(H.need(spec, "junkScore"), "junkScore");
        if (score < 0 || score > 100) {
          throw H.usage("junkScore must be 0 (not junk) to 100 (junk)");
        }
        action.junkScore = score;
        break;
      }
      case "addTag":
        // The stored value is the tag *key* from mail_tags, not its label.
        action.strValue = String(spec.tag === undefined ? H.need(spec, "strValue") : spec.tag);
        break;
      case "reply":
        // A template message URI, not an address: Thunderbird replies with a
        // stored template.
        action.strValue = String(H.need(spec, "strValue"));
        break;
      case "forward":
        action.strValue = String(H.need(spec, "strValue"));
        break;
      case "custom":
        action.customId = String(H.need(spec, "customId"));
        if (spec.strValue !== undefined && spec.strValue !== null) {
          action.strValue = String(spec.strValue);
        }
        break;
      case "label":
        action.strValue = String(H.need(spec, "strValue"));
        break;
      default:
        break;
    }
  }

  function applyActions(filter, server, specs) {
    if (!Array.isArray(specs) || !specs.length) {
      throw H.usage(
        'actions must be a non-empty array, e.g. [{"type":"moveToFolder",' +
          '"targetFolder":"/Archive/2026"}]'
      );
    }
    const built = [];
    for (const spec of specs) {
      if (!spec || typeof spec !== "object") {
        throw H.usage("each entry in actions must be an object");
      }
      const action = filter.createAction();
      action.type = ACTION.value(H.need(spec, "type"));
      const type = ACTION.name(action.type);
      fillAction(action, server, spec, type);
      built.push(action);
    }
    filter.clearActionList();
    for (const action of built) {
      filter.appendAction(action);
    }
  }

  function assertNameFree(list, name, exceptFilter) {
    for (let i = 0; i < list.filterCount; i++) {
      const other = list.getFilterAt(i);
      if (other !== exceptFilter && other.filterName === name) {
        throw H.usage(
          `this account already has a filter named ${JSON.stringify(name)} at index ${i}; ` +
            "filter names are how the user tells them apart, so pick another"
        );
      }
    }
  }

  /* ------------------------------------------------------------- handlers */

  TBX_MODULES["filters.list"] = async (params) => {
    const targets = params.accountKey
      ? [{ accountKey: String(params.accountKey), server: serverFor(params.accountKey) }]
      : filterableAccounts();
    const filters = [];
    const accounts = [];
    for (const { accountKey, server } of targets) {
      const list = listFor(server);
      accounts.push({
        accountKey,
        accountName: server.prettyName,
        serverType: server.type,
        filterCount: list.filterCount,
        loggingEnabled: H.safeGet(list, "loggingEnabled"),
      });
      for (let i = 0; i < list.filterCount; i++) {
        const filter = list.getFilterAt(i);
        if (params.enabledOnly && !filter.enabled) {
          continue;
        }
        filters.push({ accountKey, ...describeFilter(filter, i) });
      }
    }
    return { filters, accounts, vocabulary: vocabulary() };
  };

  TBX_MODULES["filters.create"] = async (params) => {
    const accountKey = String(H.need(params, "accountKey"));
    const name = String(H.need(params, "name")).trim();
    if (!name) {
      throw H.usage("name must not be blank");
    }
    const server = serverFor(accountKey);
    const list = listFor(server);
    assertNameFree(list, name, null);

    const filter = list.createFilter(name);
    // Incoming mail plus manual runs: the same default the filter editor uses
    // for a brand-new filter, and the only combination most callers want.
    filter.filterType = encodeFilterType(
      params.runWhen === undefined || params.runWhen === null ? ["inbox", "manual"] : params.runWhen
    );
    filter.enabled = params.enabled === undefined ? true : Boolean(params.enabled);
    applyTerms(filter, server, params.searchTerms);
    applyActions(filter, server, params.actions);

    // Append by default. Inserting at the top would silently reorder every
    // existing rule relative to this one, which matters as soon as one of them
    // stops execution.
    const position = Number.isInteger(params.position)
      ? Math.max(0, Math.min(params.position, list.filterCount))
      : list.filterCount;
    list.insertFilterAt(position, filter);
    persist(list);
    return { accountKey, filter: describeFilter(filter, position) };
  };

  TBX_MODULES["filters.update"] = async (params) => {
    const accountKey = String(H.need(params, "accountKey"));
    const server = serverFor(accountKey);
    const list = listFor(server);
    const index = params.index;
    const filter = filterAt(list, index);
    const before = describeFilter(filter, index);

    let touched = false;
    if (params.name !== undefined && params.name !== null) {
      const name = String(params.name).trim();
      if (!name) {
        throw H.usage("name must not be blank");
      }
      assertNameFree(list, name, filter);
      filter.filterName = name;
      touched = true;
    }
    if (params.runWhen !== undefined && params.runWhen !== null) {
      filter.filterType = encodeFilterType(params.runWhen);
      touched = true;
    }
    if (params.enabled !== undefined && params.enabled !== null) {
      filter.enabled = Boolean(params.enabled);
      touched = true;
    }
    if (params.searchTerms !== undefined && params.searchTerms !== null) {
      applyTerms(filter, server, params.searchTerms);
      touched = true;
    }
    if (params.actions !== undefined && params.actions !== null) {
      applyActions(filter, server, params.actions);
      touched = true;
    }
    if (!touched) {
      throw H.usage(
        "nothing to change — send name, runWhen, enabled, searchTerms or actions"
      );
    }
    persist(list);
    return { accountKey, previous: before, current: describeFilter(filter, index) };
  };

  TBX_MODULES["filters.delete"] = async (params) => {
    const accountKey = String(H.need(params, "accountKey"));
    const server = serverFor(accountKey);
    const list = listFor(server);
    const index = params.index;
    const filter = filterAt(list, index);
    const before = describeFilter(filter, index);
    list.removeFilter(filter);
    persist(list);
    return { accountKey, previous: before, remaining: list.filterCount };
  };

  TBX_MODULES["filters.reorder"] = async (params) => {
    const accountKey = String(H.need(params, "accountKey"));
    const server = serverFor(accountKey);
    const list = listFor(server);
    const index = params.index;
    const filter = filterAt(list, index);
    const toIndex = params.toIndex;
    if (!Number.isInteger(toIndex) || toIndex < 0 || toIndex >= list.filterCount) {
      throw H.usage(
        `toIndex must be 0..${Math.max(list.filterCount - 1, 0)}; it is the position the ` +
          "filter should end up at"
      );
    }
    // remove-then-insert, so toIndex means the final position in both
    // directions: after the removal the list is one shorter, which is exactly
    // the off-by-one a downward move needs.
    list.removeFilter(filter);
    list.insertFilterAt(toIndex, filter);
    persist(list);
    return {
      accountKey,
      moved: { name: filter.filterName, from: index, to: toIndex },
      order: orderOf(list),
    };
  };

  TBX_MODULES["filters.setEnabled"] = async (params) => {
    const accountKey = String(H.need(params, "accountKey"));
    const server = serverFor(accountKey);
    const list = listFor(server);
    const index = params.index;
    const filter = filterAt(list, index);
    if (params.enabled === undefined || params.enabled === null) {
      throw H.usage("enabled is required (true or false)");
    }
    const before = Boolean(filter.enabled);
    filter.enabled = Boolean(params.enabled);
    persist(list);
    return {
      accountKey,
      index,
      name: filter.filterName,
      previous: before,
      current: Boolean(filter.enabled),
    };
  };

  function orderOf(list) {
    const out = [];
    for (let i = 0; i < list.filterCount; i++) {
      out.push({ index: i, name: list.getFilterAt(i).filterName });
    }
    return out;
  }

  TBX_MODULES["filters.run"] = async (params) => {
    const accountKey = String(H.need(params, "accountKey"));
    const server = serverFor(accountKey);
    const list = listFor(server);

    const requested = params.filterIndexes;
    const chosen = [];
    if (requested === undefined || requested === null || (Array.isArray(requested) && !requested.length)) {
      for (let i = 0; i < list.filterCount; i++) {
        const filter = list.getFilterAt(i);
        if (filter.enabled && !filter.temporary) {
          chosen.push({ index: i, filter });
        }
      }
      if (!chosen.length) {
        throw H.usage(
          `account ${accountKey} has no enabled filters to run — enable one with ` +
            "filters.setEnabled, or name indexes explicitly in filterIndexes"
        );
      }
    } else {
      if (!Array.isArray(requested)) {
        throw H.usage("filterIndexes must be an array of indexes from filters.list");
      }
      for (const index of requested) {
        chosen.push({ index, filter: filterAt(list, index) });
      }
    }

    const refs = params.folderIds;
    let folders;
    if (Array.isArray(refs) && refs.length) {
      folders = refs.map((ref) => resolveFolder(server, ref));
    } else {
      folders = server.rootFolder.getFoldersWithFlags(Ci.nsMsgFolderFlags.Inbox);
      if (!folders.length) {
        throw H.usage(
          `account ${accountKey} has no Inbox, so folderIds is required (a folder URI or ` +
            'an account-relative path such as "/Lists/dev")'
        );
      }
    }

    const service = Cc[FILTER_SERVICE].getService(Ci.nsIMsgFilterService);
    // Both of Thunderbird's own "run now" paths copy the wanted filters into a
    // temporary list rather than handing over the account's real one, because
    // applyFiltersToFolders runs everything in the list it is given.
    const temp = service.getTempFilterList(server.rootFolder);
    if (H.safeGet(list, "loggingEnabled") === true) {
      temp.loggingEnabled = true;
      try {
        temp.logStream = list.logStream;
      } catch (ex) {
        console.warn("[tbmcp] could not share the filter log stream:", ex.message || ex);
      }
    }
    chosen.forEach((entry, position) => temp.insertFilterAt(position, entry.filter));

    const waitMs = Number.isInteger(params.waitMs) ? Math.min(params.waitMs, 600000) : 60000;
    const outcome = await new Promise((resolve) => {
      let settled = false;
      const finish = (payload) => {
        if (!settled) {
          settled = true;
          resolve(payload);
        }
      };
      const timer = setTimeout(
        () =>
          finish({
            completed: false,
            note: `still running after ${waitMs}ms — filtering continues in the background`,
          }),
        waitMs
      );
      // nsIMsgOperationListener has the single method onStopOperation, and
      // XPConnect accepts a plain object for it (MessageSend.sys.mjs passes an
      // un-QI'd class the same way). A build whose applyFiltersToFolders
      // predates the optional callback ignores the fourth argument entirely,
      // which is why the timeout above is the real exit and not a safety net.
      service.applyFiltersToFolders(temp, folders, null, {
        onStopOperation(status) {
          clearTimeout(timer);
          finish({ completed: true, status, ok: status === Cr.NS_OK });
        },
      });
    });

    return {
      accountKey,
      filters: chosen.map((entry) => ({ index: entry.index, name: entry.filter.filterName })),
      folders: folders.map((folder) => ({ uri: folder.URI, path: folderPath(folder) })),
      ...outcome,
    };
  };
}

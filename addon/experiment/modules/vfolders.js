/* Saved searches — virtual folders.
 *
 * A virtual folder is an ordinary nsIMsgFolder carrying the Virtual flag, whose
 * "contents" are a stored search: a set of folders to look in, a serialised list of
 * search terms, and whether to run the search on the server. Thunderbird's own UI
 * calls these Saved Searches.
 *
 * VirtualFolderHelper (resource:///modules/VirtualFolderWrapper.sys.mjs) exposes
 * createNewVirtualFolder and wrapVirtualFolder; the wrapper is what persists the
 * definition into the folder's msf database and virtualFolders.dat.
 *
 * The search-term vocabulary is redeclared here rather than shared with filters.js.
 * These files are concatenated in filename order at build time, so depending on
 * another module's constants would be a load-order bug waiting to happen.
 */

TBX_MODULE_NAMES.push("vfolders");

{
  /** Wire name -> nsMsgSearchAttrib constant name. Same wire names as filters.js,
   *  deliberately: a caller should not have to learn two vocabularies. */
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
    junkScore: "JunkScore",
    messageId: "MessageId",
    otherHeader: "OtherHeader",
    folder: "Location",
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
  };

  /** Resolve a wire name against an XPCOM enum, without hardcoding the integers —
   *  they have been renumbered before and would silently corrupt saved searches. */
  function enumValue(enumObject, table, wire, kind) {
    const key = String(wire || "");
    const constant = table[key] || table[Object.keys(table).find((k) => k.toLowerCase() === key.toLowerCase())];
    if (!constant || enumObject[constant] === undefined) {
      throw H.usage(`unknown ${kind} ${wire}; valid: ${Object.keys(table).join(", ")}`);
    }
    return enumObject[constant];
  }

  function enumName(enumObject, table, value) {
    for (const [wire, constant] of Object.entries(table)) {
      if (enumObject[constant] === value) {
        return wire;
      }
    }
    return `unknown(${value})`;
  }

  /** Same folder-id convention as admin.js: "<accountKey>://<path>" or a raw URI. */
  function folderFor(id) {
    const text = String(id);
    const utils = mod("MailUtils");
    if (utils && typeof utils.getExistingFolder === "function") {
      try {
        const direct = utils.getExistingFolder(text);
        if (direct) {
          return direct;
        }
      } catch (ex) {
        // not a URI; fall through
      }
    }
    const split = text.indexOf("://");
    if (split < 1) {
      throw H.usage(`${text} is not a folder id — pass one from folder_list`);
    }
    let folder = H.account(text.slice(0, split)).incomingServer.rootFolder;
    for (const segment of text.slice(split + 3).split("/")) {
      if (!segment) {
        continue;
      }
      const name = decodeURIComponent(segment);
      let child = null;
      try {
        child = folder.getChildNamed(name);
      } catch (ex) {
        child = null;
      }
      if (!child) {
        throw H.usage(`${text}: ${folder.name} has no subfolder named ${name}`);
      }
      folder = child;
    }
    return folder;
  }

  function searchSession() {
    return Cc["@mozilla.org/messenger/searchSession;1"].createInstance(Ci.nsIMsgSearchSession);
  }

  /** Build nsIMsgSearchTerm objects from the structured form callers send. */
  function buildTerms(terms) {
    if (!Array.isArray(terms) || !terms.length) {
      throw H.usage("terms must be a non-empty array of {attribute, operator, value}");
    }
    const session = searchSession();
    return terms.map((spec, index) => {
      if (!spec || typeof spec !== "object") {
        throw H.usage(`terms[${index}] must be an object`);
      }
      const term = session.createTerm();
      term.attrib = enumValue(Ci.nsMsgSearchAttrib, ATTRIBUTES, spec.attribute, "attribute");
      term.op = enumValue(Ci.nsMsgSearchOp, OPERATORS, spec.operator, "operator");
      // booleanAnd true = AND, false = OR. Mixed grouping is not expressible in a
      // flat term list, which is also true of Thunderbird's own UI.
      term.booleanAnd = spec.matchAll === undefined ? true : Boolean(spec.matchAll);
      if (spec.attribute === "otherHeader" && spec.header) {
        term.arbitraryHeader = String(spec.header);
      }

      const value = term.value;
      value.attrib = term.attrib;
      const raw = spec.value;
      switch (term.attrib) {
        case Ci.nsMsgSearchAttrib.Date:
          if (!raw) {
            throw H.usage(`terms[${index}] with attribute date needs an ISO date value`);
          }
          // nsIMsgSearchValue.date is PRTime: microseconds since the epoch.
          value.date = new Date(raw).getTime() * 1000;
          break;
        case Ci.nsMsgSearchAttrib.AgeInDays:
        case Ci.nsMsgSearchAttrib.Size:
        case Ci.nsMsgSearchAttrib.JunkScore:
        case Ci.nsMsgSearchAttrib.Priority:
        case Ci.nsMsgSearchAttrib.MsgStatus:
        case Ci.nsMsgSearchAttrib.JunkStatus:
        case Ci.nsMsgSearchAttrib.HasAttachmentStatus: {
          const n = Number(raw);
          if (!Number.isFinite(n)) {
            throw H.usage(`terms[${index}] needs a numeric value; got ${JSON.stringify(raw)}`);
          }
          if (term.attrib === Ci.nsMsgSearchAttrib.Size) {
            value.size = n;
          } else if (term.attrib === Ci.nsMsgSearchAttrib.AgeInDays) {
            value.age = n;
          } else if (term.attrib === Ci.nsMsgSearchAttrib.JunkScore) {
            value.junkScore = n;
          } else if (term.attrib === Ci.nsMsgSearchAttrib.Priority) {
            value.priority = n;
          } else {
            value.status = n;
          }
          break;
        }
        default:
          value.str = raw === null || raw === undefined ? "" : String(raw);
          break;
      }
      term.value = value;
      return term;
    });
  }

  function describeTerms(terms) {
    const out = [];
    for (const term of terms || []) {
      const entry = {
        attribute: enumName(Ci.nsMsgSearchAttrib, ATTRIBUTES, term.attrib),
        operator: enumName(Ci.nsMsgSearchOp, OPERATORS, term.op),
        matchAll: term.booleanAnd,
      };
      try {
        const value = term.value;
        if (term.attrib === Ci.nsMsgSearchAttrib.Date) {
          entry.value = new Date(value.date / 1000).toISOString();
        } else if (term.attrib === Ci.nsMsgSearchAttrib.Size) {
          entry.value = value.size;
        } else if (term.attrib === Ci.nsMsgSearchAttrib.AgeInDays) {
          entry.value = value.age;
        } else if (term.attrib === Ci.nsMsgSearchAttrib.JunkScore) {
          entry.value = value.junkScore;
        } else {
          entry.value = value.str;
        }
      } catch (ex) {
        entry.value = null;
      }
      if (term.arbitraryHeader) {
        entry.header = term.arbitraryHeader;
      }
      out.push(entry);
    }
    return out;
  }

  function folderRef(folder) {
    if (!folder) {
      return null;
    }
    let accountKey = null;
    try {
      const server = folder.server;
      const account = [...H.accounts.accounts].find(
        (a) => a.incomingServer && a.incomingServer.key === server.key
      );
      accountKey = account ? account.key : null;
    } catch (ex) {
      accountKey = null;
    }
    return {
      uri: folder.URI,
      name: folder.name,
      accountKey,
      // The WebExtension-style id, so a caller can feed it straight back to the
      // mail tools without another lookup.
      folderId: accountKey ? `${accountKey}:/${folder.URI.split("://")[1].split("/").slice(1).join("/")}` : null,
    };
  }

  function describe(folder) {
    const helper = needMod("VirtualFolderWrapper");
    const wrapper = (helper.VirtualFolderHelper || helper).wrapVirtualFolder(folder);
    return {
      uri: folder.URI,
      name: folder.prettyName || folder.name,
      parentUri: folder.parent ? folder.parent.URI : null,
      searchFolders: (wrapper.searchFolders || []).map(folderRef),
      terms: describeTerms(wrapper.searchTerms),
      searchString: wrapper.searchString || null,
      onlineSearch: Boolean(wrapper.onlineSearch),
      totalMessageCount: H.safeGet(folder, "getTotalMessages") === undefined ? null : null,
    };
  }

  /** Every folder carrying the Virtual flag, across all accounts. */
  function allVirtualFolders() {
    const found = [];
    for (const account of H.accounts.accounts) {
      const server = account.incomingServer;
      if (!server) {
        continue;
      }
      let root;
      try {
        root = server.rootFolder;
      } catch (ex) {
        continue;
      }
      const stack = [root];
      while (stack.length) {
        const folder = stack.pop();
        try {
          if (folder.flags & Ci.nsMsgFolderFlags.Virtual) {
            found.push(folder);
          }
          for (const child of folder.subFolders) {
            stack.push(child);
          }
        } catch (ex) {
          // A folder whose database will not open is not worth failing the list for.
        }
      }
    }
    return found;
  }

  function findVirtual(uriOrName) {
    const wanted = String(uriOrName);
    const candidates = allVirtualFolders();
    for (const folder of candidates) {
      if (folder.URI === wanted) {
        return folder;
      }
    }
    for (const folder of candidates) {
      if ((folder.prettyName || folder.name) === wanted) {
        return folder;
      }
    }
    const names = candidates.map((f) => f.prettyName || f.name).join(", ");
    throw H.usage(`no saved search called ${wanted} (known: ${names || "none"})`);
  }

  TBX_MODULES["vfolders.list"] = async () => {
    const folders = allVirtualFolders();
    return { savedSearches: folders.map(describe), count: folders.length };
  };

  TBX_MODULES["vfolders.create"] = async (params) => {
    const name = H.need(params, "name");
    const searchFolderIds = params.searchFolderIds;
    if (!Array.isArray(searchFolderIds) || !searchFolderIds.length) {
      throw H.usage("searchFolderIds must list at least one folder to search");
    }
    const terms = buildTerms(params.terms);
    const searchFolders = searchFolderIds.map(folderFor);

    // Saved searches live under a real folder. Local Folders is where Thunderbird's
    // own UI puts them, and it always exists.
    const parent = params.parentFolderId
      ? folderFor(params.parentFolderId)
      : H.accounts.localFoldersServer.rootFolder;

    for (const existing of allVirtualFolders()) {
      if ((existing.prettyName || existing.name) === name && existing.parent
          && existing.parent.URI === parent.URI) {
        throw H.usage(`a saved search called ${name} already exists here`);
      }
    }

    const helper = needMod("VirtualFolderWrapper");
    const wrapper = (helper.VirtualFolderHelper || helper).createNewVirtualFolder(
      name,
      parent,
      searchFolders,
      terms,
      Boolean(params.onlineSearch)
    );
    const folder = wrapper.virtualFolder || findVirtual(name);
    return { created: true, savedSearch: describe(folder) };
  };

  TBX_MODULES["vfolders.update"] = async (params) => {
    const folder = findVirtual(H.need(params, "savedSearch"));
    const helper = needMod("VirtualFolderWrapper");
    const wrapper = (helper.VirtualFolderHelper || helper).wrapVirtualFolder(folder);
    const before = describe(folder);

    if (params.searchFolderIds !== undefined) {
      if (!Array.isArray(params.searchFolderIds) || !params.searchFolderIds.length) {
        throw H.usage("searchFolderIds must list at least one folder");
      }
      wrapper.searchFolders = params.searchFolderIds.map(folderFor);
    }
    if (params.terms !== undefined) {
      wrapper.searchTerms = buildTerms(params.terms);
    }
    if (params.onlineSearch !== undefined) {
      wrapper.onlineSearch = Boolean(params.onlineSearch);
    }
    // Without this the definition stays in memory only and is lost on restart.
    wrapper.cleanUpMessageDatabase();
    H.accounts.saveVirtualFolders();

    return { updated: true, previous: before, current: describe(folder) };
  };

  TBX_MODULES["vfolders.delete"] = async (params) => {
    const folder = findVirtual(H.need(params, "savedSearch"));
    const before = describe(folder);
    const parent = folder.parent;
    if (!parent) {
      throw H.unsupported(`${before.name} has no parent folder, so it cannot be deleted`);
    }
    // Deleting a virtual folder removes only the saved search; the messages it
    // listed live in the real folders and are untouched.
    parent.propagateDelete(folder, true);
    H.accounts.saveVirtualFolders();
    return { deleted: true, previous: before, messagesAffected: 0 };
  };
}

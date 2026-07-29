/* Address books, contacts and mailing lists — official API only.
 *
 * The design decision worth knowing: on 153 a contact *is* a vCard, and writing the
 * card is the only supported way to change one. But a model should not have to parse
 * vCard to answer "what is Ada's email", so every read returns a flattened view
 * (displayName, emails, phones, organisation, notes…) alongside the card itself, and
 * every write accepts either flattened fields or a raw card.
 *
 * PHOTO/LOGO/KEY are stripped from the cards we return: an inline photo is a hundred
 * kilobytes of base64 that no caller can act on. `contacts.getPhoto` fetches it on
 * request instead.
 *
 * Two signatures differ by manifest version — MV3 passes the card as a bare string,
 * MV2 wraps it in a properties object — so writes go through `vCardArgument`.
 */

{
  const DEFAULT_LIMIT = 50;
  const SEARCH_LIMIT = 25;
  /** Ceiling on the fallback scan, so a 20k-contact LDAP mirror cannot wedge the UI. */
  const MAX_SCAN = 5000;

  /** Properties whose value is a base64 blob. Never returned inline. */
  const BULKY = new Set(["PHOTO", "LOGO", "SOUND", "KEY"]);

  /** TYPE values that carry no information a caller would act on. */
  const NOISE_TYPES = new Set(["internet", "voice", "pref", "x-mozilla-html", "unknown"]);

  // -------------------------------------------------------------------- vCard I/O

  function unfold(text) {
    return String(text || "")
      .replace(/\r\n/g, "\n")
      .replace(/\n[ \t]/g, "");
  }

  /** Split on `sep`, ignoring separators inside a quoted parameter value. */
  function splitQuoted(text, sep) {
    const out = [];
    let current = "";
    let quoted = false;
    for (const ch of String(text)) {
      if (ch === '"') {
        quoted = !quoted;
        current += ch;
      } else if (ch === sep && !quoted) {
        out.push(current);
        current = "";
      } else {
        current += ch;
      }
    }
    out.push(current);
    return out;
  }

  /** Split a value on `sep`, honouring vCard backslash escapes. */
  function splitEscaped(text, sep) {
    const out = [];
    let current = "";
    const value = String(text);
    for (let i = 0; i < value.length; i += 1) {
      if (value[i] === "\\") {
        current += value[i] + (value[i + 1] || "");
        i += 1;
      } else if (value[i] === sep) {
        out.push(current);
        current = "";
      } else {
        current += value[i];
      }
    }
    out.push(current);
    return out;
  }

  function unescapeText(value) {
    return String(value).replace(/\\(.)/g, (match, ch) => {
      if (ch === "n" || ch === "N") {
        return "\n";
      }
      if (ch === "t") {
        return "\t";
      }
      return ch;
    });
  }

  function escapeText(value) {
    return String(value)
      .replace(/([\\;,])/g, "\\$1")
      .replace(/\r?\n/g, "\\n");
  }

  /** `[group.]NAME[;PARAM=value]:value` — the colon may not be the first one. */
  function parseLine(raw) {
    let quoted = false;
    let colon = -1;
    for (let i = 0; i < raw.length; i += 1) {
      if (raw[i] === '"') {
        quoted = !quoted;
      } else if (raw[i] === ":" && !quoted) {
        colon = i;
        break;
      }
    }
    if (colon < 0) {
      return null;
    }
    const pieces = splitQuoted(raw.slice(0, colon), ";");
    let name = (pieces.shift() || "").trim();
    if (name.includes(".")) {
      name = name.slice(name.lastIndexOf(".") + 1);
    }
    const params = {};
    for (const piece of pieces) {
      if (!piece.trim()) {
        continue;
      }
      const eq = piece.indexOf("=");
      // A bare parameter is the vCard 2.1 shorthand for TYPE=, still seen in the wild.
      const key = (eq < 0 ? "type" : piece.slice(0, eq)).trim().toLowerCase();
      const rest = eq < 0 ? piece : piece.slice(eq + 1);
      const values = splitQuoted(rest, ",").map((v) => v.trim().replace(/^"([\s\S]*)"$/, "$1"));
      params[key] = (params[key] || []).concat(values.filter(Boolean));
    }
    return { name: name.toUpperCase(), params, value: raw.slice(colon + 1), raw };
  }

  /** Build a line through the parser, so created and read lines behave identically. */
  function lineOf(name, value, params) {
    const head = [name];
    for (const key of Object.keys(params || {})) {
      head.push(`${key}=${params[key]}`);
    }
    return parseLine(`${head.join(";")}:${value}`);
  }

  function parseCard(text) {
    const card = { version: "4.0", lines: [] };
    for (const raw of unfold(text).split("\n")) {
      const trimmed = raw.trim();
      if (!trimmed) {
        continue;
      }
      const line = parseLine(trimmed);
      if (!line || line.name === "BEGIN" || line.name === "END") {
        continue;
      }
      if (line.name === "VERSION") {
        card.version = line.value.trim() || card.version;
        continue;
      }
      card.lines.push(line);
    }
    return card;
  }

  function serialize(card, skip) {
    const body = card.lines.filter((line) => !skip || !skip.has(line.name)).map((line) => line.raw);
    return ["BEGIN:VCARD", `VERSION:${card.version}`, ...body, "END:VCARD"].join("\r\n");
  }

  function findAll(card, name) {
    return card.lines.filter((line) => line.name === name);
  }

  function findOne(card, name) {
    return card.lines.find((line) => line.name === name) || null;
  }

  function textOf(line) {
    return line ? unescapeText(line.value).trim() : null;
  }

  function components(line) {
    return line ? splitEscaped(line.value, ";").map((part) => unescapeText(part).trim()) : [];
  }

  function typesOf(line) {
    return (line.params.type || [])
      .map((type) => String(type).toLowerCase())
      .filter((type) => !NOISE_TYPES.has(type));
  }

  function isPreferred(line) {
    return (
      (line.params.pref || []).includes("1") ||
      (line.params.type || []).some((type) => String(type).toLowerCase() === "pref")
    );
  }

  /** The flattened view. Keep it small: a search returning 25 of these is common. */
  function describe(card, node) {
    const legacy = (node && node.properties) || {};
    const name = components(findOne(card, "N"));
    const org = components(findOne(card, "ORG"));
    const emails = findAll(card, "EMAIL")
      .map((line) => ({
        value: textOf(line),
        types: typesOf(line),
        preferred: isPreferred(line),
      }))
      .filter((entry) => entry.value);
    const phones = findAll(card, "TEL")
      .map((line) => ({ value: textOf(line), types: typesOf(line) }))
      .filter((entry) => entry.value);
    const primary = emails.find((entry) => entry.preferred) || emails[0] || null;
    const displayName =
      textOf(findOne(card, "FN")) ||
      [name[1], name[0]].filter(Boolean).join(" ") ||
      legacy.DisplayName ||
      (primary && primary.value) ||
      null;
    return {
      displayName,
      firstName: name[1] || null,
      lastName: name[0] || null,
      nickname: textOf(findOne(card, "NICKNAME")),
      primaryEmail: primary ? primary.value : null,
      emails,
      phones,
      organisation: org[0] || null,
      department: org[1] || null,
      jobTitle: textOf(findOne(card, "TITLE")),
      notes: findAll(card, "NOTE")
        .map(textOf)
        .filter(Boolean)
        .join("\n") || null,
      birthday: textOf(findOne(card, "BDAY")),
      addresses: findAll(card, "ADR").map((line) => {
        const parts = components(line);
        return {
          types: typesOf(line),
          // pobox, extended, street, locality, region, postcode, country
          formatted: [parts[2], parts[3], parts[4], parts[5], parts[6]]
            .filter(Boolean)
            .join(", "),
        };
      }),
      urls: findAll(card, "URL").map(textOf).filter(Boolean),
    };
  }

  // --------------------------------------------------------------- writing cards

  function present(fields, key) {
    return fields[key] !== undefined && fields[key] !== null;
  }

  function setLine(card, name, value, params) {
    card.lines = card.lines.filter((line) => line.name !== name);
    if (value !== "" && value !== null && value !== undefined) {
      card.lines.push(lineOf(name, value, params));
    }
  }

  /** Accept `["a@b"]` or `[{value, type}]`; the bridge is called by tests too. */
  function entriesOf(value) {
    const out = [];
    for (const item of Array.isArray(value) ? value : []) {
      if (item === null || item === undefined) {
        continue;
      }
      const text = String(typeof item === "string" ? item : item.value || "").trim();
      if (!text) {
        continue;
      }
      const types = typeof item === "string" ? [] : item.types || (item.type ? [item.type] : []);
      out.push({ value: text, types: types.map((type) => String(type).toLowerCase()) });
    }
    return out;
  }

  function setMulti(card, name, entries) {
    card.lines = card.lines.filter((line) => line.name !== name);
    entries.forEach((entry, index) => {
      const params = {};
      if (entry.types.length) {
        params.TYPE = entry.types.join(",");
      }
      // The first entry is the preferred one, which is what `primaryEmail` reports
      // back — so the order the caller gave survives a round trip.
      if (index === 0) {
        params.PREF = "1";
      }
      card.lines.push(lineOf(name, escapeText(entry.value), params));
    });
  }

  /** Merge flattened fields onto a card. Absent means "leave alone", "" means "clear". */
  function applyFields(card, fields) {
    const NAME_KEYS = ["lastName", "firstName", "middleName"];
    if (NAME_KEYS.some((key) => present(fields, key))) {
      const current = components(findOne(card, "N"));
      const merged = NAME_KEYS.map((key, index) =>
        escapeText(present(fields, key) ? String(fields[key]) : current[index] || "")
      ).concat([escapeText(current[3] || ""), escapeText(current[4] || "")]);
      setLine(card, "N", merged.some(Boolean) ? merged.join(";") : "");
    }
    if (present(fields, "displayName")) {
      setLine(card, "FN", escapeText(String(fields.displayName)));
    }
    if (present(fields, "emails")) {
      setMulti(card, "EMAIL", entriesOf(fields.emails));
    }
    if (present(fields, "phones")) {
      setMulti(card, "TEL", entriesOf(fields.phones));
    }
    if (present(fields, "organisation") || present(fields, "department")) {
      const current = components(findOne(card, "ORG"));
      const merged = ["organisation", "department"].map((key, index) =>
        escapeText(present(fields, key) ? String(fields[key]) : current[index] || "")
      );
      while (merged.length && merged[merged.length - 1] === "") {
        merged.pop();
      }
      setLine(card, "ORG", merged.join(";"));
    }
    for (const [key, name] of [
      ["jobTitle", "TITLE"],
      ["notes", "NOTE"],
      ["nickname", "NICKNAME"],
      ["birthday", "BDAY"],
    ]) {
      if (present(fields, key)) {
        setLine(card, name, escapeText(String(fields[key])));
      }
    }
    // FN is mandatory in vCard 4.0, and a card without it shows as a blank row in
    // the address book, which the user cannot then click on to fix.
    if (!findOne(card, "FN")) {
      const name = components(findOne(card, "N"));
      const guess = [name[1], name[0]].filter(Boolean).join(" ") || textOf(findOne(card, "EMAIL"));
      if (guess) {
        card.lines.unshift(lineOf("FN", escapeText(guess)));
      }
    }
    return card;
  }

  /** The card to write: an explicit vCard wins, otherwise fields over `baseText`. */
  function cardFor(params, baseText) {
    if (params.vCard) {
      // Round-trip a supplied card so a body-only fragment still gets BEGIN/END and
      // CRLF line endings, which Thunderbird's parser expects.
      const supplied = parseCard(params.vCard);
      if (!supplied.lines.length) {
        throw tbxError.usage(
          "that vCard contained no properties — send at least FN, or use the " +
            "individual fields instead"
        );
      }
      return serialize(supplied);
    }
    const card = applyFields(parseCard(baseText || ""), params.fields || {});
    if (!card.lines.length) {
      throw tbxError.usage("nothing to write — pass display_name, emails, or a vCard");
    }
    return serialize(card);
  }

  function vCardArgument(card) {
    return browser.runtime.getManifest().manifest_version >= 3 ? card : { vCard: card };
  }

  // ------------------------------------------------------------------ node views

  function cardTextOf(node) {
    return node.vCard || (node.properties && node.properties.vCard) || "";
  }

  function contactView(node) {
    const card = parseCard(cardTextOf(node));
    return {
      id: node.id,
      addressBookId: node.parentId,
      readOnly: Boolean(node.readOnly),
      remote: Boolean(node.remote),
      ...describe(card, node),
      hasPhoto: card.lines.some((line) => line.name === "PHOTO"),
      vCard: serialize(card, BULKY),
    };
  }

  function bookView(book) {
    return {
      id: book.id,
      name: book.name,
      readOnly: Boolean(book.readOnly),
      remote: Boolean(book.remote),
      contactCount: book.contacts ? book.contacts.length : undefined,
      mailingListCount: book.mailingLists ? book.mailingLists.length : undefined,
    };
  }

  function listView(node, members) {
    return {
      id: node.id,
      addressBookId: node.parentId,
      name: node.name,
      nickName: node.nickName || null,
      description: node.description || null,
      readOnly: Boolean(node.readOnly),
      remote: Boolean(node.remote),
      memberCount: members ? members.length : undefined,
    };
  }

  /** Every book, in a stable order — `contacts.list` cursors index into this. */
  async function bookIdsFor(addressBookId) {
    if (addressBookId) {
      return [addressBookId];
    }
    const books = await browser.addressBooks.list(false);
    return books.map((book) => book.id).sort();
  }

  async function writableBook(id) {
    const book = await browser.addressBooks.get(id, false);
    if (book.readOnly) {
      throw tbxError.usage(
        `the address book "${book.name}" is read-only — use a writable one ` +
          "(addressbooks.list reports readOnly per book)"
      );
    }
    return book;
  }

  async function photoOf(id) {
    const file = await browser.contacts.getPhoto(id);
    if (!file) {
      return null;
    }
    const buffer = await file.arrayBuffer();
    return {
      name: file.name || null,
      contentType: file.type || null,
      bytes: buffer.byteLength,
      base64: tbxBase64.fromArrayBuffer(buffer),
    };
  }

  // --------------------------------------------------------------- address books

  tbxRegistry.define("addressbooks.list", async (params) => {
    // Counts mean loading every card in every book, so they are opt-out rather than
    // unconditional; on a CardDAV book that is a real cost.
    const complete = params.includeCounts !== false;
    const books = await browser.addressBooks.list(complete);
    return { books: (books || []).map(bookView) };
  });

  tbxRegistry.define("addressbooks.create", async (params) => {
    const name = tbxUtil.need(params, "name", "string");
    const id = await browser.addressBooks.create({ name });
    return { book: bookView(await browser.addressBooks.get(id, false)) };
  });

  tbxRegistry.define("addressbooks.delete", async (params) => {
    const id = tbxUtil.need(params, "id", "string");
    // Read it complete first: the counts are the only record of what went, and the
    // Python side reports them so the user knows the size of what they approved.
    const book = await browser.addressBooks.get(id, true);
    const previous = bookView(book);
    await browser.addressBooks.delete(id);
    return { deleted: true, previous };
  });

  // -------------------------------------------------------------------- contacts

  function decodeCursor(cursor) {
    if (!cursor) {
      return { book: 0, offset: 0 };
    }
    const match = /^(\d+):(\d+)$/.exec(String(cursor));
    if (!match) {
      throw tbxError.usage("that cursor did not come from contacts.list — omit it to start again");
    }
    return { book: Number(match[1]), offset: Number(match[2]) };
  }

  tbxRegistry.define("contacts.list", async (params) => {
    const limit = tbxUtil.limit(params.limit, DEFAULT_LIMIT, 500);
    const bookIds = await bookIdsFor(params.addressBookId);
    const start = decodeCursor(params.cursor);
    const contacts = [];
    let offset = start.offset;
    for (let index = start.book; index < bookIds.length; index += 1, offset = 0) {
      const nodes = await browser.contacts.list(bookIds[index]);
      for (let i = offset; i < nodes.length; i += 1) {
        if (contacts.length >= limit) {
          return { contacts, cursor: `${index}:${i}` };
        }
        contacts.push(contactView(nodes[i]));
      }
    }
    return { contacts, cursor: null };
  });

  /** Substring match over the flattened view, for what quickSearch cannot see. */
  async function scan(bookIds, query, limit) {
    const needle = query.toLowerCase();
    const hits = [];
    let seen = 0;
    for (const bookId of bookIds) {
      for (const node of await browser.contacts.list(bookId)) {
        seen += 1;
        if (seen > MAX_SCAN || hits.length >= limit) {
          return hits;
        }
        const item = contactView(node);
        const haystack = [
          item.displayName,
          item.organisation,
          item.jobTitle,
          item.nickname,
          ...item.emails.map((entry) => entry.value),
          ...item.phones.map((entry) => entry.value),
        ]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        if (haystack.includes(needle)) {
          hits.push(item);
        }
      }
    }
    return hits;
  }

  tbxRegistry.define("contacts.search", async (params) => {
    const query = tbxUtil.need(params, "query", "string");
    const limit = tbxUtil.limit(params.limit, SEARCH_LIMIT, 200);
    const queryInfo = {
      searchString: query,
      includeLocal: params.includeLocal !== false,
      includeRemote: params.includeRemote !== false,
      includeReadOnly: params.includeReadOnly !== false,
      includeReadWrite: true,
    };
    let found;
    if (browser.contacts.query) {
      // The MV3 spelling. Kept first so bumping the manifest changes nothing here.
      found = await browser.contacts.query({
        ...queryInfo,
        parentId: params.addressBookId || undefined,
      });
    } else if (params.addressBookId) {
      found = await browser.contacts.quickSearch(params.addressBookId, queryInfo);
    } else {
      found = await browser.contacts.quickSearch(queryInfo);
    }
    let contacts = (found || []).map(contactView);
    let fallbackScan = false;
    if (!contacts.length) {
      // quickSearch only looks at the fields named in
      // mail.addr_book.quicksearchquery.format, so an organisation, a job title or a
      // second address never matches. A local scan is cheap and answers the question.
      contacts = await scan(await bookIdsFor(params.addressBookId), query, limit);
      fallbackScan = true;
    }
    return {
      contacts: contacts.slice(0, limit),
      matched: contacts.length,
      fallbackScan,
      query,
    };
  });

  tbxRegistry.define("contacts.get", async (params) => {
    const id = tbxUtil.need(params, "id", "string");
    const node = await browser.contacts.get(id);
    const contact = contactView(node);
    const book = await browser.addressBooks.get(node.parentId, false).catch(() => null);
    if (book) {
      contact.addressBookName = book.name;
    }
    if (params.includePhoto && contact.hasPhoto) {
      contact.photo = await photoOf(id);
    }
    return { contact };
  });

  tbxRegistry.define("contacts.getPhoto", async (params) => {
    const id = tbxUtil.need(params, "id", "string");
    const photo = await photoOf(id);
    return { hasPhoto: Boolean(photo), photo };
  });

  tbxRegistry.define("contacts.create", async (params) => {
    const parentId = tbxUtil.need(params, "addressBookId", "string");
    await writableBook(parentId);
    const card = cardFor(params, "");
    const id = await browser.contacts.create(parentId, vCardArgument(card));
    return { contact: contactView(await browser.contacts.get(id)) };
  });

  tbxRegistry.define("contacts.update", async (params) => {
    const id = tbxUtil.need(params, "id", "string");
    const before = await browser.contacts.get(id);
    if (before.readOnly) {
      throw tbxError.usage(`contact ${id} is read-only, so it cannot be changed here`);
    }
    const previous = contactView(before);
    // Writing a card replaces the whole contact, so field edits are merged onto the
    // card that is already there — otherwise setting a phone number wipes the name.
    const card = cardFor(params, cardTextOf(before));
    await browser.contacts.update(id, vCardArgument(card));
    return { previous, current: contactView(await browser.contacts.get(id)) };
  });

  tbxRegistry.define("contacts.delete", async (params) => {
    const id = tbxUtil.need(params, "id", "string");
    const previous = contactView(await browser.contacts.get(id));
    await browser.contacts.delete(id);
    return { deleted: true, previous };
  });

  // --------------------------------------------------------------- mailing lists

  tbxRegistry.define("mailinglists.list", async (params) => {
    const bookIds = await bookIdsFor(params.addressBookId);
    const lists = [];
    for (const bookId of bookIds) {
      for (const node of await browser.mailingLists.list(bookId)) {
        // Members are a local query and "who is on it" is always the next question,
        // so the count comes for free; the full roster stays opt-in.
        const members = await browser.mailingLists.listMembers(node.id);
        const entry = listView(node, members);
        if (params.includeMembers) {
          entry.members = members.map(contactView);
        }
        lists.push(entry);
      }
    }
    return { lists };
  });

  tbxRegistry.define("mailinglists.create", async (params) => {
    const parentId = tbxUtil.need(params, "addressBookId", "string");
    const name = tbxUtil.need(params, "name", "string");
    await writableBook(parentId);
    const id = await browser.mailingLists.create(parentId, {
      name,
      nickName: params.nickName || "",
      description: params.description || "",
    });
    return { list: listView(await browser.mailingLists.get(id), []) };
  });

  tbxRegistry.define("mailinglists.update", async (params) => {
    const id = tbxUtil.need(params, "id", "string");
    const before = await browser.mailingLists.get(id);
    // `name` is required by the API, so unspecified fields are carried over rather
    // than blanked.
    const properties = {
      name: params.name === undefined || params.name === null ? before.name : String(params.name),
      nickName:
        params.nickName === undefined || params.nickName === null
          ? before.nickName || ""
          : String(params.nickName),
      description:
        params.description === undefined || params.description === null
          ? before.description || ""
          : String(params.description),
    };
    const previous = listView(before);
    await browser.mailingLists.update(id, properties);
    return { previous, current: listView(await browser.mailingLists.get(id)) };
  });

  tbxRegistry.define("mailinglists.delete", async (params) => {
    const id = tbxUtil.need(params, "id", "string");
    const before = await browser.mailingLists.get(id);
    const previous = listView(before, await browser.mailingLists.listMembers(id));
    await browser.mailingLists.delete(id);
    return { deleted: true, previous };
  });

  tbxRegistry.define("mailinglists.members", async (params) => {
    const id = tbxUtil.need(params, "id", "string");
    const node = await browser.mailingLists.get(id);
    const members = await browser.mailingLists.listMembers(id);
    return { list: listView(node, members), members: members.map(contactView) };
  });

  tbxRegistry.define("mailinglists.addMember", async (params) => {
    const id = tbxUtil.need(params, "id", "string");
    const contactId = tbxUtil.need(params, "contactId", "string");
    const node = await browser.mailingLists.get(id);
    const contact = await browser.contacts.get(contactId);
    // Adding across books copies the card into the list's book; say so, because the
    // caller now has two contacts where it had one.
    const copied = contact.parentId !== node.parentId;
    await browser.mailingLists.addMember(id, contactId);
    const members = await browser.mailingLists.listMembers(id);
    return {
      added: true,
      copiedToAddressBook: copied,
      list: listView(node, members),
      contact: contactView(contact),
    };
  });

  tbxRegistry.define("mailinglists.removeMember", async (params) => {
    const id = tbxUtil.need(params, "id", "string");
    const contactId = tbxUtil.need(params, "contactId", "string");
    const node = await browser.mailingLists.get(id);
    const contact = await browser.contacts.get(contactId);
    await browser.mailingLists.removeMember(id, contactId);
    const members = await browser.mailingLists.listMembers(id);
    return {
      removed: true,
      list: listView(node, members),
      contact: contactView(contact),
    };
  });
}

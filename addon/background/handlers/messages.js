/* Message handlers — the reference implementation for a handler module.
 *
 * Shape to copy:
 *   - one `tbxRegistry.define("<module>.<action>", async (params, ctx) => …)` per method
 *   - validate with `tbxUtil.need`, fail with `tbxError.usage` so the model can retry
 *   - return plain JSON; the Python side owns presentation
 *   - use `tbxUtil.mapLimited` for bulk work so a 500-message operation neither
 *     stalls the UI nor opens 500 IMAP requests at once
 *
 * Pagination note: Thunderbird hands out a `MessageList` — one page of messages
 * plus an `id` that continues *after* that page. Page size is the user's own
 * preference (`extensions.webextensions.messagesPerPage`), so a `limit` usually
 * runs out mid-page, and returning the list id there would skip everything
 * between the two. So a cursor is one of:
 *   - a raw Thunderbird list id, when the page ended exactly on the limit and
 *     nothing was left over;
 *   - `tbx:<load>:<n>`, ours, naming the tail we parked (with the id that
 *     continues after it) because the limit stopped us mid-page. `<load>` names
 *     this load of the script, so a cursor from before a background restart is
 *     refused rather than mistaken for one of ours.
 * Only the last 32 part-read pages are kept; the rest are dropped, and their
 * Thunderbird lists aborted, so an abandoned walk costs nothing.
 */

{
  const DEFAULT_LIMIT = 25;
  const BULK_CONCURRENCY = 8;
  const PARKED_PAGES = 32;
  const CURSOR_PREFIX = "tbx:";

  /** Flatten a MessageHeader into the shape _common.message_summary expects. */
  function header(message) {
    if (!message) {
      return null;
    }
    return {
      id: message.id,
      headerMessageId: message.headerMessageId,
      subject: message.subject,
      author: message.author,
      recipients: message.recipients || [],
      ccList: message.ccList || [],
      bccList: message.bccList || [],
      date: message.date ? new Date(message.date).toISOString() : null,
      read: message.read,
      new: message.new,
      flagged: message.flagged,
      junk: message.junk,
      junkScore: message.junkScore,
      tags: message.tags || [],
      size: message.size,
      priority: message.priority,
      external: message.external,
      folderId: message.folder ? message.folder.id : undefined,
      folderPath: message.folder ? message.folder.path : undefined,
    };
  }

  /* Pages we stopped part-way through, by the cursor we minted for them. Bounded,
   * because a caller that walks away from a search must not pin its messages —
   * and its Thunderbird list — for the rest of the session. */
  const parked = new Map();
  let parkSequence = 0;

  /* Every cursor we mint names this load of the script. The map above is empty
   * again after a background restart, and without this a cursor from before it
   * would quietly claim the new load's first parked page instead of being
   * refused. */
  const LOAD_ID = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;

  /** Let go of a Thunderbird list nobody can reach any more. */
  function abandonList(listId) {
    if (listId) {
      // Best effort: the list would expire on its own, this is just sooner.
      Promise.resolve(browser.messages.abortList(listId)).catch(() => {});
    }
  }

  /** Hold `rest` for the next call, and return the cursor that claims it back. */
  function park(listId, rest) {
    if (parked.size >= PARKED_PAGES) {
      const [oldest] = parked.keys(); // a Map iterates in insertion order
      abandonList(parked.get(oldest).listId);
      parked.delete(oldest);
    }
    parkSequence += 1;
    const cursor = `${CURSOR_PREFIX}${LOAD_ID}:${parkSequence}`;
    parked.set(cursor, { listId, rest });
    return cursor;
  }

  /**
   * Take back a page we parked, or say why the cursor is worthless.
   *
   * Anything carrying our prefix is ours to answer for: a cursor from another
   * load carries another load id, so it is simply not in the map, and it is
   * refused here rather than handed to Thunderbird as if it were a list id.
   */
  function unpark(cursor) {
    const held = parked.get(cursor);
    if (!held) {
      throw tbxError.usage(
        `that cursor is no longer valid: it was evicted (only ${PARKED_PAGES} part-read ` +
          "pages are kept) or belongs to an earlier Thunderbird session — re-run the " +
          "search without a cursor"
      );
    }
    parked.delete(cursor);
    return held;
  }

  /**
   * Collect up to `limit` headers, starting a walk or resuming one.
   *
   * @param {string|null} cursor  ours (`tbx:<n>`) or a raw Thunderbird list id.
   * @param {Function} start  opens the list, when there is no cursor to resume.
   */
  async function collectPage(cursor, start, limit) {
    const messages = [];
    let pending = []; // fetched but not yet returned, in order
    let listId = null; // the Thunderbird list that continues after `pending`

    const absorb = (page) => {
      pending = page && page.messages ? [...page.messages] : [];
      listId = (page && page.id) || null;
    };

    if (!cursor) {
      absorb(await start());
    } else if (cursor.startsWith(CURSOR_PREFIX)) {
      const held = unpark(cursor);
      pending = held.rest;
      listId = held.listId;
    } else {
      try {
        absorb(await browser.messages.continueList(cursor));
      } catch (ex) {
        throw tbxError.usage(
          "that cursor has expired (Thunderbird drops message lists when it restarts " +
            "or after a timeout) — re-run the search without a cursor"
        );
      }
    }

    for (;;) {
      while (pending.length) {
        if (messages.length >= limit) {
          // Stopped mid-page: park the rest, because the list id continues after
          // the whole page and would skip every message still sitting here.
          return { messages, cursor: park(listId, pending) };
        }
        messages.push(header(pending.shift()));
      }
      if (messages.length >= limit || !listId) {
        // Nothing left over, so Thunderbird's own id is the cursor; when the list
        // is spent there is no id and the walk is over.
        return { messages, cursor: listId };
      }
      absorb(await browser.messages.continueList(listId));
      if (!pending.length) {
        // An empty page ends this call; its id, if any, can still be resumed.
        return { messages, cursor: listId };
      }
    }
  }

  // --------------------------------------------------------------------- query

  /**
   * Run a query and return its first page.
   *
   * Never ask for `returnMessageListId`: that flag makes Thunderbird answer with
   * the list id itself, a bare string with no messages on it, which is how every
   * search used to come back empty. A build that answers with one anyway is one
   * `continueList` away from the page we wanted.
   */
  async function startQuery(query) {
    const first = await browser.messages.query(query);
    return typeof first === "string" ? browser.messages.continueList(first) : first;
  }

  /** A folder or account id, or a list of them, as a list — or null for neither. */
  function idList(value) {
    if (!value) {
      return null;
    }
    return Array.isArray(value) ? [...value] : [value];
  }

  /**
   * What the search was aimed at, echoed back so the answer says what it covered
   * without anyone having to enumerate folders to find out.
   */
  function queryScope(query) {
    const folderIds = idList(query.folderId);
    const accountIds = idList(query.accountId);
    return {
      folderIds,
      accountIds,
      // There is nothing to recurse into unless a folder or account was named.
      includeSubFolders:
        typeof query.includeSubFolders === "boolean"
          ? query.includeSubFolders
          : Boolean(folderIds || accountIds),
    };
  }

  tbxRegistry.define("messages.query", async (params) => {
    const limit = params.limit || DEFAULT_LIMIT;
    const query = Object.assign({}, params.query || {});
    // Any positive page size is legal; asking for exactly what the caller wants
    // keeps the common case to one round trip. Thunderbird may still cut a page
    // short (autoPaginationTimeout), so the walk below loops regardless.
    query.messagesPerPage = limit;

    const paged = await collectPage(params.cursor, () => startQuery(query), limit);
    const result = { messages: paged.messages, cursor: paged.cursor };
    if (!params.cursor) {
      // Only on the first page: a continuation is by definition the same search.
      result.scope = queryScope(query);
    }
    if (query.fullText && browser.tbx) {
      // Worth saying: fullText only sees what the global indexer has processed.
      const indexed = await browser.tbx.globalIndexEnabled().catch(() => null);
      if (indexed === false) {
        result.indexNote =
          "Thunderbird's global index is disabled, so full_text matched nothing. " +
          "Use subject/author/body filters, or enable indexing in Settings > General.";
      }
    }
    return result;
  });

  tbxRegistry.define("messages.list", async (params) => {
    const folderId = tbxUtil.need(params, "folderId", "string");
    const limit = params.limit || DEFAULT_LIMIT;
    const paged = await collectPage(
      params.cursor,
      () =>
        browser.messages.list(folderId, {
          sortType: params.sortType || "date",
          sortOrder: params.sortOrder || "descending",
        }),
      limit
    );
    return { messages: paged.messages, cursor: paged.cursor, folderId };
  });

  // ---------------------------------------------------------------------- read

  /** Depth-first walk of the MIME tree, collecting text bodies and attachments. */
  function walkParts(part, out) {
    if (!part) {
      return;
    }
    const contentType = (part.contentType || "").toLowerCase();
    if (part.body && (contentType.startsWith("text/") || !contentType)) {
      out.texts.push({ contentType: contentType || "text/plain", body: part.body });
    }
    if (part.name && part.partName) {
      out.attachments.push({
        partName: part.partName,
        name: part.name,
        contentType: part.contentType,
        size: part.size,
      });
    }
    for (const child of part.parts || []) {
      walkParts(child, out);
    }
  }

  function pickBody(texts, preferHtml) {
    const plain = texts.find((t) => t.contentType.startsWith("text/plain"));
    const html = texts.find((t) => t.contentType.startsWith("text/html"));
    if (preferHtml && html) {
      return { body: html.body, isHtml: true };
    }
    if (plain) {
      return { body: plain.body, isHtml: false };
    }
    if (html) {
      return { body: html.body, isHtml: true };
    }
    return { body: null, isHtml: false };
  }

  async function readOne(messageId, detail, decrypt) {
    const meta = await browser.messages.get(messageId);
    if (detail === "summary") {
      return { header: header(meta), attachments: [] };
    }
    const full = await browser.messages.getFull(messageId, {
      decrypt: decrypt !== false,
      decodeContent: true,
      decodeHeaders: true,
    });
    const collected = { texts: [], attachments: [] };
    walkParts(full, collected);
    const chosen = pickBody(collected.texts, false);
    const payload = {
      header: header(meta),
      body: chosen.body,
      bodyIsHtml: chosen.isHtml,
      attachments: collected.attachments,
      decryptionStatus: full.decryptionStatus,
    };
    if (detail === "full") {
      payload.headers = full.headers || {};
      payload.parts = collected.texts.map((t) => ({
        contentType: t.contentType,
        bytes: t.body ? t.body.length : 0,
      }));
      // An HTML alternative is genuinely useful at `full` detail.
      const html = collected.texts.find((t) => t.contentType.startsWith("text/html"));
      if (html && !chosen.isHtml) {
        payload.htmlBody = html.body;
      }
    }
    return payload;
  }

  tbxRegistry.define("messages.read", async (params) => {
    const messageId = tbxUtil.need(params, "messageId", "int");
    return readOne(messageId, params.detail || "text", params.decrypt);
  });

  tbxRegistry.define("messages.readMany", async (params, ctx) => {
    const ids = tbxUtil.need(params, "messageIds", "array");
    const detail = params.detail || "summary";
    let done = 0;
    const results = await tbxUtil.mapLimited(ids, BULK_CONCURRENCY, async (id) => {
      const value = await readOne(id, detail, params.decrypt);
      done += 1;
      ctx.progress(done, ids.length, "reading messages");
      return value;
    });
    const { values, failures } = tbxUtil.partition(results);
    return { messages: values, failures };
  });

  tbxRegistry.define("messages.raw", async (params) => {
    const messageId = tbxUtil.need(params, "messageId", "int");
    let source;
    try {
      source = await browser.messages.getRaw(messageId, {
        decrypt: Boolean(params.decrypt),
        data_format: "BinaryString",
      });
    } catch (ex) {
      throw tbxError.unsupported(
        "the raw source is not available — on IMAP the message must be stored " +
          "offline first (right-click the folder > Properties > Synchronisation), " +
          `underlying error: ${ex.message || ex}`
      );
    }
    return { source, bytes: source ? source.length : 0 };
  });

  // --------------------------------------------------------------- attachments

  tbxRegistry.define("messages.listAttachments", async (params) => {
    const messageId = tbxUtil.need(params, "messageId", "int");
    const attachments = await browser.messages.listAttachments(messageId);
    return {
      attachments: (attachments || []).map((a) => ({
        partName: a.partName,
        name: a.name,
        contentType: a.contentType,
        size: a.size,
      })),
    };
  });

  tbxRegistry.define("messages.saveAttachment", async (params) => {
    const messageId = tbxUtil.need(params, "messageId", "int");
    const partName = tbxUtil.need(params, "partName", "string");
    const directory = tbxUtil.need(params, "directory", "string");
    const file = await browser.messages.getAttachmentFile(messageId, partName);
    if (!file) {
      throw tbxError.usage(`no attachment ${partName} on message ${messageId}`);
    }
    const buffer = await file.arrayBuffer();
    if (!browser.tbx) {
      throw tbxError.unsupported(
        "saving files needs the privileged half of the add-on, which did not load"
      );
    }
    const written = await browser.tbx.writeFile({
      directory,
      filename: params.filename || file.name,
      base64: tbxBase64.fromArrayBuffer(buffer),
      overwrite: Boolean(params.overwrite),
    });
    return { path: written.path, bytes: written.bytes, name: written.name };
  });

  // ------------------------------------------------------------------ mutation

  tbxRegistry.define("messages.mark", async (params, ctx) => {
    const ids = tbxUtil.need(params, "messageIds", "array");
    const addTags = params.addTags || [];
    const removeTags = params.removeTags || [];
    let done = 0;
    const results = await tbxUtil.mapLimited(ids, BULK_CONCURRENCY, async (id) => {
      const properties = {};
      if (params.read !== null && params.read !== undefined) {
        properties.read = params.read;
      }
      if (params.flagged !== null && params.flagged !== undefined) {
        properties.flagged = params.flagged;
      }
      if (params.junk !== null && params.junk !== undefined) {
        properties.junk = params.junk;
      }
      if (addTags.length || removeTags.length) {
        const current = await browser.messages.get(id);
        const next = new Set(current.tags || []);
        for (const tag of addTags) {
          next.add(tag);
        }
        for (const tag of removeTags) {
          next.delete(tag);
        }
        properties.tags = [...next];
      }
      await browser.messages.update(id, properties);
      done += 1;
      ctx.progress(done, ids.length, "updating messages");
      return id;
    });
    const { values, failures } = tbxUtil.partition(results);
    return { updated: values.length, failures };
  });

  tbxRegistry.define("messages.move", async (params) => {
    const ids = tbxUtil.need(params, "messageIds", "array");
    const destination = tbxUtil.need(params, "destinationFolderId", "string");
    // Capture the source folders first: after the move the ids are gone, and the
    // Python side reports them so a user can undo by hand.
    const sources = await tbxUtil.mapLimited(ids, BULK_CONCURRENCY, async (id) => {
      const message = await browser.messages.get(id);
      return message.folder ? message.folder.id : null;
    });
    const sourceFolderIds = [
      ...new Set(tbxUtil.partition(sources).values.filter(Boolean)),
    ];
    await browser.messages.move(ids, destination);
    return { moved: ids.length, sourceFolderIds };
  });

  tbxRegistry.define("messages.copy", async (params) => {
    const ids = tbxUtil.need(params, "messageIds", "array");
    const destination = tbxUtil.need(params, "destinationFolderId", "string");
    await browser.messages.copy(ids, destination);
    return { copied: ids.length };
  });

  tbxRegistry.define("messages.archive", async (params) => {
    const ids = tbxUtil.need(params, "messageIds", "array");
    await browser.messages.archive(ids);
    return { archived: ids.length };
  });

  tbxRegistry.define("messages.delete", async (params) => {
    const ids = tbxUtil.need(params, "messageIds", "array");
    const permanent = Boolean(params.permanent);
    await browser.messages.delete(ids, { deletePermanently: permanent });
    return { deleted: ids.length, permanent };
  });

  tbxRegistry.define("messages.import", async (params) => {
    const folderId = tbxUtil.need(params, "folderId", "string");
    const base64 = tbxUtil.need(params, "base64", "string");
    const blob = new Blob([tbxBase64.toUint8Array(base64)], { type: "message/rfc822" });
    const file = new File([blob], params.filename || "imported.eml", {
      type: "message/rfc822",
    });
    const message = await browser.messages.import(file, folderId, {
      read: params.read !== false,
      new: false,
      tags: params.tags || [],
    });
    return { message: header(message) };
  });
}

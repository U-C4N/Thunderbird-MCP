/* Message handlers — the reference implementation for a handler module.
 *
 * Shape to copy:
 *   - one `tbxRegistry.define("<module>.<action>", async (params, ctx) => …)` per method
 *   - validate with `tbxUtil.need`, fail with `tbxError.usage` so the model can retry
 *   - return plain JSON; the Python side owns presentation
 *   - use `tbxUtil.mapLimited` for bulk work so a 500-message operation neither
 *     stalls the UI nor opens 500 IMAP requests at once
 *
 * Pagination note: Thunderbird returns a `MessageList` with an opaque `id` plus a
 * first page, and `messages.continueList(id)` walks it. We surface that id as our
 * `cursor` — except when a caller's `limit` lands mid-page, where the cursor also
 * has to stand for the part of the page already fetched but not handed out. See
 * `collectPage`.
 */

{
  const DEFAULT_LIMIT = 25;
  const BULK_CONCURRENCY = 8;

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

  /** Resolve whatever `query`/`list` handed back into an actual MessageList.
   *
   *  `messages.query({returnMessageListId: true})` does not return a MessageList:
   *  its schema return type is `MessageList | string`, and with the flag set
   *  MessageQuery.startSearch() returns `this.messageList.id` — a bare string —
   *  so the caller can hold the list id before the first page has filled. Walking
   *  that string as if it were a page yielded no `.messages` and no `.id`, so
   *  every messages.query answered `{messages: [], cursor: null}` no matter what
   *  was asked. Trade the id for its first page here. */
  async function asMessageList(listOrId) {
    if (typeof listOrId === "string") {
      return browser.messages.continueList(listOrId);
    }
    return listOrId;
  }

  /** The tail of a page we fetched but have not handed out yet, by cursor.
   *
   *  Thunderbird chooses the page size and we cannot always override it —
   *  `messages.list` takes no `messagesPerPage` at all. So whenever `limit` is
   *  smaller than the page, stopping mid-page and returning the list id stranded
   *  the remainder: `continueList` advances to the *next* page, and those
   *  messages became unreachable. Measured on a 586-message folder, paging 3 at a
   *  time skipped 9 of every 12. Hold the tail against the cursor instead. */
  const carriedOver = new Map();
  const MAX_CARRIED = 32;
  let tailSequence = 0;

  /** Park `rest` and return the cursor that will serve it.
   *
   *  `listId` is null on a final page — there is nothing left to continue from,
   *  so mint a key rather than tell the caller the walk is over with messages
   *  still in hand. */
  function carryOver(listId, rest) {
    if (carriedOver.size >= MAX_CARRIED) {
      // Insertion-ordered: drop the least recently parked. An abandoned search
      // must not pin a page for the life of the session.
      carriedOver.delete(carriedOver.keys().next().value);
    }
    const key = listId || `tail-${(tailSequence += 1)}`;
    carriedOver.set(key, { messages: rest, listId });
    return key;
  }

  /** Fetch up to `limit` messages, resuming from `cursor` when there is one. */
  async function collectPage(cursor, start, limit) {
    const messages = [];
    let current;

    if (cursor && carriedOver.has(cursor)) {
      const held = carriedOver.get(cursor);
      carriedOver.delete(cursor);
      current = { messages: held.messages, id: held.listId };
    } else if (cursor) {
      try {
        current = await browser.messages.continueList(cursor);
      } catch (ex) {
        throw tbxError.usage(
          "that cursor has expired (Thunderbird drops message lists when it restarts " +
            "or after a timeout) — re-run the search without a cursor"
        );
      }
    } else {
      current = await asMessageList(await start());
    }

    while (current) {
      const page = current.messages || [];
      for (let index = 0; index < page.length; index += 1) {
        messages.push(header(page[index]));
        if (messages.length >= limit) {
          const rest = page.slice(index + 1);
          return {
            messages,
            cursor: rest.length ? carryOver(current.id, rest) : current.id || null,
          };
        }
      }
      if (!current.id) {
        return { messages, cursor: null };
      }
      current = await browser.messages.continueList(current.id);
      if (!current || !current.messages || current.messages.length === 0) {
        return { messages, cursor: current && current.id ? current.id : null };
      }
    }
    return { messages, cursor: null };
  }

  // --------------------------------------------------------------------- query

  function collectFolderIds(folders, out) {
    for (const folder of folders || []) {
      if (folder.id) {
        out.push(folder.id);
      }
      collectFolderIds(folder.subFolders, out);
    }
    return out;
  }

  /** Which folders the query covered, so an empty result can be interpreted.
   *
   *  Thunderbird does not report this back, but its scoping rule is fixed
   *  (ExtensionMessages.sys.mjs::MessageQuery.startSearch): `folderId` scopes to
   *  those folders, plus their descendants when `includeSubFolders` is set; with
   *  no `folderId` every folder of the named accounts — or of every account — is
   *  searched. Mirror that rule rather than guess at it. An unscoped search names
   *  the accounts instead of enumerating thousands of folder ids. */
  function accountIdOf(folderId) {
    const cut = String(folderId).indexOf(":/");
    return cut > 0 ? String(folderId).slice(0, cut) : null;
  }

  async function searchedScope(query) {
    const named = query.accountId ? [].concat(query.accountId) : null;
    if (query.folderId) {
      // With both set Thunderbird intersects them: a folder outside the named
      // accounts is dropped, not searched.
      const roots = [].concat(query.folderId).filter(
        (id) => !named || named.includes(accountIdOf(id))
      );
      const folderIds = roots.slice();
      if (query.includeSubFolders) {
        for (const root of roots) {
          const children = await browser.folders.getSubFolders(root, true).catch(() => []);
          collectFolderIds(children, folderIds);
        }
      }
      return {
        scope: "folders",
        accountIds: [...new Set(roots.map(accountIdOf).filter(Boolean))],
        folderIds,
        includeSubFolders: Boolean(query.includeSubFolders),
      };
    }
    const accountIds =
      named || ((await browser.accounts.list(false).catch(() => [])) || []).map((a) => a.id);
    return {
      scope: named ? "accounts" : "all-accounts",
      accountIds,
      includeSubFolders: true,
    };
  }

  tbxRegistry.define("messages.query", async (params) => {
    const limit = params.limit || DEFAULT_LIMIT;
    const query = Object.assign({}, params.query || {});
    // Ask Thunderbird for a resumable list rather than one giant array.
    query.returnMessageListId = true;
    query.messagesPerPage = Math.min(Math.max(limit, 10), 100);

    const paged = await collectPage(params.cursor, () => browser.messages.query(query), limit);
    const result = {
      messages: paged.messages,
      cursor: paged.cursor,
      searchedFolders: await searchedScope(query),
    };
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

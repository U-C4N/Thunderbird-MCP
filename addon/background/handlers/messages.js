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
 * `cursor` unchanged.
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

  /** Walk a MessageList to at most `limit` items, returning a continuation cursor. */
  async function takePage(list, limit) {
    const messages = [];
    let current = list;
    while (current) {
      for (const message of current.messages || []) {
        messages.push(header(message));
        if (messages.length >= limit) {
          // Keep the list alive so the caller can continue from it.
          return { messages, cursor: current.id || null };
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

  async function resumeOrStart(cursor, start) {
    if (cursor) {
      try {
        return await browser.messages.continueList(cursor);
      } catch (ex) {
        throw tbxError.usage(
          "that cursor has expired (Thunderbird drops message lists when it restarts " +
            "or after a timeout) — re-run the search without a cursor"
        );
      }
    }
    return start();
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

    const list = await resumeOrStart(params.cursor, () => startQuery(query));
    const paged = await takePage(list, limit);
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
    const list = await resumeOrStart(params.cursor, () =>
      browser.messages.list(folderId, {
        sortType: params.sortType || "date",
        sortOrder: params.sortOrder || "descending",
      })
    );
    const paged = await takePage(list, limit);
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

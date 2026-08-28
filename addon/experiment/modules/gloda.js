/* Gloda — Thunderbird's global message index.
 *
 * This is the only ranked, whole-corpus search Thunderbird has. `messages.query`
 * with `fullText` also goes through gloda, but it hands back a folder-ordered
 * list with the ranking thrown away, which is exactly the part worth keeping.
 *
 * Gloda is callback-driven and has no cancellation: you hand a query a
 * collection listener and it calls `onQueryCompleted` eventually, or never. Every
 * entry point here therefore wraps the listener in a promise with a deadline, so
 * a wedged indexer costs one request instead of the session.
 *
 * Ids: gloda knows nothing about WebExtension message ids, and the id space
 * lives in the *built-in* API sandbox, not ours (experiment APIs get their own
 * LazyAPIManager, so ext-mail.js's globals are not in scope here). We reach the
 * tracker through ExtensionParent when we can, and always return
 * headerMessageId + folder + subject as well, so a caller can still find the
 * message through mail_search when we cannot.
 */

TBX_MODULE_NAMES.push("gloda");

{
  const SEARCH_TIMEOUT_MS = 45000;
  const LOOKUP_TIMEOUT_MS = 30000;
  const COUNT_TIMEOUT_MS = 10000;
  const SNIPPET_CHARS = 240;
  const MAX_HITS = 200;
  const MAX_RETRIEVE = 1000;

  const _extras = new Map();

  /** Import a module core.js's shared MODULE_URLS does not name.
   *
   *  Everything here is used by this capability alone; widening the shared table
   *  would make an unrelated module's failure look like ours. Resolution is lazy
   *  and tolerant for the same reason `mod()` is. */
  function extra(key, url, symbol) {
    if (!_extras.has(key)) {
      let value = null;
      try {
        const exports = ChromeUtils.importESModule(url);
        value = symbol ? exports[symbol] || null : exports;
      } catch (ex) {
        console.warn(`[tbmcp] ${key} unavailable at ${url}: ${ex.message || ex}`);
      }
      _extras.set(key, value);
    }
    return _extras.get(key);
  }

  function glodaConstants() {
    const value = extra(
      "GlodaConstants",
      "resource:///modules/gloda/GlodaConstants.sys.mjs",
      "GlodaConstants"
    );
    if (!value) {
      throw H.unsupported(
        "gloda's constants module is missing, so a query cannot name the noun it queries"
      );
    }
    return value;
  }

  function glodaIndexer() {
    return extra(
      "GlodaIndexer",
      "resource:///modules/gloda/GlodaIndexer.sys.mjs",
      "GlodaIndexer"
    );
  }

  function glodaDatastore() {
    return extra(
      "GlodaDatastore",
      "resource:///modules/gloda/GlodaDatastore.sys.mjs",
      "GlodaDatastore"
    );
  }

  function extensionAccounts() {
    return extra("ExtensionAccounts", "resource:///modules/ExtensionAccounts.sys.mjs");
  }

  /** The singleton MessageTracker that mints WebExtension message ids. */
  function messageTracker() {
    const parent = extra(
      "ExtensionParent",
      "resource://gre/modules/ExtensionParent.sys.mjs",
      "ExtensionParent"
    );
    try {
      return (parent && parent.apiManager.global.messageTracker) || null;
    } catch (ex) {
      return null;
    }
  }

  function webExtMessageId(hdr) {
    if (!hdr) {
      return null;
    }
    const tracker = messageTracker();
    if (!tracker) {
      return null;
    }
    try {
      return tracker.getId(hdr);
    } catch (ex) {
      return null;
    }
  }

  /** `<accountKey>:/<path>`, the id folder_list and mail_list speak. */
  function webExtFolderId(folder) {
    if (!folder) {
      return null;
    }
    try {
      const accounts = extensionAccounts();
      const account = needMod("MailServices").accounts.findAccountForServer(folder.server);
      if (!accounts || !accounts.folderURIToPath || !account) {
        return null;
      }
      return `${account.key}:/${accounts.folderURIToPath(account.key, folder.URI)}`;
    } catch (ex) {
      // Unified and tag folders have a different id scheme, and a gloda hit can
      // in principle live in one. The URI we return alongside is unambiguous.
      return null;
    }
  }

  /** The account keys this profile actually has. */
  function accountKeys() {
    const keys = new Set();
    try {
      for (const account of needMod("MailServices").accounts.accounts) {
        if (account && account.key) {
          keys.add(account.key);
        }
      }
    } catch (ex) {
      // An empty set only costs us the "no such account" error message.
    }
    return keys;
  }

  /** Split `account7://INBOX` into its account key and folder path.
   *
   *  webExtFolderId builds the id as `${key}:/${path}` where `path` itself
   *  starts with "/", so the separator occupies two characters and the path
   *  begins at `cut + 2`. Slicing from `cut + 1` — as this did — kept a leading
   *  slash, producing `imap://…com//INBOX`: a URI that names no folder, so the
   *  hit filter matched nothing and every folder-scoped search_global came back
   *  empty, blaming the index. */
  function splitFolderId(text) {
    const cut = text.indexOf(":/");
    if (cut < 1) {
      return null;
    }
    const key = text.slice(0, cut);
    return accountKeys().has(key) ? { key, path: text.slice(cut + 2) } : null;
  }

  /** Decide whether a gloda hit is in the folder the caller named.
   *
   *  A caller may name either form and the two are not tellable apart by
   *  punctuation — a folder id contains "://" exactly like a folder URI does.
   *  So resolve what we can and compare against both fields a hit carries,
   *  rather than converting one form into the other and trusting the round trip
   *  to be byte-exact. */
  function folderScope(ref) {
    const text = String(ref).trim();
    const keys = new Set([text]);
    const parts = splitFolderId(text);
    let resolved = Boolean(parts);
    const accounts = extensionAccounts();
    if (parts && accounts && accounts.folderPathToURI) {
      let uri = null;
      try {
        uri = accounts.folderPathToURI(parts.key, parts.path);
      } catch (ex) {
        uri = null;
      }
      if (uri) {
        keys.add(uri);
      }
    }
    if (!resolved) {
      // Not a folder id, so it has to be a URI under one of this profile's
      // servers. Checking against the real root URIs beats guessing at schemes.
      try {
        for (const account of needMod("MailServices").accounts.accounts) {
          const root = account && account.incomingServer && account.incomingServer.rootFolder;
          if (root && (text === root.URI || text.startsWith(`${root.URI}/`))) {
            resolved = true;
            break;
          }
        }
      } catch (ex) {
        // Cannot enumerate: accept it rather than refuse a call we cannot judge.
        resolved = true;
      }
    }
    if (!resolved) {
      throw H.usage(
        `${text} names no folder in this profile — pass an id from folder_list, ` +
          "or a folder URI"
      );
    }
    return (entry) => keys.has(entry.folderUri) || keys.has(entry.folderId);
  }

  /** Run a gloda query to completion, or give up loudly. */
  function collect(start, what, timeoutMs) {
    const items = [];
    const finished = new Promise((resolve, reject) => {
      const listener = {
        onItemsAdded(added) {
          for (const item of added || []) {
            items.push(item);
          }
        },
        onItemsModified() {},
        onItemsRemoved() {},
        onQueryCompleted() {
          resolve(items);
        },
      };
      try {
        start(listener);
      } catch (ex) {
        reject(ex);
      }
    });
    return H.withTimeout(finished, timeoutMs, what);
  }

  function isoDate(value) {
    if (value === null || value === undefined) {
      return null;
    }
    /* Duck-type rather than `instanceof Date`. Gloda mints its Date objects in
     * the shared system global (GlodaDatastore.sys.mjs), while this code runs in
     * the ext-*.js sandbox — a different realm with a different Date.prototype,
     * so `instanceof` is false for every one of them. That silently nulled the
     * date on every hit, which also flattened the conversation ordering that
     * byDateThenSubject is supposed to provide. */
    if (typeof value.getTime === "function") {
      const ms = value.getTime();
      return Number.isFinite(ms) ? new Date(ms).toISOString() : null;
    }
    // Gloda stores PRTime (microseconds) and normally hands back a Date, but
    // conversation bounds have been seen raw.
    if (typeof value === "number" && value > 0) {
      return new Date(value / 1000).toISOString();
    }
    return null;
  }

  function identityLabel(identity) {
    try {
      if (!identity) {
        return null;
      }
      const name = identity.contact ? identity.contact.name : null;
      return name && name !== identity.value ? `${name} <${identity.value}>` : identity.value;
    } catch (ex) {
      return null;
    }
  }

  function snippet(message) {
    let text = null;
    try {
      text = message.indexedBodyText;
    } catch (ex) {
      return null;
    }
    if (!text) {
      return null;
    }
    const flat = String(text).replace(/\s+/g, " ").trim();
    return flat.length > SNIPPET_CHARS ? `${flat.slice(0, SNIPPET_CHARS)}…` : flat;
  }

  /** The live nsIMsgDBHdr, or null for a ghost — a message gloda knows about by
   *  reference only, because it was cited by a reply we did index. */
  function folderMessage(message) {
    try {
      return message.folderMessage;
    } catch (ex) {
      return null;
    }
  }

  function hit(message, score) {
    const hdr = folderMessage(message);
    const folder = hdr ? hdr.folder : null;
    let folderName = null;
    try {
      folderName = folder
        ? folder.localizedName
        : (message.folder && message.folder.prettyName) || null;
    } catch (ex) {
      folderName = null;
    }
    return {
      id: webExtMessageId(hdr),
      glodaId: message.id,
      headerMessageId: message.headerMessageID,
      subject: message.subject || (hdr ? hdr.mime2DecodedSubject : null),
      author: hdr ? hdr.mime2DecodedAuthor : identityLabel(message.from),
      recipients: hdr ? hdr.mime2DecodedRecipients : null,
      date: isoDate(message.date),
      folderId: webExtFolderId(folder),
      folderUri: message.folderURI,
      folderName,
      conversationId: message.conversationID,
      read: hdr ? hdr.isRead : null,
      flagged: hdr ? hdr.isFlagged : null,
      score: typeof score === "number" ? score : null,
      snippet: snippet(message),
      // False means the message is referenced by the thread but not in any
      // folder here, so there is no id to read it with.
      inStore: Boolean(hdr),
    };
  }

  /** Everyone who appears anywhere in the thread, in first-seen order.
   *
   *  `involves` is gloda's own union of from/to/cc/bcc. It is an optimization
   *  attribute and can be absent on a ghost, so fall back to from/to. */
  function participantsOf(messages) {
    const seen = new Set();
    const out = [];
    const add = (identity) => {
      const label = identityLabel(identity);
      if (label && !seen.has(label)) {
        seen.add(label);
        out.push(label);
      }
    };
    for (const message of messages) {
      let involves = null;
      try {
        involves = message.involves;
      } catch (ex) {
        involves = null;
      }
      if (involves && involves.length) {
        for (const identity of involves) {
          add(identity);
        }
        continue;
      }
      try {
        add(message.from);
        for (const identity of message.to || []) {
          add(identity);
        }
      } catch (ex) {
        // A referenced-but-unindexed message carries no identities; the rest
        // of the thread still names the people involved.
      }
    }
    return out;
  }

  function byDateThenSubject(a, b) {
    const left = a.date || "";
    const right = b.date || "";
    if (left !== right) {
      return left < right ? -1 : 1;
    }
    return String(a.subject || "").localeCompare(String(b.subject || ""));
  }

  function indexEnabled() {
    return Services.prefs.getBoolPref("mailnews.database.global.indexer.enabled", false);
  }

  const NEAR = /^NEAR(\/\d+)?$/;

  /** Whether GlodaMsgSearcher.buildFulltextQuery puts this term into the SQL at
   *  all — it silently omits anything shorter, which is why a stray "-" or "2"
   *  costs nothing. */
  function isEmitted(term) {
    if (NEAR.test(term)) {
      return true;
    }
    if (term.length >= 3) {
      return true;
    }
    const cjk = (index) => term.charCodeAt(index) >= 0x2000;
    return (term.length === 1 && cjk(0)) || (term.length === 2 && cjk(0) && cjk(1));
  }

  /** Whether the term survives tokenization with something the index can match.
   *  Gloda's tokenizer splits on non-alphanumerics and drops tokens under three
   *  characters (one for CJK), so "2.0" yields nothing while "e-mail" yields
   *  "mail". */
  function isSearchable(term) {
    if (NEAR.test(term)) {
      return true;
    }
    return String(term)
      .split(/[^\p{L}\p{N}]+/u)
      .filter(Boolean)
      .some((token) => token.length >= 3 || token.charCodeAt(0) >= 0x2000);
  }

  // ------------------------------------------------------------------- search

  TBX_MODULES["gloda.search"] = async (params) => {
    const query = String(H.need(params, "query"));
    const limit = Math.min(
      Math.max(Number.isInteger(params.limit) ? params.limit : 25, 1),
      MAX_HITS
    );
    const offset = Number.isInteger(params.offset) && params.offset > 0 ? params.offset : 0;
    const inScope = params.folderId ? folderScope(params.folderId) : null;
    const Searcher = needMod("GlodaMsgSearcher");

    const matchAll = params.matchAll !== false;
    const searcher = new Searcher(null, query, matchAll);
    const terms = searcher.fulltextTerms || [];
    // The tokenizer drops terms shorter than three characters (one or two for
    // CJK), so a query made only of those silently matches everything or
    // nothing. Say so instead.
    const usable = terms.some(isSearchable);
    if (!usable) {
      throw H.usage(
        `no searchable term in ${JSON.stringify(query)} — the global index needs ` +
          "terms of at least three characters (one for CJK)"
      );
    }
    /* A term long enough to reach the query but made only of tokens the indexer
     * throws away — "2.0" splits into "2" and "0" — is a phrase that can never
     * match, and under AND it takes the whole query down with it. Pasting a
     * subject line into search_global hits this constantly, and the result was
     * an empty answer blaming the index. Name the terms instead. */
    const unmatchable = terms.filter((term) => isEmitted(term) && !isSearchable(term));

    /* Gloda has no OFFSET, and a folder filter can only be applied after the
     * fact because the searcher's SQL is fixed. Over-fetch, then slice. */
    const retrieve = Math.min(
      Math.max((offset + limit) * (inScope ? 10 : 3), 50),
      MAX_RETRIEVE
    );
    const messages = await collect(
      (listener) => {
        // What getCollection() does, minus its pref-driven retrieval limit: the
        // searcher stays the collection listener so its scoring still runs, and
        // ours nests inside it.
        searcher.listener = listener;
        searcher.query = searcher.buildFulltextQuery();
        searcher.query.limit(retrieve);
        searcher.collection = searcher.query.getCollection(searcher, null);
      },
      "gloda search",
      SEARCH_TIMEOUT_MS
    );

    // searcher.scores accumulates in the order items were handed to us.
    const scores = searcher.scores || [];
    let hits = messages.map((message, index) => hit(message, scores[index]));
    if (inScope) {
      hits = hits.filter(inScope);
    }
    hits.sort((a, b) => (b.score || 0) - (a.score || 0) || byDateThenSubject(b, a));

    const enabled = indexEnabled();
    const result = {
      query,
      hits: hits.slice(offset, offset + limit),
      matched: hits.length,
      retrieved: messages.length,
      // The ranking only ordered what we retrieved; a deeper page may reorder.
      truncated: messages.length >= retrieve,
      indexEnabled: enabled,
    };
    if (unmatchable.length) {
      result.unmatchableTerms = unmatchable;
    }
    if (!result.hits.length) {
      if (!enabled) {
        result.note =
          "Thunderbird's global index is disabled, so this search can never match. " +
          "Enable it in Settings > General > Indexing, or use mail_search instead.";
      } else if (unmatchable.length && matchAll) {
        result.note =
          `The index cannot match ${unmatchable.map((t) => JSON.stringify(t)).join(", ")} — ` +
          "it tokenizes into pieces shorter than three characters, and every term has to " +
          "match, so this query can never return anything. Drop those words and search " +
          "again, or use mail_search with a subject filter for an exact substring.";
      } else if (unmatchable.length) {
        result.note =
          `The index cannot match ${unmatchable.map((t) => JSON.stringify(t)).join(", ")}; ` +
          "the remaining terms matched nothing either. Try mail_search with a subject " +
          "or author filter.";
      } else {
        result.note =
          "Nothing in the global index matched. Only indexed messages are searchable — " +
          "check x.gloda.stats, or fall back to mail_search with subject/author filters.";
      }
    }
    return result;
  };

  // ------------------------------------------------------------- conversation

  function trackedHeader(messageId) {
    const tracker = messageTracker();
    if (!tracker) {
      throw H.unsupported(
        "this build does not expose the WebExtension message tracker; " +
          "pass headerMessageId instead of messageId"
      );
    }
    const hdr = tracker.getMessage(messageId);
    if (!hdr) {
      throw H.usage(
        `message ${messageId} is no longer known — ids expire when Thunderbird ` +
          "restarts, so re-run the search that produced it"
      );
    }
    return hdr;
  }

  /** The gloda record a conversation is asked about, from either identifier. */
  async function seedMessage(params) {
    const gloda = needMod("Gloda");
    if (params.messageId !== undefined && params.messageId !== null) {
      const hdr = trackedHeader(params.messageId);
      const found = await collect(
        (listener) => gloda.getMessageCollectionForHeader(hdr, listener, null),
        "gloda message lookup",
        LOOKUP_TIMEOUT_MS
      );
      if (!found.length) {
        throw H.usage(
          `message ${params.messageId} is not in the global index yet, so its ` +
            "conversation is unknown — check x.gloda.stats"
        );
      }
      return found[0];
    }
    const headerMessageId = String(H.need(params, "headerMessageId")).replace(/^<|>$/g, "");
    const found = await collect(
      (listener) =>
        gloda
          .newQuery(glodaConstants().NOUN_MESSAGE)
          .headerMessageID(headerMessageId)
          .getCollection(listener, null),
      "gloda message lookup",
      LOOKUP_TIMEOUT_MS
    );
    if (!found.length) {
      throw H.usage(
        `no indexed message has Message-ID ${headerMessageId} — pass a messageId ` +
          "from mail_search instead"
      );
    }
    return found[0];
  }

  TBX_MODULES["gloda.conversation"] = async (params) => {
    const limit = Math.min(
      Math.max(Number.isInteger(params.limit) ? params.limit : 100, 1),
      500
    );
    const seed = await seedMessage(params);
    const conversation = seed.conversation;
    if (!conversation) {
      throw H.unsupported(
        "gloda has no conversation record for that message yet; it may still be indexing"
      );
    }
    const messages = await collect(
      (listener) => conversation.getMessagesCollection(listener, null),
      "gloda conversation",
      LOOKUP_TIMEOUT_MS
    );
    // Date order is the whole point: it is what makes a thread readable as a
    // discussion rather than a pile of hits.
    const ordered = messages.map((message) => hit(message, null)).sort(byDateThenSubject);
    return {
      conversationId: conversation.id,
      subject: conversation.subject,
      participants: participantsOf(messages),
      oldestDate: isoDate(conversation.oldestMessageDate),
      newestDate: isoDate(conversation.newestMessageDate),
      messages: ordered.slice(0, limit),
      total: ordered.length,
      seed: {
        glodaId: seed.id,
        headerMessageId: seed.headerMessageID,
      },
    };
  };

  // -------------------------------------------------------------------- stats

  /** COUNT over the index. Async on purpose: this table runs to hundreds of
   *  thousands of rows on a real profile and a sync statement stalls the UI. */
  function countIndexedMessages() {
    const datastore = glodaDatastore();
    const connection = datastore && datastore.asyncConnection;
    if (!connection) {
      return Promise.resolve(null);
    }
    let statement;
    try {
      statement = connection.createAsyncStatement(
        "SELECT COUNT(*) FROM messages WHERE deleted = 0"
      );
    } catch (ex) {
      return Promise.resolve(null);
    }
    const counted = new Promise((resolve, reject) => {
      let value = null;
      statement.executeAsync({
        handleResult(resultSet) {
          let row;
          while ((row = resultSet.getNextRow())) {
            value = row.getInt64(0);
          }
        },
        handleError(error) {
          reject(new Error(String(error.message || error.result)));
        },
        handleCompletion() {
          resolve(value);
        },
      });
      statement.finalize();
    });
    return H.withTimeout(counted, COUNT_TIMEOUT_MS, "counting indexed messages").catch(
      () => null
    );
  }

  TBX_MODULES["gloda.stats"] = async () => {
    const enabled = indexEnabled();
    const indexer = glodaIndexer();
    let job = null;
    try {
      job = indexer ? indexer._curIndexingJob : null;
    } catch (ex) {
      job = null;
    }
    const stats = {
      enabled,
      available: Boolean(mod("GlodaMsgSearcher")),
      indexing: indexer ? Boolean(indexer.indexing) : null,
      indexingDesired: indexer ? Boolean(indexer.indexingDesired) : null,
      queuedJobs: indexer && Array.isArray(indexer._indexQueue) ? indexer._indexQueue.length : null,
      currentJob: job
        ? { type: job.jobType, done: job.offset, total: job.goal }
        : null,
      indexedMessages: await countIndexedMessages(),
      searchRetrievalLimit: Services.prefs.getIntPref(
        "mailnews.database.global.search.msg.limit",
        0
      ),
    };
    if (!enabled) {
      stats.note =
        "Indexing is off, so the index will never grow and global search cannot match. " +
        "Settings > General > Indexing turns it on; the first pass takes a while.";
    } else if (stats.indexing) {
      stats.note =
        "Indexing is still running, so recent or newly added mail may not be searchable yet.";
    } else if (stats.indexedMessages === 0) {
      stats.note =
        "The index is empty even though indexing is enabled — Thunderbird has not " +
        "swept the folders yet. Leave it running for a few minutes.";
    }
    return stats;
  };
}

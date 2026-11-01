/* Follow-up candidates are a bounded read, not a promise that every message
 * needs a reply. No message content is sent outside Thunderbird. */
{
  const DAY = 86400000;
  const normalizeId = (value) => String(value || "").trim().replace(/^<|>$/g, "");
  function addresses(values) {
    const found = new Set();
    for (const value of Array.isArray(values) ? values : [values]) {
      for (const item of String(value || "").match(/[^\s<>(),;:"\[\]]+@[^\s<>(),;:"\[\]]+/g) || []) {
        found.add(item.toLowerCase());
      }
    }
    return [...found];
  }
  function header(headers, name) {
    const value = Object.entries(headers || {}).find(([key]) => key.toLowerCase() === name)?.[1];
    return (Array.isArray(value) ? value : [value]).filter(Boolean).join(" ").trim();
  }
  function automated(headers) {
    const submitted = header(headers, "auto-submitted").toLowerCase();
    return Boolean(submitted && submitted !== "no") || /^(bulk|list|junk)\b/i.test(header(headers, "precedence"));
  }
  function unsentHeaders(headers) {
    return header(headers, "x-unsent") === "1" || Boolean(header(headers, "x-mozilla-draft-info"));
  }
  function references(headers) {
    const text = `${header(headers, "in-reply-to")} ${header(headers, "references")}`;
    const bracketed = [...text.matchAll(/<([^<>]+)>/g)].map((match) => match[1]);
    return new Set(bracketed.length ? bracketed : text.split(/\s+/).filter(Boolean).map(normalizeId));
  }
  function timestamp(row) {
    const value = new Date(row.date).getTime();
    return Number.isFinite(value) ? value : NaN;
  }
  tbxRegistry.define("messages.followups", async (params, ctx = {}) => {
    const limit = tbxUtil.limit(params.limit, 25, 100);
    if (params.cursor) {
      if (params.accountId || params.lookbackDays || params.waitingDays) throw tbxError.usage("Pass only cursor and limit to continue follow-ups.");
      return tbxSnapshots.page("followups", params.cursor, limit);
    }
    const lookback = params.lookbackDays ?? 30, waiting = params.waitingDays ?? 7;
    if (!Number.isInteger(lookback) || lookback < 1 || lookback > 180 || !Number.isInteger(waiting) || waiting < 1 || waiting > lookback) {
      throw tbxError.usage("lookbackDays must be 1-180 and waitingDays must be 1-lookbackDays.");
    }
    if (!browser.tbx) throw tbxError.unsupported("Follow-ups require the global-index module.");
    const asOf = Date.now(), deadline = asOf + 60000, cutoff = asOf - waiting * DAY;
    const reasons = new Set(), candidates = [], seen = new Set(), knownSeeds = new Set(), headerCache = new Map(), folderCache = new Map();
    const counters = { scannedOutgoing: 0, inspectedConversations: 0, replied: 0, excluded: 0, unknown: 0 };
    async function bounded(promise) {
      if (ctx.cancelled) throw new Error("cancelled");
      const left = deadline - Date.now();
      if (left <= 0) throw new Error("time_budget");
      let timer;
      try {
        return await Promise.race([promise, new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error("time_budget")), left);
        })]);
      } finally { clearTimeout(timer); }
    }
    async function privileged(method, args) {
      try { return await bounded(browser.tbx.invoke(method, args)); }
      catch (ex) { throw tbxError.fromWire(ex) || ex; }
    }
    async function headers(row) {
      if (!Number.isInteger(row.id)) throw new Error("headers_unavailable");
      if (!headerCache.has(row.id)) headerCache.set(row.id, bounded(
        browser.messages.getHeaders ? browser.messages.getHeaders(row.id) : browser.messages.getFull(row.id, { decrypt: false })
      ).then((value) => value.headers || value));
      return headerCache.get(row.id);
    }
    async function unsentFolder(row) {
      const id = row.folderId || row.folder?.id;
      if (!id) throw new Error("folder_unavailable");
      if (!folderCache.has(id)) folderCache.set(id, bounded(browser.folders.get(id)));
      const folder = await folderCache.get(id);
      const kinds = [...(folder.specialUse || []), folder.type].filter(Boolean).map((value) => String(value).toLowerCase());
      return kinds.some((value) => ["drafts", "outbox", "templates"].includes(value));
    }
    const accounts = await bounded(browser.accounts.list(false));
    if (params.accountId && !accounts.some((account) => account.id === params.accountId)) throw tbxError.usage("accountId is not in this profile.");
    const own = new Set(accounts.flatMap((account) => (account.identities || []).flatMap((identity) => addresses(identity.email))));
    const isOwn = (row) => addresses(row.author).some((email) => own.has(email));
    const meta = {
      asOf: new Date(asOf).toISOString(), engine: "thread_headers", coverage: "indexed_only",
      requestedScope: { ...(params.accountId ? { accountId: params.accountId } : {}), lookbackDays: lookback, waitingDays: waiting },
      note: "Possible follow-ups, not proof that a reply is required. Only accessible indexed thread evidence is considered. Mailing-list exclusion uses the outgoing and subsequent reply headers inspected in this bounded scan.",
    };
    const stats = await privileged("gloda.stats", {});
    if (!stats.enabled || !stats.available) {
      return tbxSnapshots.begin("followups", [], { ...meta, partialReasons: ["index_unavailable"], partial: true, ...counters }, limit);
    }
    if (stats.indexing) reasons.add("indexing_in_progress");
    const query = { fromMe: true, fromDate: new Date(asOf - lookback * DAY), toDate: new Date(asOf), messagesPerPage: 100 };
    if (params.accountId) query.accountId = params.accountId;
    const outgoing = [];
    let listId = null;
    try {
      let page = await bounded(browser.messages.query(query));
      if (typeof page === "string") page = await bounded(browser.messages.continueList(page));
      for (;;) {
        if (!page || !Array.isArray(page.messages)) throw new Error("invalid_message_list");
        outgoing.push(...page.messages);
        listId = page.id || null;
        if (outgoing.length > 500 || (outgoing.length >= 500 && listId)) { reasons.add("outgoing_limit"); break; }
        if (!listId) break;
        page = await bounded(browser.messages.continueList(listId));
      }
    } catch (ex) { reasons.add(String(ex.message || ex)); }
    finally { if (listId) { try { await bounded(browser.messages.abortList(listId)); } catch (_) { /* expired */ } } }
    const seeds = outgoing.slice(0, 500).sort((a, b) => timestamp(b) - timestamp(a));
    counters.scannedOutgoing = seeds.length;
    // A small pool bounds both XPCOM queries and MIME header reads.
    await tbxUtil.mapLimited(seeds, 4, async (seed) => {
      if (Date.now() >= deadline || ctx.cancelled) { reasons.add(ctx.cancelled ? "cancelled" : "time_budget"); return; }
      if (!isOwn(seed) || knownSeeds.has(seed.id)) return;
      if (seen.size >= 100) { reasons.add("conversation_limit"); return; }
      try {
        if (await unsentFolder(seed)) { counters.excluded++; return; }
        const seedHeaders = await headers(seed);
        if (unsentHeaders(seedHeaders) || automated(seedHeaders) || header(seedHeaders, "list-id")) { counters.excluded++; return; }
      } catch (ex) { counters.unknown++; reasons.add("source_metadata_unavailable"); return; }
      let thread;
      try { thread = await privileged("gloda.conversation", { messageId: seed.id, limit: 1000, snapshot: true }); }
      catch (ex) { counters.unknown++; reasons.add("conversation_unavailable"); return; }
      const key = String(thread.conversationId ?? normalizeId(seed.headerMessageId));
      if (!key || seen.has(key)) return;
      if (seen.size >= 100) { reasons.add("conversation_limit"); return; }
      seen.add(key); counters.inspectedConversations++;
      const rows = (thread.messages || []).filter((row) => timestamp(row) <= asOf).sort((a, b) => timestamp(a) - timestamp(b));
      for (const row of rows) if (Number.isInteger(row.id)) knownSeeds.add(row.id);
      if (thread.total > rows.length || !rows.length) { counters.unknown++; reasons.add("incomplete_conversation"); return; }
      try {
        const sent = [];
        for (const row of rows.filter(isOwn)) {
          if (await unsentFolder(row)) continue;
          const savedHeaders = await headers(row);
          if (header(savedHeaders, "list-id")) { counters.excluded++; return; }
          if (!unsentHeaders(savedHeaders) && !automated(savedHeaders)) sent.push(row);
        }
        const latest = sent[sent.length - 1];
        if (!latest || timestamp(latest) > cutoff || timestamp(latest) < asOf - lookback * DAY) return;
        const lastHeaders = await headers(latest);
        if (automated(lastHeaders) || header(lastHeaders, "list-id")) { counters.excluded++; return; }
        const peers = addresses([header(lastHeaders, "to"), header(lastHeaders, "cc"), header(lastHeaders, "bcc"), latest.recipients || []].flat()).filter((email) => !own.has(email));
        if (!peers.length) { counters.excluded++; return; }
        const outgoingIds = new Set(sent.map((row) => normalizeId(row.headerMessageId)).filter(Boolean));
        let ambiguous = false;
        for (const row of rows.filter((item) => !isOwn(item) && timestamp(item) > timestamp(latest))) {
          const incomingHeaders = await headers(row);
          if (header(incomingHeaders, "list-id")) { counters.excluded++; return; }
          if (automated(incomingHeaders)) continue;
          if ([...references(incomingHeaders)].some((id) => outgoingIds.has(id))) { counters.replied++; return; }
          ambiguous = true;
        }
        if (ambiguous) { counters.unknown++; reasons.add("unlinked_reply"); return; }
        candidates.push({
          conversationId: thread.conversationId, subject: latest.subject || thread.subject,
          messageId: latest.id, headerMessageId: latest.headerMessageId, folderId: latest.folderId,
          recipients: peers, sentAt: latest.date, waitingDays: Math.floor((asOf - timestamp(latest)) / DAY),
          reason: "No later human reply linked to an outgoing message was found in the accessible indexed conversation.",
          evidence: { kind: "thread_headers", inspectedMessages: rows.length, lastOutgoingMessageId: latest.id, headerMessageId: latest.headerMessageId },
        });
      } catch (ex) { counters.unknown++; reasons.add("headers_unavailable"); }
      if (ctx.progress) ctx.progress(counters.inspectedConversations, Math.min(seeds.length, 100), "checking follow-up candidates");
    });
    candidates.sort((a, b) => a.sentAt.localeCompare(b.sentAt) || String(a.conversationId).localeCompare(String(b.conversationId)));
    return tbxSnapshots.begin("followups", candidates, { ...meta, ...counters, partialReasons: [...reasons], partial: reasons.size > 0 }, limit);
  });
}

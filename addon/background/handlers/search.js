/* Snapshot pagination wraps privileged search without changing the legacy RPCs. */
{
  async function invoke(method, params) {
    if (!browser.tbx) throw tbxError.unsupported("Search needs the privileged add-on module.");
    try { return await browser.tbx.invoke(method, params); }
    catch (ex) { throw tbxError.fromWire(ex) || ex; }
  }
  tbxRegistry.define("search.global", async (params) => {
    const limit = tbxUtil.limit(params.limit, 25, 200);
    if (params.cursor) {
      if (params.query || params.folderId || params.offset) throw tbxError.usage("Pass only cursor and limit when continuing a search.");
      return tbxSnapshots.page("search.global", params.cursor, limit);
    }
    const query = tbxUtil.need(params, "query", "string").trim();
    if (!query) throw tbxError.usage("query must not be blank");
    const raw = await invoke("gloda.search", { query, folderId: params.folderId, limit: 1000, snapshot: true });
    const partialReasons = [];
    if (raw.truncated) partialReasons.push("candidate_limit");
    if (raw.indexEnabled === false) partialReasons.push("index_disabled");
    if (raw.unmatchableTerms?.length) partialReasons.push("unmatchable_terms");
    const meta = {
      query, engine: "global_index", coverage: "indexed_only",
      requestedScope: { ...(params.folderId ? { folderId: params.folderId } : {}) },
      partialReasons, partial: partialReasons.length > 0, matched: raw.matched, retrieved: raw.retrieved,
      indexEnabled: raw.indexEnabled, truncated: Boolean(raw.truncated),
      ...(partialReasons.length ? {} : { totalAvailable: raw.matched }),
      ...(raw.unmatchableTerms?.length ? { unmatchableTerms: raw.unmatchableTerms } : {}),
      ...(raw.note ? { note: raw.note } : {}),
    };
    if (raw.indexEnabled === false || raw.unmatchableTerms?.length) {
      meta.alternativeSearch = {
        tool: "mail_search", parameters: { subject: query, ...(params.folderId ? { folder_id: params.folderId } : {}) },
        semanticsChanged: true,
        note: "Literal subject substring only, not the original indexed search. Choose an account/folder and date range before running it.",
      };
    }
    return tbxSnapshots.begin("search.global", raw.hits || [], meta, limit, Math.max(0, params.offset || 0));
  });
  tbxRegistry.define("search.conversation", async (params) => {
    const limit = tbxUtil.limit(params.limit, 100, 500);
    if (params.cursor) {
      if (params.messageId != null || params.headerMessageId) throw tbxError.usage("Pass only cursor and limit when continuing a conversation.");
      return tbxSnapshots.page("search.conversation", params.cursor, limit);
    }
    const raw = await invoke("gloda.conversation", { ...params, limit: 1000, snapshot: true });
    const truncated = raw.total > (raw.messages || []).length;
    const meta = { engine: "global_index", coverage: "indexed_only", truncated,
      partialReasons: truncated ? ["conversation_limit"] : [], totalAvailable: raw.total };
    for (const key of ["conversationId", "subject", "participants"]) if (raw[key] != null) meta[key] = raw[key];
    return tbxSnapshots.begin("search.conversation", raw.messages || [], meta, limit);
  });
}

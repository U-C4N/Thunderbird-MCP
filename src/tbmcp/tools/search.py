"""Global search and saved searches.

Two different search engines live behind this toolset, and choosing wrongly is the
main way an agent wastes turns here:

- `search_global` asks Thunderbird's **global index** (Gloda). It is ranked, spans
  every indexed folder and account at once, and understands conversations. It only
  knows about messages the indexer has already processed.
- `mail_search` (the `mail` toolset) filters folder databases directly. It is exact,
  works on anything Thunderbird can see whether indexed or not, and takes structured
  predicates.

Rule of thumb: a vague question about content goes to `search_global`; a precise
question about a known folder, flag, sender or date range goes to `mail_search`.
"""

from typing import Any

from ..errors import TbmcpError, UsageError
from ..safety import (
    DESTRUCTIVE,
    IDEMPOTENT_WRITE,
    MUTATING,
    Gate,
    guard_write,
    large_output,
    require,
)
from ..server import Registrar
from ._common import call, changed, clamp, dry_run, page

#: The search-term vocabulary the privileged half accepts. Repeated here so the
#: error message can list it without a round trip.
ATTRIBUTES = (
    "subject",
    "from",
    "to",
    "cc",
    "toOrCc",
    "allAddresses",
    "body",
    "anyText",
    "date",
    "ageInDays",
    "size",
    "priority",
    "status",
    "hasAttachment",
    "tag",
    "junkStatus",
    "junkScore",
    "messageId",
    "otherHeader",
    "folder",
)
OPERATORS = (
    "contains",
    "doesntContain",
    "is",
    "isnt",
    "isEmpty",
    "isntEmpty",
    "beginsWith",
    "endsWith",
    "isBefore",
    "isAfter",
    "isHigherThan",
    "isLowerThan",
    "isGreaterThan",
    "isLessThan",
)


def _check_terms(terms: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Validate terms here so a typo costs no round trip to Thunderbird."""
    if not terms:
        raise UsageError(
            "terms is required — a saved search with no conditions would match "
            "everything. Each term is {attribute, operator, value, matchAll?}."
        )
    for index, term in enumerate(terms):
        if not isinstance(term, dict):
            raise UsageError(f"terms[{index}] must be an object, not {type(term).__name__}.")
        attribute = term.get("attribute")
        operator = term.get("operator")
        if attribute not in ATTRIBUTES:
            raise UsageError(
                f"terms[{index}].attribute must be one of: {', '.join(ATTRIBUTES)}; "
                f"got {attribute!r}."
            )
        if operator not in OPERATORS:
            raise UsageError(
                f"terms[{index}].operator must be one of: {', '.join(OPERATORS)}; got {operator!r}."
            )
        if operator not in ("isEmpty", "isntEmpty") and term.get("value") in (None, ""):
            raise UsageError(f"terms[{index}].value is required for operator {operator!r}.")
    return terms


async def _indexed_call(method: str, legacy: str, params: dict[str, Any]) -> tuple[dict, bool]:
    """Keep first-page searches usable with 1.3 without pretending cursors exist."""
    try:
        return await call(method, params, timeout=120.0), False
    except TbmcpError as exc:
        if exc.code != "UNSUPPORTED_CAPABILITY" or params.get("cursor"):
            raise
    return await call(legacy, params, timeout=120.0), True


def _legacy_global(result: dict, params: dict) -> dict:
    partial = []
    if result.get("truncated"):
        partial.append("candidate_limit")
    if result.get("indexEnabled") is False:
        partial.append("index_disabled")
    if result.get("unmatchableTerms"):
        partial.append("unmatchable_terms")
    payload = page(
        result.get("hits") or result.get("messages") or [],
        total=None if partial else result.get("matched"),
        query=params["query"],
        engine="global_index",
        coverage="indexed_only",
        requestedScope={"folderId": params["folderId"]} if params.get("folderId") else {},
        snapshotSupported=False,
        pagination="offset",
        offset=params.get("offset", 0),
        partial=bool(partial),
        partialReasons=partial,
        truncated=bool(result.get("truncated")),
        compatibilityNote="The installed add-on does not support stable snapshots; offset pages may change as mail changes.",
        **{
            key: result[key]
            for key in ("matched", "retrieved", "indexEnabled", "unmatchableTerms", "note")
            if result.get(key) is not None
        },
    )
    if "index_disabled" in partial or "unmatchable_terms" in partial:
        payload["alternativeSearch"] = {
            "tool": "mail_search",
            "parameters": {
                "subject": params["query"],
                **({"folder_id": params["folderId"]} if params.get("folderId") else {}),
            },
            "semanticsChanged": True,
            "note": "Literal subject substring only; choose a folder/account and date range before running it.",
        }
    return payload


def _legacy_conversation(result: dict) -> dict:
    items = result.get("messages") or []
    truncated = isinstance(result.get("total"), int) and result["total"] > len(items)
    return page(
        items,
        total=result.get("total"),
        engine="global_index",
        coverage="indexed_only",
        snapshotSupported=False,
        pagination="limit",
        truncated=truncated,
        partialReasons=["conversation_limit"] if truncated else [],
        compatibilityNote="The installed add-on returns a live conversation prefix, without a snapshot cursor; increase limit up to 500 or update the add-on.",
        **{
            key: result[key]
            for key in ("conversationId", "subject", "participants")
            if result.get(key) is not None
        },
    )


def register(reg: Registrar) -> None:
    # ------------------------------------------------------------- global index

    @reg.read_tool(title="Search everything", meta=large_output())
    async def search_global(
        query: str | None = None,
        limit: int = 25,
        offset: int = 0,
        folder_id: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Ranked search of indexed mail, with stable snapshot pagination.

        Start with `query`, optionally scoped to a folder. Continue with only
        `cursor=nextCursor` and `limit`; a snapshot expires after five minutes or
        a Thunderbird restart. `offset` remains for older callers. Coverage is
        indexed mail only, never a claim that the whole mailbox was searched.
        `partialReasons` explains retrieval limits or unavailable index terms.
        `alternativeSearch` suggests a literal subject search without running it
        or silently changing the original query's meaning.
        Older add-ons retain live offset pages with `snapshotSupported=false`.
        """
        size = clamp(limit, default=25, minimum=1, maximum=200, field="limit")
        if cursor:
            if query is not None or folder_id is not None or offset:
                raise UsageError("Pass only cursor and limit to continue a search.")
            params: dict[str, Any] = {"cursor": cursor, "limit": size}
        else:
            if not query or not query.strip():
                raise UsageError("query is required — pass the words to search for.")
            if offset < 0:
                raise UsageError("offset cannot be negative.")
            params = {"query": query, "limit": size, "offset": offset}
            if folder_id is not None:
                params["folderId"] = folder_id
        result, legacy = await _indexed_call("search.global", "x.gloda.search", params)
        return _legacy_global(result, params) if legacy else result

    @reg.read_tool(title="Read a whole conversation", meta=large_output())
    async def search_conversation(
        message_id: int | None = None,
        header_message_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Read a conversation oldest first, across indexed folders and accounts.

        Start with one message identifier. Continue with only cursor=nextCursor
        and limit. A snapshot holds at most 1,000 messages and expires after five
        minutes; a truncated conversation is explicitly reported.
        """
        params: dict[str, Any] = {
            "limit": clamp(limit, default=100, minimum=1, maximum=500, field="limit")
        }
        if cursor:
            if message_id is not None or header_message_id is not None:
                raise UsageError("Pass only cursor and limit to continue a conversation.")
            params["cursor"] = cursor
        elif message_id is not None:
            if header_message_id is not None:
                raise UsageError("Pass message_id or header_message_id, not both.")
            params["messageId"] = message_id
        elif header_message_id:
            params["headerMessageId"] = header_message_id
        else:
            raise UsageError("Pass message_id or header_message_id.")
        result, legacy = await _indexed_call("search.conversation", "x.gloda.conversation", params)
        return _legacy_conversation(result) if legacy else result

    @reg.read_tool(title="Global index status")
    async def search_index_status() -> dict[str, Any]:
        """Whether Thunderbird's global index is enabled, and how far along it is.

        Call this to explain an empty `search_global` result. When indexing is off,
        `mail_search` with `subject`/`author`/`body` filters still works.
        """
        result = await call("x.gloda.stats", timeout=30.0)
        return result or {}

    # ------------------------------------------------------------ saved searches

    @reg.read_tool(title="List saved searches")
    async def search_saved_list() -> dict[str, Any]:
        """List the saved searches (virtual folders) and what each one matches."""
        result = await call("x.vfolders.list", timeout=45.0)
        return page(
            result.get("savedSearches") or [],
            hint="Pass a saved search's name or uri to the other search_saved_* tools.",
        )

    @reg.write_tool(title="Create a saved search", annotations=MUTATING)
    async def search_saved_create(
        name: str,
        search_folder_ids: list[str],
        terms: list[dict[str, Any]],
        match_all: bool = True,
        online_search: bool = False,
        parent_folder_id: str | None = None,
        confirm: bool = False,
        consent: Gate("create a saved search in Thunderbird") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Create a saved search that appears in the folder pane.

        `search_folder_ids` are folders to look in (from `folder_list`). Each term is
        `{"attribute": "subject", "operator": "contains", "value": "invoice"}`;
        attributes and operators are listed in the error message if you get one
        wrong. `match_all=false` makes the terms OR together. `online_search` asks
        the IMAP server to run the search instead of using the local database.

        Nothing is copied or moved — a saved search is a stored query.
        """
        guard_write("create saved searches")
        if not search_folder_ids:
            raise UsageError(
                "search_folder_ids is required — name at least one folder to search "
                "(folder_list gives the ids)."
            )
        checked = _check_terms(terms)
        for term in checked:
            term.setdefault("matchAll", match_all)
        params = {
            "name": name,
            "searchFolderIds": search_folder_ids,
            "terms": checked,
            "onlineSearch": online_search,
            "parentFolderId": parent_folder_id,
        }
        if dry_run_only:
            return dry_run("x.vfolders.create", params)
        require(consent, "create this saved search")
        result = await call("x.vfolders.create", params, timeout=60.0)
        return changed(
            f"saved search {name}",
            before=None,
            after=result.get("savedSearch"),
        )

    @reg.write_tool(title="Change a saved search", annotations=IDEMPOTENT_WRITE)
    async def search_saved_update(
        saved_search: str,
        search_folder_ids: list[str] | None = None,
        terms: list[dict[str, Any]] | None = None,
        online_search: bool | None = None,
        confirm: bool = False,
        consent: Gate("change a saved search") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Redefine an existing saved search, by name or uri.

        Only what you pass is replaced; `terms` replaces the whole condition list
        rather than merging, because a partial merge has no sensible meaning for a
        boolean query.
        """
        guard_write("change saved searches")
        params: dict[str, Any] = {"savedSearch": saved_search}
        if search_folder_ids is not None:
            if not search_folder_ids:
                raise UsageError("search_folder_ids cannot be empty; omit it to leave it alone.")
            params["searchFolderIds"] = search_folder_ids
        if terms is not None:
            params["terms"] = _check_terms(terms)
        if online_search is not None:
            params["onlineSearch"] = online_search
        if len(params) == 1:
            raise UsageError("Nothing to change — pass search_folder_ids, terms or online_search.")
        if dry_run_only:
            return dry_run("x.vfolders.update", params)
        require(consent, "change this saved search")
        result = await call("x.vfolders.update", params, timeout=60.0)
        return changed(
            f"saved search {saved_search}",
            before=result.get("previous"),
            after=result.get("current"),
        )

    @reg.write_tool(title="Delete a saved search", annotations=DESTRUCTIVE)
    async def search_saved_delete(
        saved_search: str,
        confirm: bool = False,
        consent: Gate("delete a saved search") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Remove a saved search. The messages it listed are not touched.

        A saved search only stores a query, so deleting one loses the query and
        nothing else — but the query itself is not recoverable, hence the prompt.
        """
        guard_write("delete saved searches")
        require(consent, "delete this saved search")
        result = await call("x.vfolders.delete", {"savedSearch": saved_search}, timeout=60.0)
        return changed(
            f"saved search {saved_search}",
            before=result.get("previous"),
            after=None,
            messagesAffected=result.get("messagesAffected", 0),
        )

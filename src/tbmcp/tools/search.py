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

from ..errors import UsageError
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


def register(reg: Registrar) -> None:
    # ------------------------------------------------------------- global index

    @reg.read_tool(title="Search everything", meta=large_output())
    async def search_global(
        query: str,
        limit: int = 25,
        offset: int = 0,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        """Ranked full-corpus search across every indexed folder and account.

        Best for open questions — "what did we agree about the shipment", "anything
        from the accountant about VAT". Results carry a relevance score and a
        conversation id you can pass to `search_conversation`. For precise filters
        (one folder, unread only, a date range) use `mail_search` instead.

        If this returns nothing unexpectedly, call `search_index_status`: the global
        indexer can be disabled or still catching up.
        """
        if not query or not query.strip():
            raise UsageError("query is required — pass the words to search for.")
        result = await call(
            "x.gloda.search",
            {
                "query": query,
                "limit": clamp(limit, default=25, minimum=1, maximum=200, field="limit"),
                "offset": max(0, offset),
                "folderId": folder_id,
            },
            timeout=120.0,
        )
        hits = result.get("hits") or result.get("messages") or []
        # `matched` is what the privileged half reports; there has never been a
        # `totalMatched`, so reading that key silently dropped the count from
        # every result. `truncated` means the ranking only ordered the slice we
        # retrieved, so a deeper page may reorder — worth surfacing.
        return page(
            hits,
            total=result.get("matched"),
            truncated=bool(result.get("truncated")),
            retrieved=result.get("retrieved"),
            indexEnabled=result.get("indexEnabled"),
            note=result.get("note"),
            query=query,
        )

    @reg.read_tool(title="Read a whole conversation", meta=large_output())
    async def search_conversation(
        message_id: int | None = None,
        header_message_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Every message in one thread, oldest first, across folders and accounts.

        Give either a `message_id` from `mail_search` or an RFC `header_message_id`.
        This is how you reconstruct a discussion that spans Inbox, Sent and an
        archive folder without three separate searches.
        """
        if message_id is None and not header_message_id:
            raise UsageError("Pass message_id or header_message_id.")
        params: dict[str, Any] = {
            "limit": clamp(limit, default=100, minimum=1, maximum=500, field="limit")
        }
        if message_id is not None:
            params["messageId"] = message_id
        else:
            params["headerMessageId"] = header_message_id
        result = await call("x.gloda.conversation", params, timeout=120.0)
        return page(
            result.get("messages") or [],
            total=result.get("total"),
            conversationId=result.get("conversationId"),
            subject=result.get("subject"),
            participants=result.get("participants"),
        )

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

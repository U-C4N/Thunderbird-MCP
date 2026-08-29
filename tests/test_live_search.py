"""The search tools, against a real Thunderbird.

The mocked suite cannot reach the add-on, where the search logic actually lives.
Every assertion here failed against the shipped 1.2.0 and passes now; each one
marks a bug that a green mocked run did not notice.

Skips itself when no Thunderbird is attached — see the `live_bridge` fixture.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.live]


# --------------------------------------------------------------- the tools work


async def test_substring_search_finds_mail(call, corpus) -> None:
    """`returnMessageListId` changes messages.query's return type to a bare string,
    and walking that string answered `{messages: [], cursor: null}` for every
    query, with every filter, indexed or not."""
    got = await call("mail_search", {"subject": corpus["term"], "limit": 5})
    assert got["count"] > 0, f"nothing matched {corpus['term']!r}, which came from real subjects"


async def test_substring_search_honours_a_folder(call, corpus) -> None:
    got = await call(
        "mail_search",
        {"subject": corpus["term"], "folder_id": corpus["folder_id"], "limit": 5},
    )
    assert got["count"] > 0
    assert {m["folderId"] for m in got["items"]} == {corpus["folder_id"]}


async def test_search_reports_the_scope_it_covered(call, corpus) -> None:
    """An empty result is only interpretable next to what was actually searched."""
    got = await call(
        "mail_search",
        {"subject": corpus["term"], "folder_id": corpus["folder_id"], "limit": 5},
    )
    scope = got["searchedFolders"]
    assert scope["scope"] == "folders"
    assert corpus["folder_id"] in scope["folderIds"]


async def test_global_search_returns_ranked_dated_hits(call, corpus) -> None:
    """gloda.search threw `setTimeout is not defined` — the ext sandbox has no
    timers — and every hit that did come back had `date: null`, because gloda's
    Date objects fail `instanceof` across the sandbox boundary."""
    got = await call("search_global", {"query": corpus["term"], "limit": 5})
    if not got["count"]:
        pytest.skip(f"{corpus['term']!r} is not in the global index on this profile")
    assert all(h["score"] is not None for h in got["items"]), "ranking was lost"
    assert all(h["date"] for h in got["items"]), "dates were dropped crossing the sandbox"
    assert all(h["conversationId"] for h in got["items"])


async def test_global_search_honours_a_folder(call, corpus) -> None:
    """A folder id is `<key>:/<path>` and its path starts with "/", so it contains
    "://" exactly like a URI does. Treating it as one — and splitting it one
    character short — scoped every search to a folder that does not exist."""
    got = await call(
        "search_global",
        {"query": corpus["term"], "limit": 20, "folder_id": corpus["folder_id"]},
    )
    if not got["count"]:
        pytest.skip("nothing indexed in that folder for this term")
    assert {h["folderId"] for h in got["items"]} == {corpus["folder_id"]}


async def test_a_thread_rebuilds_oldest_first(call, corpus) -> None:
    """With every date null the conversation sort collapsed to subject — identical
    within a thread — so threads came back in arbitrary order."""
    found = await call("search_global", {"query": corpus["term"], "limit": 5})
    if not found["count"]:
        pytest.skip("nothing indexed for this term")
    seed = found["items"][0]
    thread = await call("search_conversation", {"header_message_id": seed["headerMessageId"]})
    dates = [m["date"] for m in thread["items"]]
    assert all(dates), "a thread with undated messages cannot be ordered"
    assert dates == sorted(dates), "not oldest-first"
    assert thread["participants"], "participants was read but never written"


async def test_a_thread_is_reachable_by_either_identifier(call, corpus) -> None:
    found = await call("search_global", {"query": corpus["term"], "limit": 5})
    if not found["count"]:
        pytest.skip("nothing indexed for this term")
    seed = found["items"][0]
    by_id = await call("search_conversation", {"message_id": seed["id"]})
    for reference in (seed["headerMessageId"], f"<{seed['headerMessageId']}>"):
        by_header = await call("search_conversation", {"header_message_id": reference})
        assert by_header["conversationId"] == by_id["conversationId"], reference


async def test_the_index_reports_how_much_it_holds(call) -> None:
    """`indexedMessages` was null because the count was wrapped in the same
    withTimeout that could not find setTimeout, and the failure was caught."""
    stats = await call("search_index_status", {})
    if not stats.get("enabled"):
        pytest.skip("the global index is disabled on this profile")
    assert isinstance(stats["indexedMessages"], int)


# ------------------------------------------------------- boundaries and limits


@pytest.mark.parametrize("limit", [1, 2, 200])
@pytest.mark.parametrize("tool", ["mail_search", "mail_list", "search_global"])
async def test_every_tool_honours_its_documented_range(call, corpus, tool, limit) -> None:
    """`limit` is clamped to 200, and a defect lived at 90 for as long as nobody
    asked for more than 5: answers past roughly 64 KB dropped the connection,
    because the stream buffer was left at asyncio's default."""
    args = {
        "mail_search": {"subject": corpus["term"]},
        "mail_list": {"folder_id": corpus["folder_id"]},
        "search_global": {"query": corpus["term"]},
    }[tool]
    got = await call(tool, {**args, "limit": limit})
    assert 0 <= got["count"] <= limit, f"asked for {limit}, got {got['count']}"


async def test_a_bad_folder_reference_says_why(call, live_tools) -> None:
    """Errors raised in the privileged half reached the caller as the generic
    "An unexpected error occurred", with the real text stranded in the Error
    Console. That is what made the timer bug take days to find."""
    result = await live_tools.call_tool(
        "search_global", {"query": "anything", "folder_id": "accountZZ://nope"}
    )
    assert result.is_error
    text = " ".join(getattr(block, "text", "") for block in result.content)
    assert "accountZZ" in text, text
    assert "unexpected error" not in text, text


# ---------------------------------------------------------- invariants of search


async def test_offset_windows_one_ordering(call, corpus) -> None:
    """`offset` has to be a window onto a single ranking, not a fresh search."""
    full = await call("search_global", {"query": corpus["term"], "limit": 10})
    if full["count"] < 10:
        pytest.skip("too few indexed matches to window")
    tail = await call("search_global", {"query": corpus["term"], "limit": 5, "offset": 5})
    assert [h["id"] for h in tail["items"]] == [h["id"] for h in full["items"][5:10]]


async def test_a_folder_filter_selects_and_never_invents(call, corpus) -> None:
    """Scoping picks from the corpus matches; it cannot produce a hit the unscoped
    search did not have.

    Only comparable when neither side was cut off by `limit` — otherwise these are
    two different top-N slices, and a folder hit ranked below the unscoped cut
    legitimately appears in one and not the other.
    """
    unscoped = await call("search_global", {"query": corpus["term"], "limit": 200})
    scoped = await call(
        "search_global",
        {"query": corpus["term"], "limit": 200, "folder_id": corpus["folder_id"]},
    )
    complete = (
        not unscoped.get("truncated")
        and not scoped.get("truncated")
        and unscoped["count"] == unscoped.get("matched")
        and scoped["count"] == scoped.get("matched")
    )
    if not complete:
        pytest.skip("the corpus caps one side, so the two are different top-N slices")
    assert {h["id"] for h in scoped["items"]} == {
        h["id"] for h in unscoped["items"] if h["folderId"] == corpus["folder_id"]
    }


async def test_an_exact_total_is_not_called_truncated(call, corpus) -> None:
    """A LIMIT that comes back exactly full cannot tell "this is everything" from
    "the cap cut it off", so the searcher over-fetches one row. When it says the
    result was complete, the count has to be exact."""
    got = await call("search_global", {"query": corpus["term"], "limit": 200})
    if got.get("truncated"):
        pytest.skip("this term genuinely exceeds the retrieval cap")
    assert got["totalAvailable"] == got["matched"]
    assert got["count"] == min(got["matched"], 200)

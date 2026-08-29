"""Paging and cursors, against a real Thunderbird.

Paging is the part of this server most easily wrong in a way that looks right: a
page of plausible messages arrives, and only a comparison against a known-good
reference shows that some are missing. Both defects below shipped, and both
produced output a reader would accept.

Skips itself when no Thunderbird is attached — see the `live_bridge` fixture.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.live]

REFERENCE = 60


async def _walk(call, tool: str, args: dict, step: int, stop_after: int) -> list[int]:
    """Page `step` at a time, collecting ids, until the list ends."""
    seen: list[int] = []
    cursor = None
    while len(seen) < stop_after:
        page = await call(tool, {**args, "limit": step, **({"cursor": cursor} if cursor else {})})
        seen += [m["id"] for m in page["items"]]
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return seen


@pytest.mark.parametrize("step", [1, 3, 7, 25])
@pytest.mark.parametrize("tool", ["mail_list", "mail_search"])
async def test_paging_conserves_the_set(call, corpus, tool, step) -> None:
    """Thunderbird picks the page size and `messages.list` takes no override, so a
    smaller `limit` used to strand the rest of the page: `continueList` advances to
    the *next* page and those messages became unreachable. Paging a 586-message
    folder three at a time skipped nine of every twelve.

    Asserting that consecutive pages merely *differ* is not enough — that is what
    the original check did, and it passed while printing ids 1,2,3 then 11,12,13.
    Compare against one large call instead.
    """
    args = {
        "mail_list": {"folder_id": corpus["folder_id"]},
        "mail_search": {"subject": corpus["term"]},
    }[tool]
    reference = [m["id"] for m in (await call(tool, {**args, "limit": REFERENCE}))["items"]]
    if len(reference) < 12:
        pytest.skip(f"only {len(reference)} messages match; too few to page meaningfully")
    walked = await _walk(call, tool, args, step, len(reference))
    assert walked[: len(reference)] == reference


async def test_a_cursor_reaches_the_end_of_a_short_result(call, corpus) -> None:
    """A final page carries no list id, so stopping mid-page there had nothing to
    hand back and the walk reported itself finished with messages still in hand."""
    reference = [
        m["id"]
        for m in (await call("mail_list", {"folder_id": corpus["folder_id"], "limit": 12}))["items"]
    ]
    if len(reference) < 12:
        pytest.skip("folder too small")
    walked = await _walk(call, "mail_list", {"folder_id": corpus["folder_id"]}, 5, 12)
    assert walked[:12] == reference


async def test_an_evicted_cursor_refuses_rather_than_skipping(call, live_tools, corpus) -> None:
    """The tail cache is bounded, and eviction reintroduced exactly the bug the
    cache exists to prevent: the cursor is usually Thunderbird's own list id, so a
    resume missed the cache, fell through to `continueList`, and returned the next
    page — dropping the evicted messages silently.

    The dangerous outcome here is a *successful* call.
    """
    outstanding = []
    for _ in range(36):  # the cache holds 32
        page = await call("mail_list", {"folder_id": corpus["folder_id"], "limit": 3})
        if not page.get("nextCursor"):
            pytest.skip("folder too small to leave part-read walks outstanding")
        outstanding.append(page["nextCursor"])

    result = await live_tools.call_tool(
        "mail_list", {"folder_id": corpus["folder_id"], "limit": 3, "cursor": outstanding[0]}
    )
    assert result.is_error, (
        "an evicted cursor answered as if it were a valid continuation, "
        "which silently skips the messages its tail held"
    )
    text = " ".join(getattr(block, "text", "") for block in result.content)
    assert "cursor" in text.lower(), text

    # The newest cursor was never evicted and must still work.
    still_good = await call(
        "mail_list", {"folder_id": corpus["folder_id"], "limit": 3, "cursor": outstanding[-1]}
    )
    assert still_good["count"] > 0


async def test_a_large_answer_survives_the_wire(call, corpus) -> None:
    """`ipc.MAX_LINE` names a 64 MiB ceiling, but neither end passed a `limit` to
    asyncio, so the real one was the 64 KiB StreamReader default. Past it
    `readline()` raises and the connection dies, which is what made a wide search
    look like a transport fault."""
    got = await call("search_global", {"query": corpus["term"], "limit": 200})
    if got["count"] < 90:
        pytest.skip(f"only {got['count']} indexed matches; too few to exceed the old ceiling")
    assert got["count"] > 85, "answers past roughly 64 KB used to drop the connection"

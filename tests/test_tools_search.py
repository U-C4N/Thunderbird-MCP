"""The search toolset, through a real in-memory MCP client.

Every test here guards a field the Python layer *read* but the privileged half had
never written. That class of bug is invisible: the key is simply absent, `.get()`
returns None, and the tool answers with a confident-looking envelope that has
quietly dropped the count, the participants, or the scope it searched. A fake
bridge returning the add-on's real shape is enough to pin each one down.
"""

from __future__ import annotations

import pytest
from mcp import Client

from tbmcp.config import Settings
from tbmcp.server import build_server

pytestmark = pytest.mark.anyio

HIT = {
    "id": 110,
    "glodaId": 2154,
    "headerMessageId": "abc@example.com",
    "subject": "Pending release items",
    "author": "Supplier <billing@example.com>",
    "date": "2026-08-26T01:25:23.000Z",
    "folderId": "account1://INBOX",
    "folderUri": "imap://me@example.com/INBOX",
    "conversationId": 718,
    "score": 59,
}


def _server(bridge, **overrides):
    return build_server(Settings().merged_with(toolsets=("search",), **overrides), bridge=bridge)


def _text(result) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


async def test_global_search_reports_how_many_matched(fake_bridge) -> None:
    """The privileged half reports `matched`; reading `totalMatched` instead meant
    every ranked search came back without a count."""
    bridge = fake_bridge(
        {
            "x.gloda.search": {
                "query": "release",
                "hits": [HIT],
                "matched": 33,
                "retrieved": 33,
                "truncated": False,
                "indexEnabled": True,
            }
        }
    )
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("search_global", {"query": "release", "limit": 5})

    assert not result.is_error, _text(result)
    payload = result.structured_content
    assert payload["totalAvailable"] == 33
    assert payload["matched"] == 33
    assert payload["retrieved"] == 33
    assert payload["items"][0]["conversationId"] == 718


async def test_a_capped_ranking_does_not_claim_a_total(fake_bridge) -> None:
    """`matched` counts hits inside a capped retrieval whose size is derived from
    `offset + limit`, so under truncation it is a floor that moves as you page.
    Reporting it as `totalAvailable` would assert a total nobody measured."""
    bridge = fake_bridge(
        {
            "x.gloda.search": {
                "hits": [HIT],
                "matched": 75,
                "retrieved": 1000,
                "truncated": True,
                "indexEnabled": True,
            }
        }
    )
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("search_global", {"query": "release", "limit": 5})

    payload = result.structured_content
    assert "totalAvailable" not in payload
    # The floor is still worth having, just not under a name that means "total".
    assert payload["matched"] == 75
    assert payload["truncated"] is True


async def test_a_complete_ranking_is_not_flagged_as_truncated(fake_bridge) -> None:
    bridge = fake_bridge(
        {
            "x.gloda.search": {
                "hits": [HIT],
                "matched": 1,
                "retrieved": 1,
                "truncated": False,
                "indexEnabled": True,
            }
        }
    )
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("search_global", {"query": "release"})

    payload = result.structured_content
    assert "truncated" not in payload
    assert payload["totalAvailable"] == 1


async def test_global_search_surfaces_the_note_explaining_an_empty_result(fake_bridge) -> None:
    """An empty ranked search is only actionable if it says why it was empty."""
    note = 'The index cannot match "2.0" — it tokenizes into pieces shorter than three'
    bridge = fake_bridge(
        {"x.gloda.search": {"hits": [], "matched": 0, "indexEnabled": True, "note": note}}
    )
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("search_global", {"query": "widget 2.0"})

    payload = result.structured_content
    assert payload["count"] == 0
    assert payload["note"] == note


async def test_conversation_reports_participants_and_length(fake_bridge) -> None:
    """`participants` and `total` are the two fields that make a thread summary
    usable without reading every message back."""
    bridge = fake_bridge(
        {
            "x.gloda.conversation": {
                "conversationId": 718,
                "subject": "Pending release items",
                "participants": ["Ada <ada@example.com>", "Bo <bo@example.com>"],
                "messages": [HIT],
                "total": 8,
            }
        }
    )
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("search_conversation", {"message_id": 110})

    assert not result.is_error, _text(result)
    payload = result.structured_content
    assert payload["conversationId"] == 718
    assert payload["participants"] == ["Ada <ada@example.com>", "Bo <bo@example.com>"]
    # `total` is the whole thread; `count` is only what this page carried.
    assert payload["totalAvailable"] == 8
    assert payload["count"] == 1


async def test_conversation_accepts_either_identifier(fake_bridge) -> None:
    bridge = fake_bridge({"x.gloda.conversation": {"messages": [], "conversationId": 1}})
    async with Client(_server(bridge)) as client:
        await client.call_tool("search_conversation", {"message_id": 110})
        assert bridge.params_for("x.gloda.conversation")["messageId"] == 110

        bridge.calls.clear()
        await client.call_tool("search_conversation", {"header_message_id": "abc@example.com"})
        assert bridge.params_for("x.gloda.conversation")["headerMessageId"] == "abc@example.com"


async def test_conversation_needs_one_of_the_two_identifiers(fake_bridge) -> None:
    bridge = fake_bridge()
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("search_conversation", {})

    assert result.is_error
    assert bridge.calls == []


async def test_global_search_forwards_the_folder_scope(fake_bridge) -> None:
    """Scoping happens in the privileged half; the tool has to actually send it."""
    bridge = fake_bridge({"x.gloda.search": {"hits": [], "matched": 0, "indexEnabled": True}})
    async with Client(_server(bridge)) as client:
        await client.call_tool(
            "search_global", {"query": "release", "folder_id": "account1://INBOX", "offset": 10}
        )

    params = bridge.params_for("x.gloda.search")
    assert params["folderId"] == "account1://INBOX"
    assert params["offset"] == 10

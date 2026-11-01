"""The search toolset, end to end through an in-memory MCP client.

Every assertion here is about a field: which one the add-on actually writes, and
which ones must not appear at all. A `null` in a search result is not neutral —
a model reads `"totalAvailable": null` as a fact about the mailbox rather than as
a field nobody filled in.
"""

from __future__ import annotations

import pytest
from mcp import Client

from tbmcp.config import Settings
from tbmcp.server import build_server

pytestmark = pytest.mark.anyio

HIT = {
    "id": 202,
    "glodaId": 1234,
    "headerMessageId": "<m1@example.invalid>",
    "subject": "Invoice 2026-07",
    "author": "Supplier <billing@example.com>",
    "date": "2026-07-01T09:30:00.000Z",
    "folderId": "account1://INBOX",
    "folderUri": "imap://me@example.com/INBOX",
    "conversationId": 77,
    "score": 12,
}


def _server(bridge):
    return build_server(Settings().merged_with(toolsets=("search",)), bridge=bridge)


def _text(result) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


def _nulls(payload: dict) -> list[str]:
    return [key for key, value in payload.items() if value is None]


def search_result(**overrides) -> dict:
    """What the add-on answers `search.global` with."""
    result = {
        "query": "invoice",
        "items": [HIT],
        "count": 1,
        "totalAvailable": 1,
        "matched": 1,
        "retrieved": 75,
        "truncated": False,
        "indexEnabled": True,
        **overrides,
    }
    if result.get("truncated"):
        result.pop("totalAvailable", None)
    else:
        result["totalAvailable"] = result["matched"]
    return result


async def test_the_match_count_is_the_total_when_nothing_was_cut_off(fake_bridge) -> None:
    bridge = fake_bridge({"search.global": search_result(matched=3)})
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("search_global", {"query": "invoice"})
    assert not result.is_error, _text(result)

    payload = result.structured_content
    assert payload["totalAvailable"] == 3
    assert payload["matched"] == 3
    assert payload["retrieved"] == 75
    assert payload["indexEnabled"] is True
    assert _nulls(payload) == []


async def test_a_truncated_retrieval_reports_no_total(fake_bridge) -> None:
    """`matched` counts what the ranking saw, which is not a total of anything."""
    bridge = fake_bridge({"search.global": search_result(matched=1000, truncated=True)})
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("search_global", {"query": "invoice"})
    assert not result.is_error, _text(result)

    payload = result.structured_content
    assert "totalAvailable" not in payload
    assert payload["truncated"] is True
    assert payload["matched"] == 1000


async def test_unmatchable_terms_and_a_note_reach_the_caller(fake_bridge) -> None:
    bridge = fake_bridge(
        {
            "search.global": search_result(
                hits=[], matched=0, unmatchableTerms=["2.0"], note="The index cannot match 2.0."
            )
        }
    )
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("search_global", {"query": "payments 2.0"})
    assert not result.is_error, _text(result)

    payload = result.structured_content
    assert payload["unmatchableTerms"] == ["2.0"]
    assert payload["note"] == "The index cannot match 2.0."


async def test_nothing_unmatchable_means_no_such_key(fake_bridge) -> None:
    bridge = fake_bridge({"search.global": search_result()})
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("search_global", {"query": "invoice"})
    assert not result.is_error, _text(result)

    payload = result.structured_content
    assert "unmatchableTerms" not in payload
    assert "note" not in payload
    assert _nulls(payload) == []


async def test_a_conversation_reports_its_size_and_who_was_in_it(fake_bridge) -> None:
    bridge = fake_bridge(
        {
            "search.conversation": {
                "conversationId": 77,
                "subject": "Shipment",
                "participants": ["Ada <ada@example.invalid>", "Bob <bob@example.invalid>"],
                "items": [HIT],
                "count": 1,
                "totalAvailable": 3,
            }
        }
    )
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("search_conversation", {"message_id": 202})
    assert not result.is_error, _text(result)

    payload = result.structured_content
    assert payload["totalAvailable"] == 3
    assert payload["subject"] == "Shipment"
    assert payload["participants"][0] == "Ada <ada@example.invalid>"
    assert _nulls(payload) == []


async def test_a_conversation_the_index_knows_little_about_carries_no_nulls(fake_bridge) -> None:
    bridge = fake_bridge(
        {
            "search.conversation": {
                "conversationId": 77,
                "items": [],
                "count": 0,
                "totalAvailable": 0,
            }
        }
    )
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("search_conversation", {"header_message_id": "<m1@x>"})
    assert not result.is_error, _text(result)

    payload = result.structured_content
    assert "subject" not in payload
    assert "participants" not in payload
    assert _nulls(payload) == []


async def test_snapshot_cursor_continues_without_repeating_query(fake_bridge) -> None:
    bridge = fake_bridge({"search.global": {"items": [], "count": 0, "hasMore": False}})
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("search_global", {"cursor": "tbs:e:1:25"})
    assert not result.is_error
    assert bridge.params_for("search.global") == {"cursor": "tbs:e:1:25", "limit": 25}


async def test_cursor_cannot_change_search_scope(fake_bridge) -> None:
    bridge = fake_bridge()
    async with Client(_server(bridge)) as client:
        result = await client.call_tool(
            "search_global", {"cursor": "tbs:e:1:25", "query": "different"}
        )
    assert result.is_error
    assert bridge.calls == []


async def test_conversation_cursor_and_identifier_are_exclusive(fake_bridge) -> None:
    bridge = fake_bridge()
    async with Client(_server(bridge)) as client:
        result = await client.call_tool(
            "search_conversation", {"cursor": "tbs:e:1:25", "message_id": 1}
        )
    assert result.is_error
    assert bridge.calls == []

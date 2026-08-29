"""End-to-end through a real in-memory MCP client.

This is the layer that matters: it exercises schema validation, the consent
resolver, and the error path a model would actually see - none of which are covered
by calling the tool functions directly.
"""

from __future__ import annotations

import pytest
from mcp import Client

from tbmcp.config import Settings
from tbmcp.server import build_server

pytestmark = pytest.mark.anyio

MESSAGE = {
    "id": 42,
    "subject": "Invoice 2026-07",
    "author": "Supplier <billing@example.com>",
    "recipients": ["me@example.com"],
    "date": "2026-07-01T09:30:00Z",
    "read": False,
    "flagged": False,
    "tags": ["$label1"],
    "size": 4096,
    "folderId": "account1://INBOX",
}


def _server(bridge, **overrides):
    # build_server installs its own Bridge unless one is handed in, which would
    # replace the recorder the fixture just set up.
    return build_server(Settings().merged_with(toolsets=("mail",), **overrides), bridge=bridge)


def _text(result) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


async def test_search_requires_at_least_one_filter(fake_bridge) -> None:
    """A bare search over every folder would be slow and useless, so it is refused
    with a message naming the parameter to use instead."""
    bridge = fake_bridge()
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("mail_search", {})
    assert result.is_error
    assert "full_text" in _text(result)
    assert bridge.calls == []


async def test_search_maps_arguments_onto_the_query(fake_bridge) -> None:
    bridge = fake_bridge({"messages.query": {"messages": [MESSAGE], "cursor": "list-1"}})
    async with Client(_server(bridge)) as client:
        result = await client.call_tool(
            "mail_search",
            {"full_text": "invoice", "unread": True, "from_date": "2026-07-01", "limit": 5},
        )
    assert not result.is_error, _text(result)
    params = bridge.params_for("messages.query")
    assert params["query"]["fullText"] == "invoice"
    assert params["query"]["unread"] is True
    assert params["query"]["fromDate"].startswith("2026-07-01")
    assert params["limit"] == 5

    payload = result.structured_content
    assert payload["count"] == 1
    assert payload["items"][0]["subject"] == "Invoice 2026-07"
    assert payload["nextCursor"] == "list-1"


async def test_search_rejects_a_malformed_date(fake_bridge) -> None:
    bridge = fake_bridge()
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("mail_search", {"subject": "x", "from_date": "last week"})
    assert result.is_error
    assert "ISO-8601" in _text(result)


async def test_delete_without_confirmation_is_refused(fake_bridge) -> None:
    bridge = fake_bridge()
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("mail_delete", {"message_ids": [42]})
    assert result.is_error
    assert "confirm=true" in _text(result)
    assert "messages.delete" not in bridge.methods(), "it deleted anyway"


async def test_delete_with_confirmation_goes_through(fake_bridge) -> None:
    bridge = fake_bridge({"messages.delete": {"deleted": 1, "permanent": False}})
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("mail_delete", {"message_ids": [42], "confirm": True})
    assert not result.is_error, _text(result)
    assert bridge.params_for("messages.delete") == {"messageIds": [42], "permanent": False}
    payload = result.structured_content
    assert payload["changed"] is True
    assert payload["recoverable"] is True
    assert payload["previous"] == {"messageIds": [42]}


async def test_dry_run_reports_the_plan_without_calling_thunderbird(fake_bridge) -> None:
    bridge = fake_bridge()
    async with Client(_server(bridge)) as client:
        result = await client.call_tool(
            "mail_delete", {"message_ids": [1, 2], "dry_run_only": True}
        )
    assert not result.is_error, _text(result)
    payload = result.structured_content
    assert payload["dryRun"] is True and payload["changed"] is False
    assert bridge.calls == []


async def test_permanent_delete_is_reported_as_unrecoverable(fake_bridge) -> None:
    bridge = fake_bridge({"messages.delete": {"deleted": 1, "permanent": True}})
    async with Client(_server(bridge)) as client:
        result = await client.call_tool(
            "mail_delete", {"message_ids": [42], "permanent": True, "confirm": True}
        )
    assert not result.is_error, _text(result)
    assert result.structured_content["recoverable"] is False


async def test_mark_needs_something_to_change(fake_bridge) -> None:
    bridge = fake_bridge()
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("mail_mark", {"message_ids": [1]})
    assert result.is_error
    assert "add_tags" in _text(result)


async def test_mark_is_not_gated(fake_bridge) -> None:
    """Flags are cheap and reversible; a confirmation prompt for them would train the
    user to click through the prompts that do matter."""
    bridge = fake_bridge({"messages.mark": {"updated": 1}})
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("mail_mark", {"message_ids": [1], "read": True})
    assert not result.is_error, _text(result)
    assert bridge.params_for("messages.mark")["read"] is True


async def test_empty_id_list_is_rejected(fake_bridge) -> None:
    bridge = fake_bridge()
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("mail_mark", {"message_ids": [], "read": True})
    assert result.is_error


async def test_read_only_mode_hides_the_write_tools(fake_bridge) -> None:
    bridge = fake_bridge()
    async with Client(_server(bridge, read_only=True)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert "mail_search" in names
    assert "mail_delete" not in names
    assert "mail_move" not in names


async def test_yolo_skips_the_gate(fake_bridge) -> None:
    bridge = fake_bridge({"messages.delete": {"deleted": 1}})
    async with Client(_server(bridge, yolo=True)) as client:
        result = await client.call_tool("mail_delete", {"message_ids": [42]})
    assert not result.is_error, _text(result)
    assert "messages.delete" in bridge.methods()


async def test_move_reports_the_source_folders(fake_bridge) -> None:
    """Without this a move is not undoable: the ids change, and the old location is
    gone from the caller's point of view."""
    bridge = fake_bridge({"messages.move": {"moved": 2, "sourceFolderIds": ["account1://INBOX"]}})
    async with Client(_server(bridge)) as client:
        result = await client.call_tool(
            "mail_move",
            {
                "message_ids": [1, 2],
                "destination_folder_id": "account1://Archive",
                "confirm": True,
            },
        )
    assert not result.is_error, _text(result)
    payload = result.structured_content
    assert payload["previous"]["folderIds"] == ["account1://INBOX"]
    assert payload["current"]["folderId"] == "account1://Archive"
    assert "re-query" in payload["note"]
    assert bridge.params_for("messages.move")["destinationFolderId"] == "account1://Archive"


async def test_body_is_truncated_rather_than_dropped(fake_bridge) -> None:
    from tbmcp.tools._common import MAX_TEXT

    bridge = fake_bridge(
        {
            "messages.read": {
                "header": MESSAGE,
                "body": "x" * (MAX_TEXT + 100),
                "bodyIsHtml": False,
                "attachments": [],
            }
        }
    )
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("mail_get", {"message_id": 42})
    assert not result.is_error, _text(result)
    payload = result.structured_content
    assert payload["bodyTruncated"] is True
    assert len(payload["body"]) == MAX_TEXT


async def test_get_many_has_a_ceiling(fake_bridge) -> None:
    bridge = fake_bridge()
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("mail_get_many", {"message_ids": list(range(51))})
    assert result.is_error
    assert "50" in _text(result)


async def test_search_reports_the_scope_it_searched(fake_bridge) -> None:
    """An empty result is only interpretable next to what was actually searched."""
    scope = {
        "scope": "folders",
        "accountIds": ["account1"],
        "folderIds": ["account1://INBOX"],
        "includeSubFolders": True,
    }
    bridge = fake_bridge({"messages.query": {"messages": [], "searchedFolders": scope}})
    async with Client(_server(bridge)) as client:
        result = await client.call_tool("mail_search", {"subject": "nothing", "limit": 5})

    assert result.structured_content["searchedFolders"] == scope


async def test_continuing_a_search_does_not_restate_the_scope(fake_bridge) -> None:
    """The add-on computes the scope only when a walk starts, because it cannot
    change mid-walk and costs a subfolder enumeration. The key is then absent
    rather than null: null would read as "scope unknown"."""
    bridge = fake_bridge({"messages.query": {"messages": [], "cursor": None}})
    async with Client(_server(bridge)) as client:
        result = await client.call_tool(
            "mail_search", {"subject": "nothing", "cursor": "list-1", "limit": 5}
        )

    assert "searchedFolders" not in result.structured_content

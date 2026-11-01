"""Batch inputs and confirmation gates through a real MCP session."""

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from tbmcp import safety
from tbmcp.config import Settings
from tbmcp.server import Registrar
from tbmcp.tools import batch

pytestmark = pytest.mark.anyio


def server(*, read_only=False):
    settings = Settings(read_only=read_only)
    safety.set_settings(settings)
    mcp = MCPServer(name="batch-tests")
    batch.register(Registrar(mcp, settings))
    return mcp


async def test_preview_freezes_unique_ids_and_keeps_explicit_false(fake_bridge):
    bridge = fake_bridge({"daemon.operations.plan": {"planId": "plan-a", "expiresAt": 999}})
    async with Client(server()) as client:
        result = await client.call_tool(
            "mail_batch_preview",
            {
                "message_ids": [5, 5, 7],
                "action": "mark",
                "read": False,
                "add_tags": ["todo", "todo"],
            },
        )
    assert not result.is_error
    assert result.structured_content["planId"] == "plan-a"
    assert bridge.params_for("daemon.operations.plan") == {
        "kind": "batch",
        "params": {
            "messageIds": [5, 7],
            "action": "mark",
            "changes": {"read": False, "addTags": ["todo"]},
        },
    }


async def test_changes_object_keeps_explicit_false_and_tag_edits(fake_bridge):
    bridge = fake_bridge({"daemon.operations.plan": {"planId": "plan-a"}})
    async with Client(server()) as client:
        result = await client.call_tool(
            "mail_batch_preview",
            {
                "message_ids": [5],
                "action": "mark",
                "changes": {"read": False, "add_tags": ["todo"]},
            },
        )
    assert not result.is_error
    assert bridge.params_for("daemon.operations.plan")["params"]["changes"] == {
        "read": False,
        "addTags": ["todo"],
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"changes": {"read": True}, "read": False},
        {"changes": {"junk": True}},
        {"changes": {"read": "false"}},
        {"changes": {"add_tags": "todo"}},
        {"changes": {"addTags": ["a"], "add_tags": ["b"]}},
    ],
)
async def test_invalid_changes_object_never_reaches_daemon(fake_bridge, fields):
    bridge = fake_bridge()
    async with Client(server()) as client:
        result = await client.call_tool(
            "mail_batch_preview", {"message_ids": [1], "action": "mark", **fields}
        )
    assert result.is_error
    assert not bridge.calls


@pytest.mark.parametrize(
    "arguments",
    [
        {"message_ids": [], "action": "mark", "read": True},
        {"message_ids": list(range(501)), "action": "mark", "read": True},
        {"message_ids": [1], "action": "mark"},
        {"message_ids": [1], "action": "mark", "read": True, "destination_folder_id": "a://target"},
        {"message_ids": [1], "action": "move"},
        {"message_ids": [1], "action": "move", "destination_folder_id": " "},
        {"message_ids": [1], "action": "move", "read": True, "destination_folder_id": "a://target"},
        {"message_ids": [1], "action": "mark", "add_tags": ["todo"], "remove_tags": ["todo"]},
        {"message_ids": [1], "action": "mark", "add_tags": [" "]},
    ],
)
async def test_invalid_preview_never_reaches_daemon(fake_bridge, arguments):
    bridge = fake_bridge()
    async with Client(server()) as client:
        result = await client.call_tool("mail_batch_preview", arguments)
    assert result.is_error
    assert bridge.calls == []


async def test_move_preview_sends_no_mark_changes(fake_bridge):
    bridge = fake_bridge()
    async with Client(server()) as client:
        result = await client.call_tool(
            "mail_batch_preview",
            {"message_ids": [1, 2], "action": "move", "destination_folder_id": "a://target"},
        )
    assert not result.is_error
    assert bridge.params_for("daemon.operations.plan")["params"] == {
        "messageIds": [1, 2],
        "action": "move",
        "destinationFolderId": "a://target",
    }


@pytest.mark.parametrize(
    ("tool", "argument", "method", "wire_key"),
    [
        ("mail_batch_apply", "plan_id", "daemon.operations.apply", "planId"),
        ("mail_batch_undo", "operation_id", "daemon.operations.undo", "operationId"),
    ],
)
async def test_apply_and_undo_require_consent(fake_bridge, tool, argument, method, wire_key):
    bridge = fake_bridge({method: {"operationId": "op-a", "state": "queued"}})
    async with Client(server()) as client:
        denied = await client.call_tool(tool, {argument: "saved-a"})
        assert denied.is_error
        assert bridge.calls == []
        accepted = await client.call_tool(tool, {argument: "saved-a", "confirm": True})
        assert not accepted.is_error
        assert accepted.structured_content["operationId"] == "op-a"
    assert bridge.calls == [(method, {wire_key: "saved-a"})]


async def test_read_only_keeps_preview_but_removes_applying_and_undoing(fake_bridge):
    bridge = fake_bridge()
    async with Client(server(read_only=True)) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools.tools}
        assert names == {"mail_batch_preview"}
        result = await client.call_tool(
            "mail_batch_preview", {"message_ids": [1], "action": "mark", "flagged": True}
        )
        assert not result.is_error
    assert bridge.methods() == ["daemon.operations.plan"]

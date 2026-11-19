"""Browsing one folder's subtree.

`folder_list(parent_id=…)` never worked: it put `folderId` and `recursive` into the
`folders.query` queryInfo, and that schema knows `folderId` (one folder) but not
`recursive`, so Thunderbird rejected every such call with

    Type error for parameter queryInfo (Unexpected property "recursive")

The folder tree cannot be filtered by parent at all — `folders.query` has no
`parentId` — so the walk happens first and the subtree is kept here.
"""

from __future__ import annotations

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from tbmcp import safety
from tbmcp.config import Settings
from tbmcp.server import Registrar
from tbmcp.tools import folders

pytestmark = pytest.mark.anyio


def server() -> MCPServer:
    settings = Settings()
    safety.set_settings(settings)
    mcp = MCPServer(name="folder-tests")
    folders.register(Registrar(mcp, settings))
    return mcp


def tree(folders_: list[dict]) -> dict:
    """A `folders.query` answer shaped like the add-on's."""
    return {"folders": folders_, "searched": False, "truncated": False}


ROOT = {"id": "account2://", "accountId": "account2", "path": "/", "name": "Root", "depth": 0}
ARCHIVE = {
    "id": "account2://Archives",
    "accountId": "account2",
    "path": "/Archives",
    "name": "Archiv",
    "depth": 1,
    "totalMessageCount": 0,
}
Y2017 = {
    "id": "account2://Archives/2017",
    "accountId": "account2",
    "path": "/Archives/2017",
    "name": "2017",
    "depth": 2,
    "totalMessageCount": 12,
}
Y2018 = {**Y2017, "id": "account2://Archives/2018", "path": "/Archives/2018", "name": "2018"}
INBOX = {
    "id": "account1://INBOX",
    "accountId": "account1",
    "path": "/INBOX",
    "name": "Posteingang",
    "depth": 1,
}
SENT = {
    "id": "account1://Sent",
    "accountId": "account1",
    "path": "/Sent",
    "name": "Gesendet",
    "depth": 1,
}
SUB = {
    "id": "account2://Archives/2017/Q1",
    "accountId": "account2",
    "path": "/Archives/2017/Q1",
    "name": "Q1",
    "depth": 3,
}


async def call(fake_bridge, response, **arguments):
    bridge = fake_bridge({"folders.query": response})
    async with Client(server()) as client:
        result = await client.call_tool("folder_list", arguments)
    return bridge, result


async def test_parent_id_returns_the_children_not_the_folder_itself(fake_bridge):
    _, result = await call(
        fake_bridge, tree([ROOT, ARCHIVE, Y2017, Y2018, INBOX]), parent_id="account2://Archives"
    )

    assert not result.is_error, result
    names = [item["name"] for item in result.structured_content["items"]]
    assert names == ["2017", "2018"]
    assert result.structured_content["items"][0]["totalMessageCount"] == 12


async def test_the_query_carries_no_schema_it_would_reject(fake_bridge):
    bridge, _ = await call(fake_bridge, tree([ARCHIVE, Y2017]), parent_id="account2://Archives")

    query = bridge.params_for("folders.query")["query"]
    assert query == {"accountId": "account2"}
    assert "recursive" not in query and "folderId" not in query


async def test_the_walk_reaches_exactly_one_level_below_the_parent(fake_bridge):
    bridge, _ = await call(fake_bridge, tree([ARCHIVE, Y2017]), parent_id="account2://Archives")

    # /Archives is depth 1, so one level below it is depth 2.
    assert bridge.params_for("folders.query")["depth"] == 2


async def test_depth_counts_levels_below_the_parent(fake_bridge):
    bridge, _ = await call(
        fake_bridge, tree([ARCHIVE, Y2017, SUB]), parent_id="account2://Archives", depth=2
    )

    assert bridge.params_for("folders.query")["depth"] == 3


async def test_the_account_root_returns_the_top_level_folders(fake_bridge):
    _, result = await call(fake_bridge, tree([ROOT, ARCHIVE, INBOX, SENT]), parent_id="account2://")

    assert [item["path"] for item in result.structured_content["items"]] == ["/Archives"]


async def test_another_accounts_folders_never_leak_in(fake_bridge):
    _, result = await call(
        fake_bridge, tree([ROOT, ARCHIVE, INBOX]), parent_id="account2://Archives"
    )

    assert result.structured_content["items"] == []


async def test_tree_nests_the_subtree(fake_bridge):
    _, result = await call(
        fake_bridge,
        tree([ROOT, ARCHIVE, Y2017, SUB]),
        parent_id="account2://Archives",
        depth=2,
        tree=True,
    )

    items = result.structured_content["items"]
    assert [item["name"] for item in items] == ["2017"]
    assert [child["name"] for child in items[0]["children"]] == ["Q1"]


async def test_tree_is_left_to_the_add_on_when_no_parent_is_given(fake_bridge):
    bridge, _ = await call(fake_bridge, tree([]), tree=True)

    assert bridge.params_for("folders.query")["tree"] is True


async def test_a_parent_from_another_account_is_a_usage_error(fake_bridge):
    bridge = fake_bridge({})
    async with Client(server()) as client:
        result = await client.call_tool(
            "folder_list", {"account_id": "account1", "parent_id": "account2://Archives"}
        )

    assert result.is_error
    assert "account2" in result.content[0].text
    assert bridge.methods() == ["daemon.status"] or "folders.query" not in bridge.methods()

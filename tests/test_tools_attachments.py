"""Public attachment selection and confirmation gates."""

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from tbmcp import safety
from tbmcp.config import Settings
from tbmcp.server import Registrar
from tbmcp.tools.attachments import register

pytestmark = pytest.mark.anyio


def server(read_only=False):
    app = MCPServer(name="attachments-test")
    settings = Settings().merged_with(read_only=read_only)
    safety.set_settings(settings)
    register(Registrar(app, settings))
    return app


async def test_plan_preserves_filters_and_deduplicates_ids(fake_bridge):
    bridge = fake_bridge({"daemon.operations.plan": {"planId": "plan"}})
    async with Client(server()) as client:
        result = await client.call_tool(
            "mail_attachments_plan",
            {
                "message_ids": [1, 1, 2],
                "directory": "C:/export",
                "filename_contains": "fatura",
                "content_types": ["application/pdf"],
            },
        )
    assert not result.is_error
    assert bridge.params_for("daemon.operations.plan") == {
        "kind": "attachments",
        "params": {
            "messageIds": [1, 2],
            "directory": "C:/export",
            "filenameContains": "fatura",
            "contentTypes": ["application/pdf"],
        },
    }


async def test_save_requires_confirmation_and_reuses_plan(fake_bridge):
    bridge = fake_bridge({"daemon.operations.apply": {"operationId": "job"}})
    async with Client(server()) as client:
        denied = await client.call_tool("mail_attachments_save", {"plan_id": "plan"})
        assert denied.is_error
        assert not bridge.calls
        result = await client.call_tool(
            "mail_attachments_save", {"plan_id": "plan", "confirm": True}
        )
    assert not result.is_error
    assert bridge.params_for("daemon.operations.apply") == {"planId": "plan"}


async def test_more_than_100_sources_is_rejected_before_bridge(fake_bridge):
    bridge = fake_bridge()
    async with Client(server()) as client:
        result = await client.call_tool(
            "mail_attachments_plan", {"message_ids": list(range(101)), "directory": "C:/export"}
        )
    assert result.is_error
    assert not bridge.calls

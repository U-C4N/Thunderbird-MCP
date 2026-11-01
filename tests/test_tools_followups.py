"""The follow-up tool contract, without a running Thunderbird."""

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from tbmcp.config import Settings
from tbmcp.server import Registrar
from tbmcp.tools.followups import register

pytestmark = pytest.mark.anyio


def server():
    app = MCPServer(name="followups-test")
    register(Registrar(app, Settings()))
    return app


async def test_defaults_and_evidence_are_preserved(fake_bridge):
    payload = {
        "items": [{"messageId": 5, "evidence": {"kind": "thread_headers"}}],
        "partial": False,
    }
    bridge = fake_bridge({"messages.followups": payload})
    async with Client(server()) as client:
        result = await client.call_tool("mail_followups", {})
    assert not result.is_error
    assert result.structured_content == payload
    assert bridge.params_for("messages.followups") == {
        "lookbackDays": 30,
        "waitingDays": 7,
        "limit": 25,
    }


@pytest.mark.parametrize(
    "params",
    [
        {"lookback_days": 181},
        {"waiting_days": 31},
        {"waiting_days": 0},
        {"cursor": "old", "account_id": "account1"},
    ],
)
async def test_invalid_scope_never_calls_thunderbird(fake_bridge, params):
    bridge = fake_bridge()
    async with Client(server()) as client:
        result = await client.call_tool("mail_followups", params)
    assert result.is_error
    assert not bridge.calls


async def test_cursor_reads_existing_snapshot(fake_bridge):
    bridge = fake_bridge({"messages.followups": {"items": [], "count": 0}})
    async with Client(server()) as client:
        result = await client.call_tool("mail_followups", {"cursor": "tbs:e:1:25", "limit": 50})
    assert not result.is_error
    assert bridge.params_for("messages.followups") == {"cursor": "tbs:e:1:25", "limit": 50}

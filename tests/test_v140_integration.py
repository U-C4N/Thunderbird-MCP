"""Release boundaries: old add-ons, custom toolsets and honest job outcomes."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp import Client

from tbmcp.config import Settings
from tbmcp.daemon import AddonSession
from tbmcp.errors import BlockedError, TbmcpError, ThunderbirdError
from tbmcp.operations import Operations
from tbmcp.server import build_server

pytestmark = pytest.mark.anyio


def unavailable(_params):
    raise BlockedError("Update the add-on.", code="UNSUPPORTED_CAPABILITY")


class OldAddonSocket:
    """If reached, reproduce the old registry's untyped unknown-method error."""

    def __init__(self):
        self.frames = []
        self.session = None

    async def send(self, raw):
        frame = json.loads(raw)
        self.frames.append(frame)
        await self.session._handle(
            {
                "t": "res",
                "id": frame["id"],
                "ok": False,
                "error": {"kind": "usage", "message": "unknown method " + frame["method"]},
            },
            lambda _: None,
        )


@pytest.mark.parametrize("method", ["search.global", "search.conversation", "messages.followups"])
@pytest.mark.parametrize("methods", [None, ["x.gloda.search", "x.gloda.conversation"]])
async def test_new_read_rpc_rejects_old_addon_before_dispatch(method, methods):
    socket = OldAddonSocket()
    capabilities = {"experiment": True}
    if methods is not None:
        capabilities["methods"] = methods
    socket.session = AddonSession(socket, {"addonVersion": "1.3.0", "capabilities": capabilities})
    with pytest.raises(TbmcpError) as caught:
        await socket.session.call(method, {}, timeout=0.2)
    assert caught.value.code == "UNSUPPORTED_CAPABILITY"
    assert socket.frames == []


async def test_old_addon_global_search_keeps_offset_without_claiming_snapshot(fake_bridge):
    bridge = fake_bridge(
        {
            "search.global": unavailable,
            "x.gloda.search": {
                "hits": [{"id": 9}],
                "matched": 4,
                "retrieved": 50,
                "indexEnabled": True,
            },
        }
    )
    server = build_server(Settings().merged_with(toolsets=("search",)), bridge=bridge)
    async with Client(server) as client:
        result = await client.call_tool("search_global", {"query": "invoice", "offset": 2})
    assert not result.is_error
    payload = result.structured_content
    assert payload["items"] == [{"id": 9}]
    assert payload["snapshotSupported"] is False and payload["pagination"] == "offset"
    assert payload["coverage"] == "indexed_only" and payload["totalAvailable"] == 4
    assert "nextCursor" not in payload and "snapshotAt" not in payload
    assert bridge.params_for("x.gloda.search")["offset"] == 2


async def test_old_addon_truncated_search_never_reports_corpus_total(fake_bridge):
    bridge = fake_bridge(
        {
            "search.global": unavailable,
            "x.gloda.search": {"hits": [], "matched": 500, "truncated": True},
        }
    )
    server = build_server(Settings().merged_with(toolsets=("search",)), bridge=bridge)
    async with Client(server) as client:
        result = await client.call_tool("search_global", {"query": "invoice"})
    payload = result.structured_content
    assert "totalAvailable" not in payload
    assert payload["partialReasons"] == ["candidate_limit"]


async def test_old_addon_conversation_retains_total_without_fabricating_cursor(fake_bridge):
    bridge = fake_bridge(
        {
            "search.conversation": unavailable,
            "x.gloda.conversation": {"messages": [{"id": 9}], "total": 4, "conversationId": 12},
        }
    )
    server = build_server(Settings().merged_with(toolsets=("search",)), bridge=bridge)
    async with Client(server) as client:
        result = await client.call_tool("search_conversation", {"message_id": 9})
    payload = result.structured_content
    assert payload["totalAvailable"] == 4 and payload["truncated"] is True
    assert payload["snapshotSupported"] is False and "nextCursor" not in payload
    assert bridge.params_for("x.gloda.conversation")["messageId"] == 9


@pytest.mark.parametrize(
    "tool,method",
    [("search_global", "search.global"), ("search_conversation", "search.conversation")],
)
async def test_old_addon_cursor_cannot_silently_restart_search(fake_bridge, tool, method):
    bridge = fake_bridge({method: unavailable})
    server = build_server(Settings().merged_with(toolsets=("search",)), bridge=bridge)
    async with Client(server) as client:
        result = await client.call_tool(tool, {"cursor": "tbs:old:1:25"})
    assert result.is_error
    assert bridge.methods() == [method]


async def test_search_backend_failure_does_not_switch_engines(fake_bridge):
    def failed(_):
        raise ThunderbirdError("Index read failed.", code="INDEX_FAILED")

    bridge = fake_bridge({"search.global": failed})
    server = build_server(Settings().merged_with(toolsets=("search",)), bridge=bridge)
    async with Client(server) as client:
        result = await client.call_tool("search_global", {"query": "invoice"})
    assert result.is_error
    assert bridge.methods() == ["search.global"]


@pytest.mark.parametrize("toolsets", [("mail",), ("compose",), ("mail", "compose", "admin")])
@pytest.mark.parametrize("read_only", [False, True])
async def test_custom_toolsets_keep_operation_observers_and_policy(toolsets, read_only):
    tools = await build_server(
        Settings().merged_with(toolsets=toolsets, read_only=read_only)
    ).list_tools()
    names = [tool.name for tool in tools]
    assert names.count("tb_operation_status") == names.count("tb_operation_list") == 1
    assert names.count("tb_operation_cancel") == (0 if read_only else 1)
    if "mail" in toolsets:
        assert "mail_followups" in names and "mail_attachments_plan" in names
        assert ("mail_attachments_save" in names) is not read_only
    if read_only:
        assert all(tool.annotations.read_only_hint for tool in tools)


async def test_dispatched_send_cancellation_preserves_real_outcome(tmp_path):
    started, complete = asyncio.Event(), asyncio.Event()
    calls = []

    async def guarded(method, params, **_kwargs):
        calls.append(method)
        started.set()
        await complete.wait()
        return {"status": "succeeded", "mode": "later", "queued": True, "sent": False}

    daemon = SimpleNamespace(
        session=SimpleNamespace(
            hello={"runtimeEpoch": "current"},
            capabilities={"methods": ["compose.send", "operations.receipt"]},
        ),
        _write_lock=asyncio.Lock(),
        _guarded_call=guarded,
    )
    operations = Operations(daemon, tmp_path / "journal.sqlite")
    job = asyncio.create_task(
        operations.submit({"kind": "send", "requestKey": "once", "params": {"mode": "later"}})
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        ident = operations.list()["items"][0]["operationId"]
        cancelled = await operations.call("cancel", {"operationId": ident})
        assert cancelled["cancellationTooLate"] is True
        assert cancelled["status"] == "running" and cancelled["cancelRequested"] is False
        complete.set()
        result = await job
        assert result["status"] == "succeeded" and result["queued"] is True
        assert result["sent"] is False and calls == ["compose.send"]
    finally:
        complete.set()
        await job
        operations.journal.db.close()

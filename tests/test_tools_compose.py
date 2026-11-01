"""Compose contracts through the real MCP client, without accessing Thunderbird."""

from __future__ import annotations

import pytest
from mcp import Client

from tbmcp.config import Settings
from tbmcp.errors import UnsupportedError
from tbmcp.server import build_server

pytestmark = pytest.mark.anyio


def server(bridge, **kwargs):
    return build_server(Settings().merged_with(toolsets=("compose",), **kwargs), bridge=bridge)


def text(result):
    return " ".join(getattr(item, "text", "") for item in result.content)


@pytest.mark.parametrize("params", [{}, {"message_id": 2, "draft_ref": "d1"}, {"draft_ref": " "}])
async def test_get_requires_exactly_one_reference(fake_bridge, params):
    bridge = fake_bridge()
    async with Client(server(bridge)) as client:
        result = await client.call_tool("mail_draft_get", params)
    assert result.is_error
    assert not bridge.calls


async def test_get_returns_recorded_snapshot_without_compose(fake_bridge):
    bridge = fake_bridge(
        {"daemon.drafts.get": {"draftRef": "d1", "revision": "r1", "body": "hello"}}
    )
    async with Client(server(bridge)) as client:
        result = await client.call_tool("mail_draft_get", {"message_id": 2})
    assert not result.is_error, text(result)
    assert bridge.calls == [("daemon.drafts.get", {"messageId": 2})]
    assert result.structured_content["revision"] == "r1"


async def test_update_preserves_empty_recipient_list_and_omits_other_fields(fake_bridge):
    bridge = fake_bridge({"daemon.operations.submit": {"status": "succeeded", "operationId": "op"}})
    async with Client(server(bridge)) as client:
        result = await client.call_tool(
            "mail_draft_update",
            {
                "draft_ref": "d1",
                "expected_revision": "r1",
                "request_key": "edit1",
                "cc": [],
            },
        )
    assert not result.is_error, text(result)
    assert bridge.params_for("daemon.operations.submit") == {
        "kind": "draft_update",
        "requestKey": "edit1",
        "params": {"draftRef": "d1", "expectedRevision": "r1", "changes": {"cc": []}},
    }


async def test_update_changes_object_preserves_empty_recipients_and_turkish(fake_bridge):
    bridge = fake_bridge({"daemon.operations.submit": {"status": "succeeded", "operationId": "op"}})
    async with Client(server(bridge)) as client:
        result = await client.call_tool(
            "mail_draft_update",
            {
                "draft_ref": "d1",
                "expected_revision": "r1",
                "request_key": "edit1",
                "changes": {"subject": "İş görüşmesi", "cc": [], "body": "İyi çalışmalar"},
            },
        )
    assert not result.is_error, text(result)
    assert bridge.params_for("daemon.operations.submit")["params"]["changes"] == {
        "subject": "İş görüşmesi",
        "cc": [],
        "body": "İyi çalışmalar",
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"changes": {"subject": "a"}, "subject": "b"},
        {"changes": {"identity_id": "other"}},
        {"changes": {"body": None}},
        {"changes": {"cc": "somebody@example.invalid"}},
        {"changes": {"addAttachments": [], "add_attachments": []}},
    ],
)
async def test_invalid_changes_object_never_submits(fake_bridge, fields):
    bridge = fake_bridge()
    async with Client(server(bridge)) as client:
        result = await client.call_tool(
            "mail_draft_update",
            {
                "draft_ref": "d1",
                "expected_revision": "r1",
                "request_key": "edit1",
                **fields,
            },
        )
    assert result.is_error
    assert not bridge.calls


@pytest.mark.parametrize("change", [{}, {"body": "a" * 200001}, {"to": ["wrong"]}])
async def test_invalid_update_never_submits(fake_bridge, change):
    bridge = fake_bridge()
    async with Client(server(bridge)) as client:
        result = await client.call_tool(
            "mail_draft_update",
            {
                "draft_ref": "d1",
                "expected_revision": "r1",
                "request_key": "edit1",
                **change,
            },
        )
    assert result.is_error
    assert not bridge.calls


async def test_saved_draft_send_requires_approval(fake_bridge):
    bridge = fake_bridge()
    async with Client(server(bridge)) as client:
        result = await client.call_tool(
            "mail_draft_send",
            {
                "draft_ref": "d1",
                "expected_revision": "r1",
                "request_key": "send1",
            },
        )
    assert result.is_error
    assert "confirm=true" in text(result)
    assert not bridge.calls


async def test_saved_draft_send_unknown_is_not_sent(fake_bridge):
    bridge = fake_bridge({"daemon.operations.submit": {"status": "unknown", "operationId": "op"}})
    async with Client(server(bridge)) as client:
        result = await client.call_tool(
            "mail_draft_send",
            {
                "draft_ref": "d1",
                "expected_revision": "r1",
                "request_key": "send1",
                "confirm": True,
            },
        )
    assert not result.is_error, text(result)
    assert result.structured_content["status"] == "unknown"
    assert not result.structured_content.get("sent")
    assert bridge.params_for("daemon.operations.submit")["kind"] == "draft_send"


@pytest.mark.parametrize(
    "name,params,kind",
    [
        ("mail_send", {"to": ["you@example.com"], "subject": "hello", "body": "hi"}, "send"),
        ("mail_reply", {"message_id": 2, "body": "hi"}, "reply"),
        ("mail_forward", {"message_id": 2, "to": ["you@example.com"]}, "forward"),
    ],
)
async def test_legacy_actual_sends_use_journal(fake_bridge, name, params, kind):
    bridge = fake_bridge({"daemon.operations.submit": {"status": "running", "operationId": "op"}})
    async with Client(server(bridge)) as client:
        result = await client.call_tool(
            name, {**params, "mode": "send", "confirm": True, "request_key": "k"}
        )
    assert not result.is_error, text(result)
    assert bridge.params_for("daemon.operations.submit")["kind"] == kind
    assert result.structured_content["status"] == "running"
    assert not result.structured_content.get("sent")


async def test_legacy_draft_default_does_not_submit_send(fake_bridge):
    bridge = fake_bridge({"compose.send": {"messageId": 3}})
    async with Client(server(bridge)) as client:
        result = await client.call_tool(
            "mail_send",
            {
                "to": ["you@example.com"],
                "subject": "hello",
                "body": "hi",
                "confirm": True,
            },
        )
    assert not result.is_error, text(result)
    assert bridge.methods() == ["compose.send"]
    assert result.structured_content["sent"] is False


async def test_explicit_key_applies_to_default_draft_mode(fake_bridge):
    bridge = fake_bridge(
        {"daemon.operations.submit": {"operationId": "one", "status": "succeeded", "messageId": 8}}
    )
    async with Client(server(bridge)) as client:
        result = await client.call_tool(
            "mail_send",
            {
                "to": ["you@example.com"],
                "subject": "review",
                "body": "hello",
                "request_key": "one",
                "confirm": True,
            },
        )
    assert not result.is_error, text(result)
    assert bridge.params_for("daemon.operations.submit")["params"]["mode"] == "draft"
    assert result.structured_content["draftId"] == 8 and result.structured_content["saved"]
    assert not result.structured_content["sent"]


async def test_saved_outcome_survives_unavailable_handle_registration(fake_bridge):
    def unsupported(_params):
        raise UnsupportedError("old addon")

    bridge = fake_bridge({"compose.save": {"messageId": 3}, "daemon.drafts.get": unsupported})
    async with Client(server(bridge)) as client:
        result = await client.call_tool("mail_draft_save", {"subject": "hello"})
    assert not result.is_error, text(result)
    assert result.structured_content["saved"] is True
    assert result.structured_content["draftHandleAvailable"] is False


async def test_read_only_has_get_but_no_draft_mutations(fake_bridge):
    bridge = fake_bridge()
    async with Client(server(bridge, read_only=True)) as client:
        tools = await client.list_tools()
    names = {tool.name for tool in tools.tools}
    assert "mail_draft_get" in names
    assert not {"mail_draft_update", "mail_draft_send", "mail_draft_save"} & names

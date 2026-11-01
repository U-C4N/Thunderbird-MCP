"""Crash/replay/privacy contracts for the shared operation worker."""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from tbmcp.errors import (
    BlockedError,
    NotConnectedError,
    ThunderbirdError,
    TransportError,
    UsageError,
)
from tbmcp.methods import replay_safe
from tbmcp.operations import RETENTION, Journal, Operations, digest


def reference(epoch="one", ident=1):
    return {
        "accountId": "a",
        "folderPath": "/Drafts",
        "folderId": "a://Drafts",
        "headerMessageId": "opaque@example.invalid",
        "fingerprint": "hash",
        "runtimeEpoch": epoch,
        "messageId": ident,
    }


class DaemonDouble:
    def __init__(self):
        self._write_lock = asyncio.Lock()
        self.calls = []
        self.result = {"status": "succeeded", "reference": reference(), "revision": "r2"}
        self.error = None
        self.gate = None
        self.session = SimpleNamespace(
            hello={"runtimeEpoch": "one"},
            capabilities={
                "methods": [
                    "messages.batchPreview",
                    "messages.batchStep",
                    "messages.batchUndo",
                    "operations.receipt",
                    "compose.draftGet",
                    "compose.draftUpdate",
                    "compose.draftSend",
                    "compose.send",
                    "messages.attachmentsPrepare",
                    "messages.attachmentsSave",
                    "messages.attachmentsReconcile",
                ]
            },
        )

    def _not_connected(self):
        return NotConnectedError()

    async def _addon_call(self, method, params, **kwargs):
        if method == "messages.batchPreview":
            return {
                "items": [
                    {"reference": reference(), "action": "mark", "previous": {"read": False}}
                ],
                "preview": [{"subject": "private subject", "author": "private@example.invalid"}],
                "invalid": [],
            }
        return {"reference": reference(), "body": "private body", "revision": "r1"}

    async def _guarded_call(self, method, params, **kwargs):
        self.calls.append((method, params))
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        return dict(self.result)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["send", "reply", "forward"])
@pytest.mark.parametrize("mode", ["send", "later"])
async def test_old_addon_unkeyed_delivery_remains_journalled(tmp_path, kind, mode):
    daemon = DaemonDouble()
    daemon.session.hello = {}
    daemon.session.capabilities = {"methods": [f"compose.{kind}"]}
    daemon.result = {"messageId": 11, "mode": "sendNow" if mode == "send" else "sendLater"}
    ops = Operations(daemon, tmp_path / "j.sqlite")
    result = await ops.submit({"kind": kind, "params": {"mode": mode}})
    assert result["status"] == "succeeded"
    assert result["sent"] is (mode == "send")
    assert result["queued"] is (mode == "later")
    assert ops.journal.operation(result["operationId"])["status"] == "succeeded"
    assert len(daemon.calls) == 1


@pytest.mark.anyio
async def test_old_addon_keyed_delivery_requires_new_capability(tmp_path):
    daemon = DaemonDouble()
    daemon.session.hello = {}
    daemon.session.capabilities = {"methods": ["compose.send"]}
    ops = Operations(daemon, tmp_path / "j.sqlite")
    with pytest.raises(BlockedError) as caught:
        await ops.submit({"kind": "send", "requestKey": "stable", "params": {"mode": "send"}})
    assert caught.value.code == "UNSUPPORTED_CAPABILITY"
    assert not daemon.calls


@pytest.mark.anyio
async def test_old_addon_unknown_unkeyed_send_never_reconciles_by_replay(tmp_path):
    daemon = DaemonDouble()
    daemon.session.hello = {}
    daemon.session.capabilities = {"methods": ["compose.send"]}
    daemon.error = TransportError("reply lost")
    ops = Operations(daemon, tmp_path / "j.sqlite")
    result = await ops.submit({"kind": "send", "params": {"mode": "send"}})
    result = await ops.call("status", {"operationId": result["operationId"]})
    assert result["status"] == "unknown" and "sent" not in result
    assert len(daemon.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["plan", "apply", "undo"])
@pytest.mark.parametrize("missing", ["method", "experiment"])
async def test_moves_require_native_support_before_starting(tmp_path, stage, missing):
    daemon = DaemonDouble()
    daemon.session.capabilities["experiment"] = missing != "experiment"
    if missing != "method":
        daemon.session.capabilities["methods"].append("x.batch.move")
    ops = Operations(daemon, tmp_path / "j.sqlite")
    item = {"action": "move", "reference": reference()}
    if stage == "plan":
        params = {"kind": "batch", "params": {"action": "move", "messageIds": [1]}}
    elif stage == "apply":
        plan = ops.journal.plan("batch", {"items": [item]})
        params = {"planId": plan}
    else:
        ident, _ = ops.journal.create("old", "batch", "hash", [item])
        ops.journal.item_status(ident, 0, "succeeded", {"reference": reference()})
        ops.journal.set_status(ident, "succeeded")
        params = {"operationId": ident}
    with pytest.raises(BlockedError) as caught:
        await ops.call(stage, params)
    assert caught.value.code == "UNSUPPORTED_CAPABILITY"
    assert not daemon.calls and not ops.tasks
    count = ops.journal.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
    assert count == (1 if stage == "undo" else 0)


def test_restart_and_retention(tmp_path):
    now = [1000000.0]
    path = tmp_path / "operations.sqlite"
    j = Journal(path, now=lambda: now[0])
    ident, _ = j.create("key", "send", "hash", [{}])
    j.set_status(ident, "running")
    j.item_status(ident, 0, "running")
    j.db.close()
    j = Journal(path, now=lambda: now[0])
    assert j.operation(ident)["status"] == "unknown"
    assert j.items(ident)[0]["status"] == "unknown"
    now[0] += RETENTION + 1
    with pytest.raises(UsageError, match="expired"):
        j.operation(ident)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["draft_update", "draft_send"])
@pytest.mark.parametrize("advertised", [False, None])
async def test_unreliable_editor_refused_before_operations_or_writes(tmp_path, kind, advertised):
    daemon = DaemonDouble()
    daemon.session.capabilities["features"] = {"draftEditNative": advertised}
    probes = []

    async def probe(method, _params, **_kwargs):
        probes.append(method)
        return {"draftEditorSupported": False}

    daemon._addon_call = probe
    ops = Operations(daemon, tmp_path / "j.sqlite")
    with pytest.raises(BlockedError) as caught:
        await ops.submit({"kind": kind, "requestKey": "k", "params": {}})
    assert caught.value.code == "UNSUPPORTED_CAPABILITY"
    assert probes == (["x.admin.appInfo"] if advertised is None else [])
    assert not daemon.calls and not ops.tasks
    assert ops.list()["count"] == 0


@pytest.mark.anyio
async def test_keyed_default_draft_creation_replays_the_same_operation(tmp_path):
    daemon = DaemonDouble()
    daemon.result = {"messageId": 7, "mode": "draft"}
    ops = Operations(daemon, tmp_path / "j.sqlite")
    args = {"kind": "send", "requestKey": "review", "params": {"mode": "draft"}}
    first = await ops.submit(args)
    second = await ops.submit(args)
    assert first["operationId"] == second["operationId"]
    assert second["saved"] and not second["sent"] and not second["queued"]
    assert len(daemon.calls) == 1


def test_key_collision_and_schema(tmp_path):
    j = Journal(tmp_path / "j.sqlite")
    ident, fresh = j.create("key", "batch", "hash", [{}])
    assert fresh
    assert j.create("key", "batch", "hash", [{}]) == (ident, False)
    with pytest.raises(UsageError, match="different content"):
        j.create("key", "batch", "different", [{}])
    j.db.execute("PRAGMA user_version=99")
    j.db.close()
    with pytest.raises(BlockedError, match="newer schema"):
        Journal(tmp_path / "j.sqlite")


def test_reference_reconnect_is_same_handle(tmp_path):
    j = Journal(tmp_path / "j.sqlite")
    first = j.remember(reference())
    assert j.remember(reference("two", 10)) == first
    assert j.resolve(first)["messageId"] == 10


@pytest.mark.anyio
async def test_plan_freezes_selection_does_not_persist_preview(tmp_path):
    daemon = DaemonDouble()
    ops = Operations(daemon, tmp_path / "j.sqlite")
    plan = await ops.call("plan", {"kind": "batch", "params": {"messageIds": [1]}})
    assert plan["preview"][0]["subject"] == "private subject"
    first = await ops.call("apply", {"planId": plan["planId"]})
    second = await ops.call("apply", {"planId": plan["planId"]})
    assert first["operationId"] == second["operationId"]
    await asyncio.gather(*list(ops.tasks.values()))
    assert len(daemon.calls) == 1
    assert ops.status(first["operationId"])["status"] == "succeeded"
    raw = (tmp_path / "j.sqlite").read_bytes()
    assert b"private subject" not in raw and b"private@example.invalid" not in raw


@pytest.mark.anyio
async def test_unknown_stops_remaining_items(tmp_path):
    daemon = DaemonDouble()
    daemon.result = {"status": "unknown", "code": "OUTCOME_UNKNOWN"}
    ops = Operations(daemon, tmp_path / "j.sqlite")
    plan_id = ops.journal.plan("batch", {"items": [{"id": 1}, {"id": 2}]})
    op = await ops.call("apply", {"planId": plan_id})
    await asyncio.gather(*list(ops.tasks.values()))
    status = ops.status(op["operationId"])
    assert status["status"] == "unknown"
    assert status["counts"] == {"unknown": 1, "interrupted": 1}
    assert len(daemon.calls) == 1


@pytest.mark.anyio
async def test_cancel_waits_for_current_step(tmp_path):
    daemon = DaemonDouble()
    daemon.gate = asyncio.Event()
    ops = Operations(daemon, tmp_path / "j.sqlite")
    plan_id = ops.journal.plan("batch", {"items": [{"id": 1}, {"id": 2}]})
    op = await ops.call("apply", {"planId": plan_id})
    await asyncio.sleep(0)
    await ops.call("cancel", {"operationId": op["operationId"]})
    daemon.gate.set()
    await asyncio.gather(*list(ops.tasks.values()))
    assert ops.status(op["operationId"])["counts"] == {"succeeded": 1, "cancelled": 1}
    assert len(daemon.calls) == 1


@pytest.mark.anyio
async def test_send_replay_no_body_or_addresses_on_disk(tmp_path):
    daemon = DaemonDouble()
    daemon.result = {"status": "succeeded", "sent": True, "body": "secret result"}
    ops = Operations(daemon, tmp_path / "j.sqlite")
    args = {
        "kind": "send",
        "requestKey": "user-private@example.invalid",
        "params": {"body": "SECRET-BODY", "to": ["secret@example.invalid"], "mode": "send"},
    }
    one = await ops.submit(args)
    two = await ops.submit(args)
    assert one["operationId"] == two["operationId"]
    assert len(daemon.calls) == 1
    raw = (tmp_path / "j.sqlite").read_bytes()
    for private in (
        b"SECRET-BODY",
        b"secret@example.invalid",
        b"secret result",
        b"user-private@example.invalid",
    ):
        assert private not in raw
    with pytest.raises(UsageError, match="different content"):
        await ops.submit({**args, "params": {"body": "changed"}})


@pytest.mark.anyio
async def test_unknown_send_blocks_new_key_after_reference_refresh(tmp_path):
    daemon = DaemonDouble()
    daemon.error = TransportError("lost", code="OUTCOME_UNKNOWN")
    ops = Operations(daemon, tmp_path / "j.sqlite")
    handle = ops.journal.remember(reference())
    args = {
        "kind": "draft_send",
        "requestKey": "one",
        "params": {"draftRef": handle, "expectedRevision": "r1"},
    }
    result = await ops.submit(args)
    assert result["status"] == "unknown"
    refreshed = ops.journal.remember(reference("two", 99))
    with pytest.raises(BlockedError, match="uncertain send"):
        await ops.submit(
            {
                **args,
                "requestKey": "two",
                "params": {"draftRef": refreshed, "expectedRevision": "r1"},
            }
        )
    assert len(daemon.calls) == 1


@pytest.mark.anyio
async def test_undo_uses_only_successes_and_is_singleton(tmp_path):
    daemon = DaemonDouble()
    ops = Operations(daemon, tmp_path / "j.sqlite")
    source, _ = ops.journal.create("source", "batch", digest([1, 2]), [{"n": 1}, {"n": 2}])
    ops.journal.item_status(source, 0, "succeeded", {"reference": reference(), "changed": True})
    ops.journal.item_status(source, 1, "conflict")
    ops.journal.set_status(source, "partial")
    first = await ops.call("undo", {"operationId": source})
    await asyncio.gather(*list(ops.tasks.values()))
    second = await ops.call("undo", {"operationId": source})
    assert first["operationId"] == second["operationId"]
    assert daemon.calls[0][0] == "messages.batchUndo"
    assert daemon.calls[0][1]["previousResult"]["changed"] is True
    assert len(daemon.calls) == 1


def test_expired_invalid_and_foreign_plans(tmp_path):
    now = [1000000.0]
    j = Journal(tmp_path / "j.sqlite", now=lambda: now[0])
    invalid = j.plan("batch", {"items": [], "invalid": [{"code": "NO_ID"}]})
    with pytest.raises(UsageError, match="ineligible"):
        j.get_plan(invalid)
    good = j.plan("batch", {"items": [{}]})
    now[0] += 901
    with pytest.raises(UsageError, match="expired"):
        j.get_plan(good)
    with pytest.raises(UsageError, match="not found"):
        j.get_plan("another-profile")


@pytest.mark.parametrize(
    "method,params,expected",
    [
        ("messages.read", {}, True),
        ("messages.query", {"cursor": "one-shot"}, False),
        ("messages.move", {}, False),
        ("compose.send", {}, False),
        ("future.unknown", {}, False),
        ("daemon.operations.submit", {"requestKey": "stable"}, True),
        ("daemon.operations.submit", {}, False),
        ("search.global", {"cursor": "snapshot"}, True),
    ],
)
def test_retry_classification(method, params, expected):
    assert replay_safe(method, params) is expected


@pytest.mark.anyio
async def test_submit_and_undo_replay_work_without_thunderbird(tmp_path):
    daemon = DaemonDouble()
    ops = Operations(daemon, tmp_path / "j.sqlite")
    args = {"kind": "send", "requestKey": "known", "params": {"body": "private"}}
    sent = await ops.submit(args)
    source, _ = ops.journal.create("source", "batch", "hash", [{}])
    ops.journal.item_status(source, 0, "succeeded", {"reference": reference()})
    ops.journal.set_status(source, "succeeded")
    undone = await ops.call("undo", {"operationId": source})
    await asyncio.gather(*list(ops.tasks.values()))
    daemon.session = None
    assert (await ops.submit(args))["operationId"] == sent["operationId"]
    assert (await ops.call("undo", {"operationId": source}))["operationId"] == undone["operationId"]
    assert len(daemon.calls) == 2


def test_permissions_failure_does_not_create_database(tmp_path, monkeypatch):
    from tbmcp import operations

    monkeypatch.setattr(operations.sys, "platform", "win32")

    def deny(paths):
        raise PermissionError("cannot restrict ACL")

    monkeypatch.setattr(operations, "_windows_private_paths", deny)
    path = tmp_path / "private" / "j.sqlite"
    with pytest.raises(BlockedError, match="restrict operation history"):
        Journal(path)
    assert not path.exists()


@pytest.mark.anyio
async def test_attachment_hash_is_durable_before_save_and_can_reconcile(tmp_path):
    daemon = DaemonDouble()
    ops = Operations(daemon, tmp_path / "j.sqlite")
    plan = ops.journal.plan(
        "attachments", {"items": [{"reference": reference(), "path": "file.pdf"}]}
    )
    prepared = False

    async def read(method, params, **kwargs):
        nonlocal prepared
        if method == "messages.attachmentsPrepare":
            prepared = True
            return {"item": {**params["item"], "expectedSha256": "1234", "expectedBytes": 50}}
        if method == "operations.receipt":
            return {"found": False}
        if method == "messages.attachmentsReconcile":
            return {"status": "succeeded", "saved": True, "bytes": 50, "sha256": "1234"}
        raise AssertionError(method)

    async def write(method, params, **kwargs):
        assert prepared
        task = ops.journal.items(params["operationId"])[0]["task"]
        assert task["expectedSha256"] == "1234" and task["expectedBytes"] == 50
        daemon.calls.append((method, params))
        return {"status": "unknown"}

    daemon._addon_call = read
    daemon._guarded_call = write
    op = await ops.call("apply", {"planId": plan})
    await asyncio.gather(*list(ops.tasks.values()))
    assert ops.status(op["operationId"])["status"] == "unknown"
    reconciled = await ops.call("status", {"operationId": op["operationId"]})
    assert reconciled["status"] == "succeeded"
    assert reconciled["items"][0]["sha256"] == "1234"
    assert len(daemon.calls) == 1


@pytest.mark.anyio
async def test_attachment_preparation_failure_has_no_write(tmp_path):
    daemon = DaemonDouble()
    ops = Operations(daemon, tmp_path / "j.sqlite")
    plan = ops.journal.plan("attachments", {"items": [{"reference": reference()}]})

    async def read(method, params, **kwargs):
        raise TransportError("read interrupted")

    daemon._addon_call = read
    op = await ops.call("apply", {"planId": plan})
    await asyncio.gather(*list(ops.tasks.values()))
    assert ops.status(op["operationId"])["status"] == "failed"
    assert not daemon.calls


@pytest.mark.anyio
async def test_late_send_receipt_updates_history_without_resending(tmp_path):
    daemon = DaemonDouble()
    daemon.error = TransportError("lost", code="OUTCOME_UNKNOWN")
    ops = Operations(daemon, tmp_path / "j.sqlite")
    sent = await ops.submit({"kind": "send", "requestKey": "one", "params": {"body": "private"}})
    assert sent["status"] == "unknown"

    async def read(method, params, **kwargs):
        assert method == "operations.receipt"
        return {"found": True, "pending": False, "result": {"status": "succeeded", "sent": True}}

    daemon._addon_call = read
    status = await ops.call("status", {"operationId": sent["operationId"]})
    assert status["status"] == "succeeded" and status["sent"]
    assert len(daemon.calls) == 1


@pytest.mark.anyio
async def test_reconciled_first_item_does_not_claim_unstarted_remainder_succeeded(tmp_path):
    daemon = DaemonDouble()
    daemon.result = {"status": "unknown"}
    ops = Operations(daemon, tmp_path / "j.sqlite")
    plan = ops.journal.plan("batch", {"items": [{"n": 1}, {"n": 2}]})
    op = await ops.call("apply", {"planId": plan})
    await asyncio.gather(*list(ops.tasks.values()))

    async def read(method, params, **kwargs):
        return {"found": True, "pending": False, "result": {"status": "succeeded"}}

    daemon._addon_call = read
    status = await ops.call("status", {"operationId": op["operationId"]})
    assert status["status"] == "interrupted"
    assert status["counts"] == {"succeeded": 1, "interrupted": 1}


def test_boolean_tag_keys_are_not_mistaken_for_message_content(tmp_path):
    j = Journal(tmp_path / "j.sqlite")
    task = {
        "previous": {"tags": {"body": False, "subject": True}},
        "desired": {"tags": {"body": True, "subject": False}},
        "body": "PRIVATE",
    }
    plan = j.plan("batch", {"items": [task]})
    stored = j.get_plan(plan)["items"][0]
    assert stored["previous"]["tags"] == {"body": False, "subject": True}
    assert stored["desired"]["tags"] == {"body": True, "subject": False}
    assert "body" not in stored
    assert b"PRIVATE" not in (tmp_path / "j.sqlite").read_bytes()


@pytest.mark.anyio
async def test_failed_draft_update_reports_recovery_window_without_raw_message(tmp_path):
    daemon = DaemonDouble()
    daemon.error = ThunderbirdError(
        "private error text",
        code="DRAFT_UPDATE_FAILED",
        recoveryWindowOpen=True,
        tabId=42,
        body="SECRET",
    )
    ops = Operations(daemon, tmp_path / "j.sqlite")
    handle = ops.journal.remember(reference())
    result = await ops.submit(
        {
            "kind": "draft_update",
            "requestKey": "recover",
            "params": {"draftRef": handle, "expectedRevision": "r1"},
        }
    )
    assert result["recoveryWindowOpen"] is True
    assert result["tabId"] == 42
    raw = (tmp_path / "j.sqlite").read_bytes()
    assert b"private error text" not in raw and b"SECRET" not in raw


@pytest.mark.anyio
async def test_failed_receipt_persistence_latches_health_and_does_not_resend(tmp_path, monkeypatch):
    daemon = DaemonDouble()
    ops = Operations(daemon, tmp_path / "j.sqlite")
    real_status = ops.journal.item_status

    def fail_success(ident, number, status, result=None):
        if status == "succeeded":
            raise sqlite3.OperationalError("disk full PRIVATE@example.invalid")
        return real_status(ident, number, status, result)

    monkeypatch.setattr(ops.journal, "item_status", fail_success)
    args = {"kind": "send", "requestKey": "one", "params": {"body": "PRIVATE BODY"}}
    result = await ops.submit(args)
    assert result["status"] == "unknown"
    assert result["counts"] == {"unknown": 1}
    assert ops.health_error["code"] == "JOURNAL_UNHEALTHY"
    assert "PRIVATE" not in str(ops.health_error)
    assert (await ops.submit(args))["operationId"] == result["operationId"]
    with pytest.raises(BlockedError, match="No further writes"):
        await ops.submit({**args, "requestKey": "two"})
    assert len(daemon.calls) == 1
    assert b"PRIVATE" not in (tmp_path / "j.sqlite").read_bytes()


@pytest.mark.anyio
async def test_batch_receipt_failure_stops_unstarted_items(tmp_path, monkeypatch):
    daemon = DaemonDouble()
    ops = Operations(daemon, tmp_path / "j.sqlite")
    plan = ops.journal.plan("batch", {"items": [{"n": 1}, {"n": 2}]})
    real_status = ops.journal.item_status

    def fail_success(ident, number, status, result=None):
        if status == "succeeded":
            raise sqlite3.OperationalError("disk unavailable")
        return real_status(ident, number, status, result)

    monkeypatch.setattr(ops.journal, "item_status", fail_success)
    result = await ops.call("apply", {"planId": plan})
    await asyncio.gather(*list(ops.tasks.values()))
    status = ops.status(result["operationId"])
    assert status["counts"] == {"unknown": 1, "interrupted": 1}
    assert ops.health_error["code"] == "JOURNAL_UNHEALTHY"
    assert not ops.busy and len(daemon.calls) == 1


@pytest.mark.anyio
async def test_health_latches_even_when_uncertainty_marker_cannot_be_written(tmp_path, monkeypatch):
    daemon = DaemonDouble()
    ops = Operations(daemon, tmp_path / "j.sqlite")

    async def write(method, params, **kwargs):
        daemon.calls.append((method, params))

        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("persistent disk failure")

        monkeypatch.setattr(ops.journal, "item_status", fail)
        return {"status": "succeeded"}

    daemon._guarded_call = write
    result = await ops.submit({"kind": "send", "requestKey": "one", "params": {}})
    assert result["status"] == "running"  # The last durable state, not a false success.
    assert ops.health_error["code"] == "JOURNAL_UNHEALTHY"
    assert not ops.busy and len(daemon.calls) == 1
    with pytest.raises(BlockedError, match="No further writes"):
        await ops.submit({"kind": "send", "requestKey": "two", "params": {}})

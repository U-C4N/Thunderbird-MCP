"""Exercise the real daemon write barrier, including native work after a reply."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from tbmcp.daemon import AddonSession, Daemon, _profile_journal_id, error_payload
from tbmcp.errors import BlockedError, ThunderbirdError, TimeoutError_, TransportError
from tbmcp.profile import ThunderbirdProfile

pytestmark = pytest.mark.anyio


class Session:
    def __init__(self, epoch="epoch-a"):
        self.hello = {"runtimeEpoch": epoch}
        self.capabilities = {"methods": ["operations.receipt", "x.admin.appInfo"]}
        self._closed = asyncio.Event()
        self._next_id = 0
        self.uncertain = set()
        self.late_outcomes = {}
        self.calls = []
        self.result = {"status": "succeeded"}
        self.error = None
        self.receipt = {"found": False}
        self.started = (time.time() - 3600) * 1000

    async def call(self, method, params, **kwargs):
        self._next_id += 1
        self.calls.append((method, params))
        if method == "operations.receipt":
            return self.receipt
        if method == "x.admin.appInfo":
            return {"processStartedAt": self.started}
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("TBMCP_STATE_DIR", str(tmp_path / "state"))
    value = Daemon(ThunderbirdProfile(tmp_path / "profile", "test", False, tmp_path))
    value.session = Session()
    yield value
    if value._operations:
        value._operations.journal.db.close()


async def test_handler_unknown_does_not_unlock_on_completed_receipt(daemon):
    daemon.session.result = {"status": "unknown"}
    await daemon._guarded_call("messages.batchStep", {"operationId": "op", "itemId": 0})
    daemon.session.receipt = {"found": True, "pending": False, "result": {"status": "unknown"}}
    assert daemon.operations().journal.barrier()["needsRestart"]
    with pytest.raises(BlockedError, match="previous write"):
        await daemon._guarded_call("messages.mark", {})
    assert len([call for call in daemon.session.calls if call[0] == "messages.mark"]) == 0


async def test_unknown_draft_keeps_recovery_window_metadata(daemon):
    daemon.session.error = ThunderbirdError(
        "save uncertain",
        code="OUTCOME_UNKNOWN",
        recoveryWindowOpen=True,
        tabId=42,
        body="must not be propagated",
    )
    with pytest.raises(TransportError) as caught:
        await daemon._guarded_call("compose.draftUpdate", {})
    assert caught.value.extra == {"recoveryWindowOpen": True, "tabId": 42}
    assert error_payload(caught.value)["recoveryWindowOpen"] is True


async def test_filter_background_completion_blocks_next_mutation(daemon):
    daemon.session.result = {"completed": False}
    result = await daemon._guarded_call("x.filters.run", {})
    assert result["status"] == "unknown"
    assert daemon.operations().journal.barrier()["needsRestart"]
    with pytest.raises(BlockedError):
        await daemon._guarded_call("messages.mark", {})


async def test_native_timeout_requires_process_restart_not_addon_reload(daemon):
    daemon.session.error = ThunderbirdError("native still running", code="NATIVE_TIMEOUT")
    with pytest.raises(TransportError, match="Native work"):
        await daemon._guarded_call("x.calendar.createEvent", {})
    daemon.session = Session("epoch-b")
    with pytest.raises(BlockedError):
        await daemon._guarded_call("messages.mark", {})
    daemon.session.started = (time.time() + 1) * 1000
    assert await daemon._guarded_call("messages.mark", {}) == {"status": "succeeded"}
    assert daemon.operations().journal.barrier() is None


async def test_old_addon_barrier_recovers_after_upgrade_and_real_restart(daemon):
    daemon.session = Session(None)
    daemon.session.error = TransportError("disconnected")
    with pytest.raises(TransportError):
        await daemon._guarded_call("messages.move", {})
    daemon.session = Session("new-capable-runtime")
    with pytest.raises(BlockedError, match=r"install the 1\.4\.0"):
        await daemon._guarded_call("messages.mark", {})
    daemon.session.started = (time.time() + 1) * 1000
    await daemon._guarded_call("messages.mark", {})
    assert daemon.operations().journal.barrier() is None


async def test_same_epoch_reconnect_uses_receipt_not_socket_replacement(daemon):
    original = daemon.session
    original._closed.set()
    original.error = TransportError("lost")
    with pytest.raises(TransportError):
        await daemon._guarded_call("messages.batchStep", {"operationId": "op", "itemId": 0})
    daemon.session = Session("epoch-a")
    daemon.session.receipt = {"found": True, "pending": True}
    with pytest.raises(BlockedError):
        await daemon._guarded_call("messages.mark", {})
    daemon.session.receipt = {"found": True, "pending": False, "result": {"status": "succeeded"}}
    await daemon._guarded_call("messages.mark", {})
    assert daemon.operations().journal.barrier() is None


async def test_late_native_unknown_is_not_treated_as_finished_mutation(daemon):
    future = asyncio.get_running_loop().create_future()
    daemon.session.uncertain.add(future)
    daemon.session.error = TimeoutError_("messages.batchStep", 1)
    with pytest.raises(TransportError):
        await daemon._guarded_call("messages.batchStep", {"operationId": "op"})
    barrier = daemon.operations().journal.barrier()
    daemon.session.uncertain.clear()
    daemon.session.late_outcomes[barrier["requestId"]] = {"result": {"status": "unknown"}}
    daemon.session.error = None
    with pytest.raises(BlockedError):
        await daemon._guarded_call("messages.mark", {})
    assert daemon.operations().journal.barrier()["needsRestart"]


async def test_late_known_result_releases_legacy_write_barrier(daemon):
    future = asyncio.get_running_loop().create_future()
    daemon.session.uncertain.add(future)
    daemon.session.error = TimeoutError_("messages.mark", 1)
    with pytest.raises(TransportError):
        await daemon._guarded_call("messages.mark", {})
    barrier = daemon.operations().journal.barrier()
    daemon.session.uncertain.clear()
    daemon.session.late_outcomes[barrier["requestId"]] = {"result": {"updated": 1}}
    daemon.session.error = None
    await daemon._guarded_call("messages.mark", {})
    assert daemon.operations().journal.barrier() is None


async def test_reads_bypass_write_lock_and_restart_remains_available(daemon):
    await daemon._write_lock.acquire()
    try:
        result = await asyncio.wait_for(
            daemon._invoke("messages.read", {}, timeout=1, on_progress=None), 0.2
        )
        assert result["status"] == "succeeded"
        await asyncio.wait_for(
            daemon._invoke("x.admin.restart", {}, timeout=1, on_progress=None), 0.2
        )
    finally:
        daemon._write_lock.release()


async def test_session_timeout_keeps_late_result_and_cancellation_tracking():
    class Socket:
        async def send(self, value):
            pass

    session = AddonSession(Socket(), {})
    with pytest.raises(TimeoutError_):
        await session.call("messages.mark", {}, timeout=0.001)
    assert len(session.uncertain) == 1
    await session._handle(
        {"t": "res", "id": 1, "ok": True, "result": {"updated": 1}}, lambda _: None
    )
    await asyncio.sleep(0)
    assert not session.uncertain
    assert session.late_outcomes[1]["result"] == {"updated": 1}
    task = asyncio.create_task(session.call("messages.mark", {}, timeout=30))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(session.uncertain) == 1
    await session._handle(
        {"t": "res", "id": 2, "ok": True, "result": {"updated": 1}}, lambda _: None
    )
    await asyncio.sleep(0)
    assert not session.uncertain


def test_profile_paths_only_fold_case_on_windows(monkeypatch):
    from tbmcp import daemon as module

    left = SimpleNamespace(resolve=lambda: "/profiles/Work")
    right = SimpleNamespace(resolve=lambda: "/profiles/work")
    monkeypatch.setattr(module.sys, "platform", "linux")
    assert _profile_journal_id(left) != _profile_journal_id(right)
    monkeypatch.setattr(module.sys, "platform", "win32")
    assert _profile_journal_id(left) == _profile_journal_id(right)


def test_error_envelope_only_allows_typed_recovery_fields():
    error = ThunderbirdError(
        "failed", code="FAILED", recoveryWindowOpen=True, tabId=7, body="private"
    )
    wire = error_payload(error)
    assert wire["recoveryWindowOpen"] is True and wire["tabId"] == 7
    assert "body" not in wire
    malformed = ThunderbirdError("failed", recoveryWindowOpen="yes", tabId=True)
    assert "recoveryWindowOpen" not in error_payload(malformed)
    assert "tabId" not in error_payload(malformed)

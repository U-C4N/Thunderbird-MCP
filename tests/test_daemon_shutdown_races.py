"""A graceful upgrade cannot race receipt reconciliation or an admitted writer."""

import asyncio
import os

import pytest

from tbmcp import ipc
from tbmcp.daemon import Daemon, run_daemon
from tbmcp.errors import BlockedError
from tbmcp.profile import ThunderbirdProfile

pytestmark = pytest.mark.anyio


class Session:
    def __init__(self):
        self.hello = {"runtimeEpoch": "same-runtime"}
        self._closed = asyncio.Event()
        self._next_id = 0
        self.uncertain = set()
        self.late_outcomes = {}
        self.calls = []
        self.receipt_started = asyncio.Event()
        self.receipt_ready = asyncio.Event()

    async def call(self, method, params, **kwargs):
        self._next_id += 1
        self.calls.append(method)
        if method == "operations.receipt":
            self.receipt_started.set()
            await self.receipt_ready.wait()
            return {"found": True, "pending": False, "result": {"status": "succeeded"}}
        return {"changed": True}


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("TBMCP_STATE_DIR", str(tmp_path / "state"))
    value = Daemon(ThunderbirdProfile(tmp_path / "profile", "test", False, tmp_path))
    value.session = Session()
    yield value
    if value._operations:
        value._operations.journal.db.close()


async def invoke(daemon, method):
    return await daemon._invoke(method, {}, timeout=1, on_progress=None)


async def test_shutdown_receipt_cannot_clear_a_concurrent_writers_barrier(daemon):
    daemon.operations().journal.set_barrier(
        {"epoch": "same-runtime", "method": "messages.batchStep", "operationId": "previous"}
    )
    shutdown = asyncio.create_task(invoke(daemon, "daemon.shutdown"))
    writer = None
    try:
        await asyncio.wait_for(daemon.session.receipt_started.wait(), 1)
        assert daemon._write_lock.locked(), "shutdown must serialize receipt reconciliation"
        writer = asyncio.create_task(invoke(daemon, "messages.mark"))
        await asyncio.sleep(0)
        assert daemon.session.calls == ["operations.receipt"]
        daemon.session.receipt_ready.set()
        assert await shutdown == {"stopping": True}
        with pytest.raises(BlockedError) as caught:
            await writer
        assert caught.value.code == "DAEMON_STOPPING"
        assert daemon.session.calls == ["operations.receipt"]
        assert daemon.operations().journal.barrier() is None
    finally:
        daemon.session.receipt_ready.set()
        for task in [shutdown, writer]:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in [shutdown, writer] if task), return_exceptions=True)


async def test_previously_admitted_writer_rechecks_stop_after_lock_wait(daemon):
    await daemon._write_lock.acquire()
    writer = asyncio.create_task(invoke(daemon, "messages.mark"))
    await asyncio.sleep(0)
    assert not writer.done()
    # A graceful shutdown can win between releasing an earlier writer and
    # resuming its lock waiter. The entry-time guard alone is insufficient.
    daemon._stop.set()
    daemon._write_lock.release()
    with pytest.raises(BlockedError) as caught:
        await writer
    assert caught.value.code == "DAEMON_STOPPING"
    assert daemon.session.calls == []


async def test_shutdown_fails_promptly_during_a_running_write(daemon):
    async with daemon._write_lock:
        with pytest.raises(BlockedError) as caught:
            await asyncio.wait_for(invoke(daemon, "daemon.shutdown"), 0.2)
        assert caught.value.code == "OPERATIONS_ACTIVE"
    assert not daemon._stop.is_set()


async def test_two_forced_upgrades_retire_and_publish_only_once(daemon, monkeypatch):
    old = ipc.DaemonInfo(
        version=ipc.PROTOCOL_VERSION,
        port=12345,
        token="old-owner",
        pid=os.getpid(),
        profile=str(daemon.profile.path),
    )
    old.write()
    retirement_started = asyncio.Event()
    allow_retirement = asyncio.Event()
    retired = []
    started = []

    async def retire(info):
        retired.append(info.port)
        retirement_started.set()
        await allow_retirement.wait()
        ipc.DaemonInfo.clear()
        return True

    async def run(instance):
        started.append(instance)
        assert instance.startup_lock is not None
        assert instance.startup_lock.holder() == os.getpid()
        ipc.DaemonInfo(
            version=ipc.PROTOCOL_VERSION,
            port=23456,
            token="new-owner",
            pid=os.getpid(),
            profile=str(instance.profile.path),
        ).write()
        instance.startup_lock.release()
        instance.startup_lock = None

    monkeypatch.setattr("tbmcp.daemon._retire_existing", retire)
    monkeypatch.setattr("tbmcp.daemon.find_profile", lambda *_: daemon.profile)
    monkeypatch.setattr(Daemon, "run", run)
    first = asyncio.create_task(run_daemon(force=True))
    second = None
    try:
        await asyncio.wait_for(retirement_started.wait(), 1)
        second = asyncio.create_task(run_daemon(force=True))
        assert await asyncio.wait_for(second, 0.5) == 0
        assert retired == [old.port], "second upgrade bypassed startup ownership"
        assert not started
        allow_retirement.set()
        assert await first == 0
        assert len(started) == 1
        assert ipc.DaemonInfo.load().port == 23456
        assert not (ipc.state_dir() / "daemon.lock").exists()
    finally:
        allow_retirement.set()
        for task in [first, second]:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in [first, second] if task), return_exceptions=True)
        ipc.DaemonInfo.clear()

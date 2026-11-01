"""Persisted diagnostics and graceful replacement must not hide active writes."""

import asyncio
import sqlite3

import pytest

from tbmcp import ipc
from tbmcp.daemon import Daemon, _retire_existing, run_daemon
from tbmcp.errors import BlockedError
from tbmcp.profile import ThunderbirdProfile

pytestmark = pytest.mark.anyio


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("TBMCP_STATE_DIR", str(tmp_path / "state"))
    value = Daemon(ThunderbirdProfile(tmp_path / "profile", "test", False, tmp_path))
    yield value
    if value._operations:
        value._operations.journal.db.close()


def test_status_recovers_persisted_unknown_before_first_tool(daemon):
    journal = daemon.operations().journal
    op, _ = journal.create("key", "send", "hash", [{}])
    with journal.db:
        journal.db.execute("UPDATE operations SET status='running' WHERE id=?", (op,))
        journal.db.execute("UPDATE items SET status='running' WHERE operation_id=?", (op,))
    journal.set_barrier({"epoch": "old", "operationId": op})
    journal.db.close()
    daemon._operations = None
    health = daemon.status()["operationJournal"]
    assert health["available"] and health["writeBlocked"]
    assert health["counts"] == {"unknown": 1}


def test_read_only_status_does_not_create_journal(daemon):
    assert daemon.status()["operationJournal"]["available"]
    assert not daemon._operation_path().exists()


@pytest.mark.parametrize("content", [b"not sqlite", None])
def test_diagnostics_survive_broken_or_future_schema(daemon, content):
    path = daemon._operation_path()
    path.parent.mkdir(parents=True)
    if content is not None:
        path.write_bytes(content)
    else:
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA user_version=999")
    health = daemon.status()["operationJournal"]
    assert not health["available"] and health["writeBlocked"]
    assert health["error"]["message"]


async def test_shutdown_refuses_running_unjournaled_write(daemon):
    async with daemon._write_lock:
        with pytest.raises(BlockedError, match="active or unresolved"):
            await daemon._invoke("daemon.shutdown", {}, timeout=1, on_progress=None)
    assert not daemon._stop.is_set()


async def test_shutdown_refuses_persisted_unknown_write(daemon):
    daemon.operations().journal.set_barrier({"epoch": "old"})
    with pytest.raises(BlockedError):
        await daemon._invoke("daemon.shutdown", {}, timeout=1, on_progress=None)
    assert not daemon._stop.is_set()


async def test_shutdown_acceptance_prevents_new_write(daemon):
    await daemon._invoke("daemon.shutdown", {}, timeout=1, on_progress=None)
    with pytest.raises(BlockedError, match="shutting down"):
        await daemon._invoke("messages.mark", {}, timeout=1, on_progress=None)


async def test_worker_health_latch_allows_restart_after_native_work_settled(daemon):
    daemon.operations().health_error = {"code": "JOURNAL_UNHEALTHY", "message": "worker failed"}
    assert daemon.status()["operationJournal"]["writeBlocked"]
    await daemon._invoke("daemon.shutdown", {}, timeout=1, on_progress=None)
    assert daemon._stop.is_set()


async def test_force_takeover_preserves_busy_owner(daemon):
    daemon.operations().journal.set_barrier({"epoch": "uncertain"})
    async with await asyncio.start_server(daemon._serve_control, "127.0.0.1", 0) as control:
        info = ipc.DaemonInfo(
            version=ipc.PROTOCOL_VERSION,
            port=control.sockets[0].getsockname()[1],
            token=daemon.control_token,
            pid=__import__("os").getpid(),
            profile=str(daemon.profile.path),
        )
        info.write()
        assert await run_daemon(force=True) == 2
        assert ipc.DaemonInfo.load().port == info.port
        assert not daemon._stop.is_set()
        ipc.DaemonInfo.clear()


async def test_force_does_not_guess_that_an_old_daemon_is_safe(daemon, monkeypatch):
    # Even an idle status reply from an old daemon cannot exclude a new write
    # racing its unguarded shutdown implementation.
    monkeypatch.setattr(
        daemon, "status", lambda: {"daemon": {"clients": 1}, "thunderbird": {"inFlight": 0}}
    )
    async with await asyncio.start_server(daemon._serve_control, "127.0.0.1", 0) as control:
        info = ipc.DaemonInfo(
            version=ipc.PROTOCOL_VERSION,
            port=control.sockets[0].getsockname()[1],
            token=daemon.control_token,
            pid=__import__("os").getpid(),
            profile=str(daemon.profile.path),
        )
        info.write()
        assert await run_daemon(force=True) == 2
        assert not daemon._stop.is_set()
        assert ipc.DaemonInfo.load().port == info.port
        ipc.DaemonInfo.clear()


async def test_idle_owner_acknowledges_retirement_and_removes_advertisement(daemon):
    daemon.profile.path.mkdir()
    server = asyncio.create_task(daemon.run())
    try:
        async with asyncio.timeout(5):
            while not (owner := ipc.DaemonInfo.load()):
                await asyncio.sleep(0.01)
        assert await _retire_existing(owner)
        await asyncio.wait_for(server, timeout=5)
        assert ipc.DaemonInfo.load() is None
    finally:
        daemon._stop.set()
        await asyncio.gather(server, return_exceptions=True)

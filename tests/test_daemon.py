"""Daemon lifecycle rules.

Every case here comes from something that actually went wrong during development, and
each one presented as "Thunderbird is not connected" with every process looking
healthy — which is why they are pinned down as tests rather than left to judgement:

- a second daemon binding its own sockets while the add-on stayed attached to the
  first, so `serve` and the add-on were talking to different processes;
- a live process holding the start-up lock without ever advertising, which made every
  later daemon stand down and left the bridge down until that process died.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time

import pytest
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from tbmcp import ipc
from tbmcp.daemon import Daemon, run_daemon
from tbmcp.errors import NotConnectedError
from tbmcp.profile import ThunderbirdProfile

pytestmark = pytest.mark.anyio


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    ipc.DaemonInfo.clear()
    yield tmp_path
    ipc.DaemonInfo.clear()


class TestLock:
    """The lock is the actual singleton guarantee; the advertisement is only a hint."""

    def test_only_one_holder(self, isolated_state) -> None:
        first = ipc.DaemonLock.acquire()
        assert first is not None
        assert first.holder() == os.getpid()
        # A second attempt from a live holder must fail, which is what stops two
        # daemons binding in the same second.
        assert ipc.DaemonLock.acquire() is None
        first.release()
        again = ipc.DaemonLock.acquire()
        assert again is not None
        again.release()

    def test_a_stale_lock_is_reclaimed(self, isolated_state) -> None:
        """A daemon that crashed must not lock the user out for ever."""
        path = ipc.state_dir() / "daemon.lock"
        path.write_text("2147483647", encoding="ascii")  # a pid that cannot be alive
        lock = ipc.DaemonLock.acquire()
        assert lock is not None
        assert lock.holder() == os.getpid()
        lock.release()

    def test_an_unreadable_lock_is_reclaimed(self, isolated_state) -> None:
        path = ipc.state_dir() / "daemon.lock"
        path.write_text("not a pid", encoding="ascii")
        lock = ipc.DaemonLock.acquire()
        assert lock is not None
        lock.release()

    def test_release_does_not_remove_someone_elses_lock(self, isolated_state) -> None:
        path = ipc.state_dir() / "daemon.lock"
        path.write_text("2147483647", encoding="ascii")
        ipc.DaemonLock(path).release()
        assert path.is_file(), "released a lock it did not hold"


async def test_stands_down_when_another_daemon_advertises(isolated_state) -> None:
    held = ipc.DaemonLock.acquire()
    assert held is not None
    # The holder has published, so we are genuinely redundant.
    ipc.DaemonInfo(
        version=ipc.PROTOCOL_VERSION,
        port=12345,
        token="theirs",
        pid=os.getpid(),
        profile="/somewhere",
    ).write()
    try:
        assert await run_daemon() == 0
        still = ipc.DaemonInfo.load()
        assert still is not None and still.port == 12345, "it took over instead of standing down"
    finally:
        held.release()


async def test_a_wedged_lock_holder_does_not_block_startup(isolated_state, monkeypatch) -> None:
    """The failure this guards against: a live process holding the start-up lock but
    never advertising left every later daemon standing down, and the bridge stayed
    down until that process died."""
    held = ipc.DaemonLock.acquire()
    assert held is not None
    assert ipc.DaemonInfo.load() is None, "no advertisement, so the holder is wedged"
    monkeypatch.setattr("tbmcp.daemon.find_profile", lambda *_a, **_k: None)
    monkeypatch.setattr("tbmcp.daemon._await_advertisement", _no_advertisement)
    # Reaching the profile check proves it took the lock over rather than giving up.
    assert await run_daemon() == 2


async def _no_advertisement(*, timeout: float):
    return None


async def test_force_starts_anyway(isolated_state, monkeypatch) -> None:
    """`--force` exists for recovery; the loser then stands down via the watchdog."""
    held = ipc.DaemonLock.acquire()
    assert held is not None
    monkeypatch.setattr("tbmcp.daemon.find_profile", lambda *_a, **_k: None)
    try:
        # Past the singleton check, and stopped at the missing profile.
        assert await run_daemon(force=True) == 2
    finally:
        held.release()


async def test_a_missing_profile_releases_the_lock(isolated_state, monkeypatch) -> None:
    """Otherwise one bad start would block every later one."""
    monkeypatch.setattr("tbmcp.daemon.find_profile", lambda *_a, **_k: None)
    assert await run_daemon() == 2
    lock = ipc.DaemonLock.acquire()
    assert lock is not None, "the lock was not released after a failed start"
    lock.release()


def test_supersede_rule(isolated_state, monkeypatch) -> None:
    """The watchdog's decision: stand down when the advertisement names someone else."""
    # Nothing advertised: keep running.
    assert Daemon.superseded_by() is None

    # We own it: keep running.
    ipc.DaemonInfo(
        version=ipc.PROTOCOL_VERSION, port=1, token="ours", pid=os.getpid(), profile=""
    ).write()
    assert Daemon.superseded_by() is None

    # Someone else owns it: stand down and name the winner.
    monkeypatch.setattr(
        ipc.DaemonInfo,
        "load",
        classmethod(
            lambda cls: ipc.DaemonInfo(
                version=ipc.PROTOCOL_VERSION, port=2, token="theirs", pid=999_001, profile=""
            )
        ),
    )
    assert Daemon.superseded_by() == 999_001


# ------------------------------------------------------- handshake telemetry
#
# The morning this class exists for: thunderbird.exe opened two connections to the
# add-on port every ~25 s for half an hour, each dying ~10 s later with the HTTP
# upgrade never completed. The handler never ran, so nothing was logged, nothing was
# counted, and every reporter could only say "Thunderbird is not connected". These
# tests drive the real `serve()` the daemon runs — same connection class, same
# process_request hook — because the whole point is what happens *before* the
# handler.


def _daemon(path) -> Daemon:
    profile = ThunderbirdProfile(path=path, name="test", is_default=True, root=path)
    return Daemon(profile, idle_timeout=0)


def _hello(token: str, *, protocol: object = ipc.PROTOCOL_VERSION) -> str:
    return json.dumps(
        {
            "t": "hello",
            "token": token,
            "protocol": protocol,
            "addonVersion": "1.3.0",
            "app": {"name": "Thunderbird", "version": "155.0"},
            "capabilities": {"experiment": True, "namespaces": ["messages"]},
        }
    )


async def _settled(daemon: Daemon, predicate, *, timeout: float = 2.0) -> dict:
    """The summary once it says what the test is waiting for.

    Both ends of a close race: the client is told before the server's handler has
    finished unwinding, and a connection that never upgrades is only recorded when
    websockets gives up on it.
    """
    deadline = time.monotonic() + timeout
    while True:
        summary = daemon.handshakes.summary()
        if predicate(summary):
            return summary
        if time.monotonic() >= deadline:
            raise AssertionError(f"handshake summary never settled: {summary}")
        await asyncio.sleep(0.02)


class TestHandshakeTelemetry:
    async def test_a_client_that_never_says_hello_is_recorded(
        self, isolated_state, monkeypatch
    ) -> None:
        monkeypatch.setattr("tbmcp.daemon.HELLO_TIMEOUT", 0.3)
        daemon = _daemon(isolated_state)
        async with daemon._addon_server(open_timeout=0.3) as server:
            port = server.sockets[0].getsockname()[1]
            async with ws_connect(f"ws://127.0.0.1:{port}/tbmcp") as ws:
                with pytest.raises(ConnectionClosed):
                    await asyncio.wait_for(ws.recv(), timeout=5.0)
                assert ws.close_code == 4002

        summary = daemon.status()["handshake"]
        assert summary["lastOutcome"] == "no-hello-timeout"
        assert summary["recentFailures"] == 1

    async def test_a_stale_token_is_recorded_without_being_logged(
        self, isolated_state, caplog
    ) -> None:
        daemon = _daemon(isolated_state)
        with caplog.at_level(logging.DEBUG):
            async with daemon._addon_server(open_timeout=0.3) as server:
                port = server.sockets[0].getsockname()[1]
                async with ws_connect(f"ws://127.0.0.1:{port}/tbmcp") as ws:
                    await ws.send(_hello("not-the-token"))
                    with pytest.raises(ConnectionClosed):
                        await asyncio.wait_for(ws.recv(), timeout=5.0)
                    assert ws.close_code == 4001

        summary = daemon.handshakes.summary()
        assert summary["lastOutcome"] == "bad-token"
        assert daemon.addon_token not in caplog.text, "a token must never reach a log"

    async def test_a_non_numeric_protocol_is_a_mismatch_not_a_crash(
        self, isolated_state, caplog
    ) -> None:
        """`int(hello["protocol"])` on a string raised straight out of the handler:
        websockets logged a traceback and the add-on learnt nothing."""
        daemon = _daemon(isolated_state)
        with caplog.at_level(logging.DEBUG):
            async with daemon._addon_server(open_timeout=0.3) as server:
                port = server.sockets[0].getsockname()[1]
                async with ws_connect(f"ws://127.0.0.1:{port}/tbmcp") as ws:
                    await ws.send(_hello(daemon.addon_token, protocol="x"))
                    with pytest.raises(ConnectionClosed):
                        await asyncio.wait_for(ws.recv(), timeout=5.0)
                    assert ws.close_code == 4002

        assert daemon.handshakes.summary()["lastOutcome"] == "protocol-mismatch"
        assert "Traceback" not in caplog.text

    async def test_a_good_hello_is_welcomed_and_then_disconnects(self, isolated_state) -> None:
        daemon = _daemon(isolated_state)
        async with daemon._addon_server(open_timeout=0.3) as server:
            port = server.sockets[0].getsockname()[1]
            async with ws_connect(f"ws://127.0.0.1:{port}/tbmcp") as ws:
                await ws.send(_hello(daemon.addon_token))
                welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                assert welcome["t"] == "welcome"
                assert daemon.status()["connected"] is True

            summary = await _settled(daemon, lambda s: s["lastOutcome"] == "disconnected")

        assert summary["lastCloseCode"] == 1000
        assert summary["recentFailures"] == 0
        assert summary["recent"][-1]["addonVersion"] == "1.3.0"

    async def test_the_session_that_is_replaced_reads_as_superseded(self, isolated_state) -> None:
        daemon = _daemon(isolated_state)
        async with daemon._addon_server(open_timeout=0.3) as server:
            port = server.sockets[0].getsockname()[1]
            async with ws_connect(f"ws://127.0.0.1:{port}/tbmcp") as first:
                await first.send(_hello(daemon.addon_token))
                await asyncio.wait_for(first.recv(), timeout=5.0)

                async with ws_connect(f"ws://127.0.0.1:{port}/tbmcp") as second:
                    await second.send(_hello(daemon.addon_token))
                    await asyncio.wait_for(second.recv(), timeout=5.0)
                    with pytest.raises(ConnectionClosed):
                        await asyncio.wait_for(first.recv(), timeout=5.0)
                    assert first.close_code == 1012

                    summary = await _settled(
                        daemon, lambda s: s["recent"][0]["outcome"] == "superseded"
                    )

        assert [row["outcome"] for row in summary["recent"]] == ["superseded", "welcomed"]

    async def test_a_connection_that_never_upgrades_is_recorded(self, isolated_state) -> None:
        """The one the handler cannot see: the socket opens, nothing is sent, and
        websockets drops it when `open_timeout` expires without ever calling us."""
        daemon = _daemon(isolated_state)
        async with daemon._addon_server(open_timeout=0.3) as server:
            port = server.sockets[0].getsockname()[1]
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                summary = await _settled(daemon, lambda s: s["lastOutcome"] == "no-upgrade")
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()

        assert summary["recentFailures"] == 1

    async def test_a_failing_handshake_reaches_the_caller_of_a_tool(
        self, isolated_state, monkeypatch
    ) -> None:
        """The payoff: every "not connected" now carries why, instead of leaving the
        model to guess between "start Thunderbird" and a broken add-on."""
        monkeypatch.setattr("tbmcp.daemon.HELLO_TIMEOUT", 0.3)
        daemon = _daemon(isolated_state)
        async with daemon._addon_server(open_timeout=0.3) as server:
            port = server.sockets[0].getsockname()[1]
            async with ws_connect(f"ws://127.0.0.1:{port}/tbmcp") as ws:
                with pytest.raises(ConnectionClosed):
                    await asyncio.wait_for(ws.recv(), timeout=5.0)

            with pytest.raises(NotConnectedError) as caught:
                await daemon._invoke("messages.query", {}, timeout=1, on_progress=None)

        assert "never completed the handshake" in caught.value.message

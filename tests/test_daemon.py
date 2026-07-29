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

import os

import pytest

from tbmcp import ipc
from tbmcp.daemon import run_daemon

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
    from tbmcp.daemon import Daemon

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

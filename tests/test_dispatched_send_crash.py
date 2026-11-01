"""Crash the process after a send crosses the daemon's real dispatch boundary."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tbmcp.daemon import Daemon
from tbmcp.profile import ThunderbirdProfile

CRASH_EXIT = 73
REQUEST = {"kind": "send", "requestKey": "same-key", "params": {"mode": "send"}}


class Addon:
    def __init__(self, marker, *, crash=False):
        self.hello = {"runtimeEpoch": "live-addon"}
        self.capabilities = {"methods": ["compose.send", "operations.receipt"]}
        self.marker = marker
        self.crash = crash
        self.calls = []

    async def call(self, method, params, **kwargs):
        self.calls.append(method)
        if method == "operations.receipt":
            return {"found": False}
        assert method == "compose.send"
        # The external receiver has observed the dispatch, but the daemon has
        # not received or recorded any return value. No orderly worker cleanup.
        with self.marker.open("a", encoding="ascii") as recorded:
            recorded.write(params["operationId"] + "\n")
            recorded.flush()
            os.fsync(recorded.fileno())
        if self.crash:
            os._exit(CRASH_EXIT)
        raise AssertionError("an uncertain send was dispatched again")


def daemon_for(root, *, crash=False):
    daemon = Daemon(ThunderbirdProfile(root / "profile", "fixture", False, root))
    daemon.session = Addon(root / "dispatched.txt", crash=crash)
    return daemon


@pytest.mark.anyio
async def test_process_death_after_send_dispatch_does_not_replay(tmp_path, monkeypatch):
    (tmp_path / "profile").mkdir()
    monkeypatch.setenv("TBMCP_STATE_DIR", str(tmp_path / "state"))
    child = await asyncio.to_thread(
        subprocess.run, [sys.executable, __file__, str(tmp_path)], capture_output=True, timeout=20
    )
    assert child.returncode == CRASH_EXIT, child.stderr.decode(errors="replace")
    marker = tmp_path / "dispatched.txt"
    [original_operation] = marker.read_text(encoding="ascii").splitlines()
    restarted = daemon_for(tmp_path)
    try:
        assert restarted.journal_health()["writeBlocked"]
        replay = await restarted._invoke(
            "daemon.operations.submit", REQUEST, timeout=5, on_progress=None
        )
        assert replay["operationId"] == original_operation
        assert replay["status"] == "unknown"
        status = await restarted._invoke(
            "daemon.operations.status",
            {"operationId": original_operation},
            timeout=5,
            on_progress=None,
        )
        assert status["counts"] == {"unknown": 1}
        assert restarted.session.calls == ["operations.receipt"]
        assert marker.read_text(encoding="ascii").splitlines() == [original_operation]
        assert not restarted.operations().busy
    finally:
        restarted.operations().journal.db.close()


if __name__ == "__main__":
    isolated = daemon_for(Path(sys.argv[1]), crash=True)
    asyncio.run(isolated._invoke("daemon.operations.submit", REQUEST, timeout=5, on_progress=None))

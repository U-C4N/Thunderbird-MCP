"""Exercise Windows daemon spawning through a real venv redirector."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from tbmcp import ipc
from tbmcp.bridge import Bridge

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows console regression"),
]

_CONSOLE_PROBE = """\
import ctypes
import json
import sys
from ctypes import wintypes

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetConsoleWindow.restype = wintypes.HWND
kernel.FreeConsole()
attached = bool(kernel.AttachConsole(int(sys.argv[1])))
error = ctypes.get_last_error() if not attached else 0
window = int(kernel.GetConsoleWindow() or 0) if attached else None
kernel.FreeConsole()
print(json.dumps({"attached": attached, "error": error, "window": window}))
"""


async def test_venv_daemon_has_no_console_and_remains_usable(tmp_path, monkeypatch) -> None:
    """DETACHED_PROCESS defeats CREATE_NO_WINDOW and the venv child gets a console.

    Checking flags alone misses the second CreateProcess call in the redirector.
    Use an empty profile and isolated state, then inspect the actual daemon's
    console in a separate probe process so pytest keeps its own console intact.
    """
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / "Scripts" / "python.exe"
    profile = tmp_path / "empty-profile"
    profile.mkdir()
    monkeypatch.setenv("TBMCP_STATE_DIR", str(tmp_path / "state"))
    # Let the new venv import the checkout and dependencies without installing
    # anything or requiring network access in the test.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p))

    children: list[subprocess.Popen] = []
    original_popen = subprocess.Popen

    def record_child(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    first = Bridge(profile_hint=str(profile), autostart=False)
    second = Bridge(profile_hint=str(profile), autostart=False)
    try:
        with monkeypatch.context() as launch:
            launch.setattr(sys, "executable", str(python))
            launch.setattr(subprocess, "Popen", record_child)
            await first._spawn_daemon()

        info = ipc.DaemonInfo.load()
        assert info is not None
        assert Path(info.profile) == profile
        probe = subprocess.run(
            [sys.executable, "-c", _CONSOLE_PROBE, str(info.pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=10,
            check=True,
        )
        console = json.loads(probe.stdout)
        assert console["attached"], console
        assert console["window"] == 0, "venv daemon unexpectedly allocated a console window"

        statuses = [await first.status(), await second.status(), await first.status()]
        assert all(status["daemon"]["pid"] == info.pid for status in statuses)
        assert statuses[-1]["daemon"]["clients"] == 2
        assert not any(status["connected"] for status in statuses)
        assert len(children) == 1
    finally:
        # Every process here was created by this test, with its own state/profile.
        # Also clean up when the console assertion fails against the old code.
        with contextlib.suppress(Exception):
            await first.call("daemon.shutdown", timeout=5, _retry=False)
        await first.close()
        await second.close()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)

    assert all(child.returncode == 0 for child in children)
    assert not ipc.DaemonInfo.path().exists()

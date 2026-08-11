"""A console script that exists is not the same as one that runs.

Windows Application Control blocks pip's generated .exe shims: the file is right
there, `is_file()` is true, and spawning it raises. Registering that path produces a
client that times out with nothing to point at.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from tbmcp import clients
from tbmcp.config import Settings


@pytest.fixture
def shim(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / ("thunderbird-mcp.exe" if os.name == "nt" else "thunderbird-mcp")
    path.write_bytes(b"MZ")
    return path


def test_unrunnable_shim_is_rejected(monkeypatch, shim):
    def blocked(argv, **kwargs):
        raise OSError("Uygulama Denetimi ilkesi bu dosyayi engelledi")

    monkeypatch.setattr(clients.subprocess, "run", blocked)
    assert clients._runnable(shim) is False


def test_nonzero_exit_is_rejected(monkeypatch, shim):
    monkeypatch.setattr(clients, "_probe_exit", lambda argv: 1)
    assert clients._runnable(shim) is False


def test_runnable_shim_is_accepted(monkeypatch, shim):
    monkeypatch.setattr(clients, "_probe_exit", lambda argv: 0)
    assert clients._runnable(shim) is True


def test_server_command_falls_back_to_module(monkeypatch):
    monkeypatch.setattr(clients, "_console_script", lambda: None)
    command, args = clients.server_command(Settings())
    assert args[:2] == ["-m", "tbmcp"]
    assert command.endswith(("python", "python.exe", "python3"))

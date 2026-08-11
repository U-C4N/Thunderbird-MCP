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


def test_console_script_returns_none_when_all_unrunnable(monkeypatch, tmp_path):
    """The candidate loop rejects all unrunnable paths and returns None."""
    fake_python = tmp_path / "python.exe"
    fake_python.write_bytes(b"fake")

    monkeypatch.setattr(clients.shutil, "which", lambda name: None)
    monkeypatch.setattr(clients.sys, "executable", str(fake_python))
    # All candidates exist as files
    monkeypatch.setattr(clients.Path, "is_file", lambda self: True)
    # But all are unrunnable
    monkeypatch.setattr(clients, "_runnable", lambda path: False)

    result = clients._console_script()
    assert result is None


def test_console_script_returns_runnable_candidate(monkeypatch, tmp_path):
    """The candidate loop returns the first runnable candidate."""
    fake_python = tmp_path / "python.exe"
    fake_python.write_bytes(b"fake")

    monkeypatch.setattr(clients.shutil, "which", lambda name: None)
    monkeypatch.setattr(clients.sys, "executable", str(fake_python))
    # All candidates exist as files
    monkeypatch.setattr(clients.Path, "is_file", lambda self: True)
    # Only the .exe candidate is runnable
    def is_runnable(path):
        return str(path).endswith(("thunderbird-mcp.exe", "thunderbird-mcp"))
    monkeypatch.setattr(clients, "_runnable", is_runnable)

    result = clients._console_script()
    assert result is not None
    assert result.name == "thunderbird-mcp.exe"

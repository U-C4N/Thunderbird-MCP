from __future__ import annotations

import os
import subprocess

from tbmcp import addon_install


def _fake_run(outputs: dict[str, str], seen: list[list[str]]):
    def run(argv, **_kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=outputs.get(argv[0], ""))

    return run


def test_pids_match_the_process_name_not_the_command_line(monkeypatch):
    """`pgrep -f thunderbird` also matched …/thunderbird-mcp/venv/bin/python, so
    install-addon sent SIGTERM to itself and left Thunderbird closed."""
    monkeypatch.setattr(addon_install.sys, "platform", "linux")
    seen: list[list[str]] = []
    monkeypatch.setattr(addon_install.subprocess, "run", _fake_run({"pgrep": "4242\n"}, seen))
    assert addon_install.running_pids() == [4242]
    assert seen == [["pgrep", "-x", "thunderbird|thunderbird-bin"]]


def test_pids_never_include_this_process(monkeypatch):
    monkeypatch.setattr(addon_install.sys, "platform", "linux")
    own = os.getpid()
    monkeypatch.setattr(addon_install.subprocess, "run", _fake_run({"pgrep": f"{own}\n4242\n"}, []))
    assert addon_install.running_pids() == [4242]

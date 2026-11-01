"""The acceptance harness must never adopt or stop a user's existing profile."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "test_live_profile_helper", Path(__file__).resolve().parents[1] / "tools" / "live_profile.py"
)
live_profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live_profile)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    binary = tmp_path / "thunderbird.exe"
    binary.touch()
    ports = iter([45001, 45002])
    monkeypatch.setattr(live_profile, "free_port", lambda: next(ports))
    return live_profile.LiveProfile(binary=binary, root=tmp_path / "fresh")


def test_existing_directory_is_never_reused(fixture):
    fixture.root.mkdir()
    sentinel = fixture.root / "mailbox"
    sentinel.write_text("untouched")
    with pytest.raises(ValueError, match="existing profiles are never reused"):
        live_profile.LiveProfile(binary=fixture.binary, root=fixture.root)
    assert sentinel.read_text() == "untouched"


def test_foreign_marionette_profile_is_refused_before_any_mutation(fixture, monkeypatch):
    commands = []

    class ForeignConnection:
        def __init__(self, **kwargs):
            pass

        def start_chrome_session(self):
            pass

        def execute(self, script):
            commands.append(script)
            return str(fixture.root / "different-profile")

        def close(self):
            commands.append("closed")

    monkeypatch.setattr(live_profile, "Marionette", ForeignConnection)
    with pytest.raises(RuntimeError, match="not this fixture"):
        fixture.chrome("throw new Error('must not execute')")
    assert len(commands) == 2
    assert "ProfD" in commands[0]
    assert commands[1] == "closed"


def test_failed_profile_connection_only_terminates_own_process_handle(fixture, monkeypatch):
    actions = []

    class OwnedProcess:
        def wait(self, *, timeout):
            actions.append("wait")
            if "terminate own" not in actions:
                raise subprocess.TimeoutExpired("fixture", timeout)

        def terminate(self):
            actions.append("terminate own")

    def wrong_profile():
        raise RuntimeError("not this fixture")

    fixture.process = OwnedProcess()
    monkeypatch.setattr(fixture, "_connection", wrong_profile)
    fixture.stop()
    assert actions == ["wait", "terminate own", "wait"]

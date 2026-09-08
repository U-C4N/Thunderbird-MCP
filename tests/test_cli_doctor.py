"""Coverage for `cmd_doctor` and `_doctor_ok`.

Before this file, neither had any test at all — the cli half of the C1 fix (the
`verify` step's `doctor --json` really certifying a working chain) rested on
inspection and manual runs alone. These tests fake the daemon/profile/server layer
so the whole function runs without a real Thunderbird, and target the two things the
final re-review flagged: `--no-start` must reach the *one* bridge `cmd_doctor` ever
builds (new breakage #1), and a deselected `admin` toolset must not read as a broken
chain (new breakage #4).
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from tbmcp.cli import _doctor_ok, build_parser, cmd_doctor
from tbmcp.profile import ThunderbirdProfile

# ------------------------------------------------------------------- _doctor_ok


def test_doctor_ok_true_when_bridge_and_tool_call_both_connected():
    report = {"bridge": {"connected": True}, "tbStatusCall": {"connected": True}}
    assert _doctor_ok(report) is True


def test_doctor_ok_false_when_bridge_is_not_connected():
    report = {"bridge": {"connected": False}, "tbStatusCall": {"connected": True}}
    assert _doctor_ok(report) is False


def test_doctor_ok_false_when_tool_call_genuinely_errors():
    report = {
        "bridge": {"connected": True},
        "tbStatusCall": {"error": "ToolError: Error executing tool tb_status: boom"},
    }
    assert _doctor_ok(report) is False


def test_doctor_ok_true_when_admin_toolset_deselected_and_bridge_is_healthy():
    """Item 4: `tb_status` only exists when `admin` is selected. Deselecting it is a
    supported configuration, not a broken chain — `tbStatusCall` carries `skipped`
    for exactly this case, and it must not sink `ok` the way a real dispatch
    failure would.
    """
    report = {
        "bridge": {"connected": True},
        "tbStatusCall": {"skipped": "admin toolset not selected; tb_status is not registered"},
    }
    assert _doctor_ok(report) is True


def test_doctor_ok_false_when_admin_deselected_but_bridge_is_not_connected():
    """The skip excuses the missing tool check only — it must not also excuse a
    genuinely disconnected bridge."""
    report = {
        "bridge": {"connected": False},
        "tbStatusCall": {"skipped": "admin toolset not selected; tb_status is not registered"},
    }
    assert _doctor_ok(report) is False


# ------------------------------------------------------------------- cmd_doctor


@pytest.fixture
def _clean_tbmcp_env(monkeypatch):
    """`Settings.from_env()` reads these; strip them so the test's outcome depends
    only on what it configures explicitly, not on this machine's shell."""
    for var in (
        "TBMCP_TOOLSETS",
        "TBMCP_READ_ONLY",
        "TBMCP_YOLO",
        "TBMCP_UNSAFE_PREFS",
        "TBMCP_SEND",
        "TBMCP_PROFILE",
        "TBMCP_TIMEOUT",
        "TBMCP_NO_AUTOSTART",
        "TBMCP_TOOLS",
    ):
        monkeypatch.delenv(var, raising=False)


def _stub_common(
    monkeypatch,
    *,
    bridge_status: dict,
    addon_summary: dict | None = None,
    profile=None,
) -> list:
    """Fake every dependency `cmd_doctor` reaches for besides `Bridge`/`build_server`,
    which each test fakes itself. Returns the bridges that were constructed.

    `addon_summary` stands in for `addon_install.summary()` (the version in *this*
    package's source), and `profile` for a real profile directory — the version the
    add-on actually installed is read out of the status file it leaves there.
    """
    created_bridges: list = []

    class FakeBridge:
        def __init__(self, *, profile_hint=None, autostart=True, default_timeout=30.0):
            self.profile_hint = profile_hint
            self.autostart = autostart
            self.default_timeout = default_timeout
            self.closed = False
            created_bridges.append(self)

        async def require_thunderbird(self, *, wait=0.0):
            return dict(bridge_status)

        async def status(self):
            return dict(bridge_status)

        async def close(self):
            self.closed = True

    monkeypatch.setattr("tbmcp.bridge.Bridge", FakeBridge)
    monkeypatch.setattr("tbmcp.profile.list_profiles", lambda: [profile] if profile else [])
    monkeypatch.setattr("tbmcp.profile.find_profile", lambda *_a, **_k: profile)
    monkeypatch.setattr("tbmcp.addon_install.summary", lambda: dict(addon_summary or {}))

    from tbmcp.ipc import DaemonInfo

    monkeypatch.setattr(DaemonInfo, "load", classmethod(lambda cls: None))

    return created_bridges


@pytest.mark.usefixtures("_clean_tbmcp_env")
def test_no_start_reuses_the_one_bridge_and_never_autostarts_a_second(monkeypatch):
    """Regression guard for new breakage #1 in the final re-review: `cmd_doctor`
    built a deliberately non-autostarting `Bridge` for `--no-start`, then handed
    `build_server` no bridge at all — `build_server` then constructed a *second*,
    autostarting `Bridge` (`server.py:136`), silently defeating `--no-start` and
    leaking the first connection. The fix is `build_server(settings, bridge=bridge)`.
    This fails if that keyword argument regresses: reverting it makes
    `build_server_calls == [None]` instead of `[created_bridges[0]]`.
    """
    created_bridges = _stub_common(monkeypatch, bridge_status={"connected": True})
    build_server_calls: list = []

    class FakeMCP:
        async def call_tool(self, name, args):
            return SimpleNamespace(structured_content={"connected": True})

    def fake_build_server(settings, *, bridge=None):
        build_server_calls.append(bridge)
        return FakeMCP()

    monkeypatch.setattr("tbmcp.server.build_server", fake_build_server)

    args = build_parser().parse_args(["doctor", "--json", "--no-start", "--wait", "0"])
    exit_code = cmd_doctor(args)

    assert len(created_bridges) == 1, "cmd_doctor must construct exactly one Bridge"
    assert created_bridges[0].autostart is False, (
        "--no-start must reach the one bridge cmd_doctor builds"
    )
    assert build_server_calls == [created_bridges[0]], (
        "build_server must reuse the already-established, non-autostarting bridge, "
        "not default to constructing a fresh (autostarting) one"
    )
    assert created_bridges[0].closed is True, "the one bridge must be closed on the way out"
    assert exit_code == 0


@pytest.mark.usefixtures("_clean_tbmcp_env")
def test_admin_toolset_deselected_does_not_dispatch_tb_status_and_still_reports_ok(monkeypatch):
    """Item 4: with `admin` deselected, `tb_status` is not registered — dispatching
    it anyway would just raise `ToolError: Unknown tool: tb_status`, indistinguishable
    from a real failure to a naive check. `cmd_doctor` must recognise the deselection
    up front and never even attempt the call, and the overall report must still be
    `ok` when the bridge itself is healthy.
    """
    created_bridges = _stub_common(monkeypatch, bridge_status={"connected": True})
    call_tool_invocations: list = []
    build_server_settings: list = []

    class FakeMCP:
        async def call_tool(self, name, args):
            call_tool_invocations.append(name)
            return SimpleNamespace(structured_content={"connected": True})

    def fake_build_server(settings, *, bridge=None):
        build_server_settings.append(settings)
        return FakeMCP()

    monkeypatch.setattr("tbmcp.server.build_server", fake_build_server)

    args = build_parser().parse_args(["doctor", "--json", "--toolsets", "mail"])
    exit_code = cmd_doctor(args)

    assert "admin" not in build_server_settings[0].toolsets
    assert call_tool_invocations == [], (
        "tb_status must never be dispatched when admin is deselected"
    )
    assert len(created_bridges) == 1
    assert exit_code == 0


@pytest.mark.usefixtures("_clean_tbmcp_env")
def test_returns_nonzero_when_the_bridge_is_genuinely_not_connected(monkeypatch):
    """Sanity check on the other side of item 4: a real problem must still fail."""
    _stub_common(monkeypatch, bridge_status={"connected": False})

    class FakeMCP:
        async def call_tool(self, name, args):
            return SimpleNamespace(structured_content={"connected": False})

    monkeypatch.setattr("tbmcp.server.build_server", lambda settings, *, bridge=None: FakeMCP())

    args = build_parser().parse_args(["doctor", "--json"])
    exit_code = cmd_doctor(args)

    assert exit_code == 1


# ------------------------------------------------- what doctor now has to explain
#
# The chain was healthy-looking and broken at the same time: Thunderbird was
# running, the add-on was dialling in every ~25 s and failing the handshake, and
# the add-on it had installed (1.2.0) was older than the one in this package. All
# `doctor` would say was "the add-on has not dialled in yet; try again in a moment".

_ADDON_STATUS = {
    "writtenAt": "2026-09-08T15:00:00.000Z",
    "addonVersion": "1.2.0",
    "capabilities": {"privilegedModules": {"loaded": ["admin", "prefs"]}},
    "methodCount": 70,
}

_TRANSPORT = {
    "state": "connecting",
    "connecting": True,
    "attempt": 7,
    "inFlight": 0,
    "consecutiveFailures": 7,
    "lastCloseCode": 1006,
    "lastCloseAt": "2026-09-08T15:04:00.000Z",
    "lastWelcomeAt": None,
    "port": 50284,
}

_FAILING_HANDSHAKE = {
    "attempts": 3,
    "lastAttemptAt": time.time() - 12,
    "lastOutcome": "no-hello-timeout",
    "lastCloseCode": 4002,
    "lastUserAgent": "Thunderbird/155.0",
    "recentFailures": 3,
    "recentOutcomes": {"no-hello-timeout": 3},
    "windowSeconds": 60.0,
    "recent": [],
}


def _profile(tmp_path, status: dict | None = None):
    """A real `ThunderbirdProfile` on disk, optionally with the add-on's own report."""
    profile = ThunderbirdProfile(path=tmp_path, name="default", is_default=True, root=tmp_path)
    if status is not None:
        profile.addon_status_file.write_text(json.dumps(status), encoding="utf-8")
    return profile


def _stub_server(monkeypatch, *, connected: bool) -> None:
    class FakeMCP:
        async def call_tool(self, name, args):
            return SimpleNamespace(structured_content={"connected": connected})

    monkeypatch.setattr("tbmcp.server.build_server", lambda settings, *, bridge=None: FakeMCP())


def _run_doctor(monkeypatch, argv: list[str], **stubs) -> int:
    _stub_common(monkeypatch, **stubs)
    _stub_server(monkeypatch, connected=bool(stubs["bridge_status"].get("connected")))
    return cmd_doctor(build_parser().parse_args(["doctor", *argv, "--wait", "0"]))


@pytest.mark.usefixtures("_clean_tbmcp_env")
def test_the_json_report_compares_the_installed_add_on_with_the_source(
    monkeypatch, tmp_path, capsys
):
    exit_code = _run_doctor(
        monkeypatch,
        ["--json"],
        bridge_status={"connected": False},
        addon_summary={"addonVersion": "1.3.0", "thunderbirdRunning": True},
        profile=_profile(tmp_path, _ADDON_STATUS),
    )

    report = json.loads(capsys.readouterr().out)
    assert report["addonVersions"] == {
        "installed": "1.2.0",
        "live": None,
        "source": "1.3.0",
        "mismatch": True,
    }
    assert exit_code == 1


@pytest.mark.usefixtures("_clean_tbmcp_env")
def test_an_out_of_date_add_on_is_named_with_its_remedy(monkeypatch, tmp_path, capsys):
    exit_code = _run_doctor(
        monkeypatch,
        [],
        bridge_status={"connected": False},
        addon_summary={"addonVersion": "1.3.0", "thunderbirdRunning": True},
        profile=_profile(tmp_path, _ADDON_STATUS),
    )

    out = capsys.readouterr().out
    assert "add-on version (installed)" in out
    assert "installed add-on is 1.2.0, source is 1.3.0" in out
    assert "tbmcp install-addon" in out
    assert exit_code == 1


@pytest.mark.usefixtures("_clean_tbmcp_env")
def test_a_working_chain_on_an_older_add_on_is_a_warning_not_a_failure(
    monkeypatch, tmp_path, capsys
):
    """It works, so `ok` stays true — but the user is running code they replaced."""
    exit_code = _run_doctor(
        monkeypatch,
        [],
        bridge_status={
            "connected": True,
            "thunderbird": {"addonVersion": "1.2.0", "experiment": True, "app": {}},
        },
        addon_summary={"addonVersion": "1.3.0", "thunderbirdRunning": True},
        profile=_profile(tmp_path, _ADDON_STATUS),
    )

    out = capsys.readouterr().out
    assert "Warning: installed add-on is 1.2.0, source is 1.3.0" in out
    assert exit_code == 0


@pytest.mark.usefixtures("_clean_tbmcp_env")
def test_a_matching_add_on_says_nothing_about_versions(monkeypatch, tmp_path, capsys):
    exit_code = _run_doctor(
        monkeypatch,
        [],
        bridge_status={
            "connected": True,
            "thunderbird": {"addonVersion": "1.3.0", "experiment": True, "app": {}},
        },
        addon_summary={"addonVersion": "1.3.0", "thunderbirdRunning": True},
        profile=_profile(tmp_path, {**_ADDON_STATUS, "addonVersion": "1.3.0"}),
    )

    out = capsys.readouterr().out
    assert "Warning" not in out
    assert exit_code == 0


@pytest.mark.usefixtures("_clean_tbmcp_env")
def test_repeated_handshake_failures_replace_the_try_again_in_a_moment_advice(
    monkeypatch, tmp_path, capsys
):
    """The advice that was wrong all morning: the add-on had dialled in dozens of
    times, so telling the user to wait a moment sent them nowhere."""
    exit_code = _run_doctor(
        monkeypatch,
        [],
        bridge_status={"connected": False, "handshake": _FAILING_HANDSHAKE},
        addon_summary={"addonVersion": "1.2.0", "thunderbirdRunning": True},
        profile=_profile(tmp_path, _ADDON_STATUS),
    )

    out = capsys.readouterr().out
    # The diagnosis is a wrapped paragraph, so read it as one flowing sentence.
    flat = " ".join(out.split())
    assert "add-on handshakes" in out
    assert "3 attempts" in out
    assert "never completed the handshake" in flat
    assert "try again in a moment" not in flat
    assert exit_code == 1


@pytest.mark.usefixtures("_clean_tbmcp_env")
def test_the_add_ons_own_account_of_the_connection_is_printed(monkeypatch, tmp_path, capsys):
    """The add-on's side of the same story, from the file it writes itself: without
    it a reader cannot tell a daemon nobody dialled from an add-on that keeps
    failing to."""
    _run_doctor(
        monkeypatch,
        [],
        bridge_status={"connected": False},
        addon_summary={"addonVersion": "1.2.0", "thunderbirdRunning": True},
        profile=_profile(tmp_path, {**_ADDON_STATUS, "transport": _TRANSPORT}),
    )

    out = capsys.readouterr().out
    assert "add-on transport" in out
    assert "connecting" in out
    assert "7 failed attempts" in out
    assert "1006" in out


@pytest.mark.usefixtures("_clean_tbmcp_env")
def test_the_daemon_log_is_named_because_nothing_else_shows_it(monkeypatch, tmp_path, capsys):
    _run_doctor(
        monkeypatch,
        [],
        bridge_status={"connected": False},
        addon_summary={"addonVersion": "1.2.0", "thunderbirdRunning": False},
        profile=_profile(tmp_path),
    )

    out = capsys.readouterr().out
    assert "daemon log" in out
    assert "daemon.log" in out
    assert "add-on handshakes" in out
    assert "none since the daemon started" in out

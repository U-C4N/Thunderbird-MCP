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
from types import SimpleNamespace

import pytest

from tbmcp.cli import _doctor_ok, build_parser, cmd_doctor, main

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


def _stub_common(monkeypatch, *, bridge_status: dict) -> tuple[list, list]:
    """Fake every dependency `cmd_doctor` reaches for besides `Bridge`/`build_server`,
    which each test fakes itself. Returns (created_bridges, build_server_calls).
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
    monkeypatch.setattr("tbmcp.profile.list_profiles", lambda: [])
    monkeypatch.setattr("tbmcp.profile.find_profile", lambda *_a, **_k: None)
    monkeypatch.setattr("tbmcp.addon_install.summary", lambda: {})

    from tbmcp.ipc import DaemonInfo

    monkeypatch.setattr(DaemonInfo, "load", classmethod(lambda cls: None))

    return created_bridges


def test_doctor_ok_false_when_the_configuration_was_refused():
    """A healthy bridge must not vouch for a server that was never built."""
    report = {"bridge": {"connected": True}, "configError": "unknown toolset 'bogus'"}
    assert _doctor_ok(report) is False


@pytest.mark.usefixtures("_clean_tbmcp_env")
def test_a_refused_flag_still_produces_a_json_report(capsys):
    """The flag parsers reject with `SystemExit`, which is a `BaseException` and so
    slips past every `except Exception` here. `doctor --json` therefore printed
    nothing at all on a typo — and a caller reading stdout cannot tell "you gave me a
    bad flag" from "doctor crashed", which is the one distinction the machine-readable
    output exists to make.

    No stubs: this must return before it reaches a bridge or a profile.
    """
    args = build_parser().parse_args(["doctor", "--json", "--toolsets", "bogus"])
    exit_code = cmd_doctor(args)

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "bogus" in payload["configError"]


@pytest.mark.usefixtures("_clean_tbmcp_env")
def test_a_refused_flag_says_so_in_the_human_readable_output_too(capsys):
    args = build_parser().parse_args(["doctor", "--toolsets", "bogus"])
    exit_code = cmd_doctor(args)

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "configuration rejected" in out
    assert "bogus" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["doctor", "--json", "--timeout", "bogus"],
        ["doctor", "--json", "--nosuchflag"],
    ],
)
@pytest.mark.usefixtures("_clean_tbmcp_env")
def test_argparse_refusals_also_produce_a_json_report(capsys, argv):
    """`cmd_doctor` guards the refusals it can see, but argparse rejects a malformed
    command line before any subcommand runs — and exited with usage on stderr and
    nothing on stdout, which is the same broken contract one layer earlier.
    """
    with pytest.raises(SystemExit) as caught:
        main(argv)

    assert caught.value.code == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["configError"]
    # argparse's own message is the useful one; a person should not have to read JSON.
    assert "error:" in captured.err


@pytest.mark.usefixtures("_clean_tbmcp_env")
def test_help_and_version_are_not_treated_as_refusals(capsys):
    """`--help` also leaves through `SystemExit`, with code 0. Emitting a failure
    report for it would turn a successful command into a broken one."""
    with pytest.raises(SystemExit) as caught:
        main(["doctor", "--help"])
    assert caught.value.code == 0
    assert "configError" not in capsys.readouterr().out


def test_the_parser_alone_keeps_argparse_behaviour(capsys):
    """The contract belongs to `main`, which knows the real command line. A parser
    driven directly — every other test in this repo — must be unaffected."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["doctor", "--json", "--timeout", "bogus"])
    assert capsys.readouterr().out == ""


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

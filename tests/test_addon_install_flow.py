"""`install-addon` must never leave Thunderbird stopped.

Seen live: the add-on was installed and had already attached to the daemon, but the
Marionette call that ran the install script answered `None`. `install_automatic`
raised before any restart, so the command failed *and* the user's mail client
stayed closed — a worse state than before the command ran.

The rule is simple: once we stopped Thunderbird, we restart it on every exit path,
and an unreadable install report is checked against the AddonManager before it is
allowed to count as a failure.
"""

from __future__ import annotations

import pathlib

import pytest

from tbmcp import addon_install
from tbmcp.errors import TbmcpError

EXPECTED_VERSION = "9.9.9"


class FakeClient:
    """Just enough Marionette: the install script answers `install_report`, the
    status script answers `status_report`, anything else raises `blow_up`."""

    def __init__(self, install_report, status_report=None, blow_up=None):
        self.install_report = install_report
        self.status_report = status_report
        self.blow_up = blow_up
        self.quit_calls = 0
        self.close_calls = 0

    def start_chrome_session(self):
        return {}

    def execute(self, script, args=None, *, timeout_ms=0):
        if self.blow_up is not None:
            raise self.blow_up
        if script is addon_install.INSTALL_SCRIPT:
            return self.install_report
        if script is addon_install.STATUS_SCRIPT:
            return self.status_report
        raise AssertionError("unexpected script")

    def quit_application(self):
        self.quit_calls += 1

    def close(self):
        self.close_calls += 1


@pytest.fixture
def flow(monkeypatch, tmp_path):
    """Stub every seam that touches the machine; record the restarts."""
    calls = {"restart": 0, "stop": 0, "launch": []}
    package = tmp_path / "bridge.xpi"
    package.write_bytes(b"not really a zip")

    monkeypatch.setattr(addon_install, "find_thunderbird", lambda: pathlib.Path("tb.exe"))
    monkeypatch.setattr(addon_install, "build_xpi", lambda _dest: package)
    monkeypatch.setattr(addon_install, "addon_id", lambda: "bridge@example")
    monkeypatch.setattr(addon_install, "addon_version", lambda src=None: EXPECTED_VERSION)
    monkeypatch.setattr(addon_install, "is_running", lambda: True)

    def stop(timeout=60.0):
        calls["stop"] += 1
        return True

    def launch(exe, extra_args, profile):
        calls["launch"].append(list(extra_args))

    def restart(exe, profile, restart):
        calls["restart"] += 1

    monkeypatch.setattr(addon_install, "_stop", stop)
    monkeypatch.setattr(addon_install, "_launch", launch)
    monkeypatch.setattr(addon_install, "_restart_plain", restart)
    monkeypatch.setattr(addon_install.marionette, "wait_for_port", lambda timeout=0: True)

    def use(client):
        monkeypatch.setattr(addon_install.marionette, "connect", lambda timeout=0: client)
        return calls

    return use


def test_an_unreadable_install_report_is_checked_before_it_counts_as_a_failure(flow):
    client = FakeClient(
        install_report=None,
        status_report={"installed": True, "version": EXPECTED_VERSION, "isActive": True},
    )
    calls = flow(client)

    outcome = addon_install.install_automatic()

    assert outcome.ok, outcome.message
    assert EXPECTED_VERSION in outcome.message
    assert calls["restart"] == 1, "Thunderbird must be restarted exactly once"
    assert client.quit_calls == 1, "Marionette must still be taken down"


def test_an_unreadable_report_with_a_bad_status_fails_but_still_restarts(flow):
    client = FakeClient(
        install_report=None,
        status_report={"installed": True, "version": "1.0.0", "isActive": False},
    )
    calls = flow(client)

    with pytest.raises(TbmcpError) as caught:
        addon_install.install_automatic()

    assert "restarted" in str(caught.value).lower()
    assert calls["restart"] == 1


def test_a_failure_inside_the_automation_still_restarts_thunderbird(flow):
    client = FakeClient(install_report=None, blow_up=RuntimeError("marionette exploded"))
    calls = flow(client)

    with pytest.raises(RuntimeError):
        addon_install.install_automatic()

    assert calls["restart"] == 1
    assert client.quit_calls == 1


def test_a_normal_success_restarts_once(flow):
    client = FakeClient(
        install_report={"result": {"ok": True, "version": EXPECTED_VERSION, "isActive": True}}
    )
    calls = flow(client)

    outcome = addon_install.install_automatic()

    assert outcome.ok
    assert calls["restart"] == 1

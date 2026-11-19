"""Finding a mail client that is not called `thunderbird` in `/usr/bin`.

Seen live on a Betterbird-only machine: the Flatpak build keeps its executable at
`~/.var/app/eu.betterbird.Betterbird/...`, its profile at
`~/.var/app/eu.betterbird.Betterbird/.thunderbird`, and its processes named
`betterbird`. Every guess the installer made about "Thunderbird" missed, and the
one that would have been worst — launching over the running instance, so that
`-marionette` is swallowed by the window already open — was masked by
`is_running()` answering "closed".

The Flatpak profile path also cannot be passed through `-profile` unchanged: the
sandbox mounts the app's data directory *as* `$HOME`, so the host path points at
nothing inside and Thunderbird would create a fresh profile there.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

from tbmcp import addon_install
from tbmcp.profile import ThunderbirdProfile, candidate_roots

APP = "eu.betterbird.Betterbird"


@pytest.fixture
def home(tmp_path, monkeypatch) -> pathlib.Path:
    """A machine with no system-wide mail client, and `$HOME` under the test."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(addon_install.shutil, "which", lambda name: None)
    # Hermetic: this test host really does have exports in /var/lib/flatpak.
    monkeypatch.setattr(addon_install, "_flatpak_exports", lambda app: [tmp_path / "exports" / app])
    monkeypatch.setattr(addon_install, "NATIVE_DIRS", (tmp_path / "no-bin",))
    return tmp_path


def _exe(home: pathlib.Path) -> pathlib.Path:
    path = home / "exports" / APP
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------ profiles


def test_flatpak_profile_root_is_a_candidate(home):
    (home / ".var" / "app" / APP / ".thunderbird").mkdir(parents=True)
    assert home / ".var" / "app" / APP / ".thunderbird" in candidate_roots()


def test_an_unknown_flatpak_build_is_still_a_candidate(home):
    root = home / ".var" / "app" / "org.example.Thunderbird" / ".thunderbird"
    root.mkdir(parents=True)
    assert root in candidate_roots()


# ---------------------------------------------------------------- executable


def test_override_wins_and_is_expanded(home, monkeypatch):
    exe = home / "opt" / "betterbird"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    _exe(home)
    monkeypatch.setenv("TBMCP_THUNDERBIRD", "~/opt/betterbird")
    assert addon_install.find_thunderbird() == exe


def test_flatpak_wrapper_is_found_without_an_override(home, monkeypatch):
    monkeypatch.delenv("TBMCP_THUNDERBIRD", raising=False)
    exe = _exe(home)
    assert addon_install.find_thunderbird() == exe


def test_a_missing_override_still_searches(home, monkeypatch):
    monkeypatch.setenv("TBMCP_THUNDERBIRD", "~/opt/typo")
    exe = _exe(home)
    assert addon_install.find_thunderbird() == exe


# ------------------------------------------------------------------- profile


def _profile(home: pathlib.Path) -> ThunderbirdProfile:
    path = home / ".var" / "app" / APP / ".thunderbird" / "abc.default"
    path.mkdir(parents=True)
    return ThunderbirdProfile(path=path, name="default", is_default=True, root=path.parent)


def test_flatpak_profile_is_translated_to_the_sandbox_view(home):
    profile = _profile(home)
    assert addon_install._profile_argument(home / "exports" / APP, profile) == str(
        home / ".thunderbird" / "abc.default"
    )


def test_native_profile_path_is_passed_unchanged(home):
    profile = _profile(home)
    assert addon_install._profile_argument(pathlib.Path("/usr/bin/thunderbird"), profile) == str(
        profile.path
    )


def test_a_profile_outside_the_sandbox_is_not_passed(home):
    elsewhere = home / "mnt" / "mail" / "abc.default"
    profile = ThunderbirdProfile(
        path=elsewhere, name="abc.default", is_default=True, root=elsewhere.parent
    )
    assert addon_install._profile_argument(home / "exports" / APP, profile) is None


def test_launch_uses_the_translated_profile(home, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: calls.append(argv))
    profile = _profile(home)

    addon_install._launch(home / "exports" / APP, ["-marionette"], profile)

    assert calls[0][-2:] == ["-profile", str(home / ".thunderbird" / "abc.default")]


# ------------------------------------------------------------------ processes


def test_betterbird_counts_as_running_and_bwrap_is_not_signalled(home, monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "10\n11\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(addon_install, "_is_sandbox_wrapper", lambda pid: pid == 11)

    assert addon_install.running_pids() == [10]
    assert "betterbird" in seen[0][-1]

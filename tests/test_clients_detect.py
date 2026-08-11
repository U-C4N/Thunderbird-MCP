# tests/test_clients_detect.py
"""`installed_clients` — which clients look present, and on what evidence.

Every writer already knows where its client's config lives; this is that same
knowledge read back rather than written. Each test turns on exactly one signal so a
failure points at the one path or CLI check that is wrong.
"""

from __future__ import annotations

import pytest

from tbmcp import clients


@pytest.fixture(autouse=True)
def _nothing_present(monkeypatch, tmp_path):
    """Start every test from a machine with none of the seven clients installed.

    Without this, `installed_clients` would read the real filesystem and PATH of
    whatever machine happens to run the suite — exactly the flakiness
    `_no_stray_interpreters` guards against in the bootstrap tests, for the same
    underlying reason.
    """
    monkeypatch.setattr(clients.shutil, "which", lambda name: None)
    nowhere = tmp_path / "nowhere"
    monkeypatch.setattr(clients, "_claude_code_path", lambda: nowhere / "claude.json")
    monkeypatch.setattr(clients, "_codex_home", lambda: nowhere / "codex")
    monkeypatch.setattr(
        clients, "_claude_desktop_path", lambda: nowhere / "claude-desktop" / "c.json"
    )
    monkeypatch.setattr(clients, "_cursor_path", lambda: nowhere / "cursor" / "mcp.json")
    monkeypatch.setattr(
        clients, "_vscode_user_path", lambda: nowhere / "vscode" / "User" / "mcp.json"
    )
    monkeypatch.setattr(clients, "_gemini_path", lambda: nowhere / "gemini" / "settings.json")
    monkeypatch.setattr(clients, "_zed_path", lambda: nowhere / "zed" / "settings.json")


def test_nothing_present_detects_nothing():
    assert clients.installed_clients() == []


def test_claude_code_cli_on_path_counts_even_without_a_config_file(monkeypatch):
    """The config file only appears after `claude mcp add`, so the CLI is the signal."""
    monkeypatch.setattr(
        clients.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None
    )
    assert clients.installed_clients() == ["claude-code"]


def test_codex_config_dir_counts_even_without_the_cli_on_path(monkeypatch, tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setattr(clients, "_codex_home", lambda: home)
    assert clients.installed_clients() == ["codex"]


def test_vscode_cli_on_path_counts_even_without_a_config_file(monkeypatch):
    """`_vscode` tries `code --add-mcp` before ever touching the file — same as claude/codex."""
    monkeypatch.setattr(
        clients.shutil, "which", lambda name: "/usr/bin/code" if name == "code" else None
    )
    assert clients.installed_clients() == ["vscode"]


def test_gui_client_config_file_present_is_the_signal(monkeypatch, tmp_path):
    path = tmp_path / "cursor" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(clients, "_cursor_path", lambda: path)
    assert clients.installed_clients() == ["cursor"]


def test_gui_client_config_dir_without_the_file_still_counts(monkeypatch, tmp_path):
    """An app that is installed but never had a server registered has the dir, not the file."""
    path = tmp_path / "gemini" / "settings.json"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(clients, "_gemini_path", lambda: path)
    assert clients.installed_clients() == ["gemini"]


def test_result_order_follows_clients_tuple_not_discovery_order(monkeypatch, tmp_path):
    """Zed is found before Codex here, but CLIENTS puts codex first — the report must too."""
    zed_path = tmp_path / "zed" / "settings.json"
    zed_path.parent.mkdir(parents=True)
    monkeypatch.setattr(clients, "_zed_path", lambda: zed_path)
    monkeypatch.setattr(
        clients.shutil, "which", lambda name: "/bin/codex" if name == "codex" else None
    )
    assert clients.installed_clients() == ["codex", "zed"]

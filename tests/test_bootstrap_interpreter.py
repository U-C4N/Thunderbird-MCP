"""Ranking prefers a system interpreter; the load test decides, not the ranking."""

from __future__ import annotations

import json

import pytest

from tbmcp.bootstrap import (
    BootstrapError,
    choose_interpreter,
    interpreter_ok,
    rank_interpreters,
)

SYSTEM = r"C:\Python314\python.exe"
UV = r"C:\Users\me\AppData\Roaming\uv\python\cpython-3.13\python.exe"


def test_uv_managed_ranks_last_but_is_not_dropped():
    assert rank_interpreters([UV, SYSTEM]) == [SYSTEM, UV]


def test_ranking_removes_duplicates_and_keeps_order():
    assert rank_interpreters([SYSTEM, SYSTEM, UV]) == [SYSTEM, UV]


def test_load_test_requires_the_extension_modules():
    def blocked(argv):
        return 1, "ImportError: DLL load failed while importing _sqlite3: engellendi"

    def healthy(argv):
        return 0, json.dumps({"ok": True})

    assert interpreter_ok("python", run=blocked) is False
    assert interpreter_ok("python", run=healthy) is True


def test_first_healthy_candidate_wins():
    def run(argv):
        if argv[0] == UV:
            return 0, json.dumps({"ok": True})
        return 1, "blocked"

    assert choose_interpreter(candidates=[SYSTEM, UV], run=run) == UV


def test_explicit_choice_is_still_load_tested():
    def run(argv):
        return 1, "blocked"

    with pytest.raises(BootstrapError) as caught:
        choose_interpreter(SYSTEM, candidates=[UV], run=run)
    assert SYSTEM in str(caught.value)


def test_nothing_usable_names_what_was_tried():
    def run(argv):
        return 1, "blocked"

    with pytest.raises(BootstrapError) as caught:
        choose_interpreter(candidates=[SYSTEM, UV], run=run)
    assert "2" in str(caught.value)


def test_json_parse_rejects_mere_substring_match():
    """Echoing the load-test source (contains marker but no JSON) must fail."""

    def run(argv):
        # Returns 0 but output contains the marker string without valid JSON
        return 0, 'echo "ok": true\n'

    assert interpreter_ok("python", run=run) is False


def test_launcher_resolves_versions(monkeypatch):
    """The py launcher is resolved to concrete paths, not passed as-is."""
    from tbmcp.bootstrap import candidate_interpreters

    # Use distinctive paths that won't appear from hardcoded C:\PythonXXX fallback.
    sentinel_launcher = r"C:\WINDOWS\py.EXE"
    resolved_313 = r"C:\LauncherResolved\313\python.exe"
    resolved_312 = r"C:\LauncherResolved\312\python.exe"

    def fake_which(name):
        if name == "py":
            return sentinel_launcher
        # Don't find other pythons; we control the test environment.
        return None

    def run_fake(argv):
        # Launcher resolution calls must match the resolved path from shutil.which.
        if len(argv) >= 2 and argv[0] == sentinel_launcher:
            if argv[1] == "-3.13":
                return 0, f"{resolved_313}\n"
            if argv[1] == "-3.12":
                return 0, f"{resolved_312}\n"
            if argv[1] == "-3.11":
                return 1, "not found"  # Simulate unresolvable version.
            raise AssertionError(f"Unexpected launcher call: {argv}")
        # Interpreter load tests (from interpreter_ok).
        if "-c" in argv:
            return 0, json.dumps({"ok": True})
        raise AssertionError(f"Unexpected run call: {argv}")

    monkeypatch.setattr("shutil.which", fake_which)

    candidates = candidate_interpreters(run=run_fake)

    # Resolved launcher paths must be present.
    assert resolved_313 in candidates, f"Missing {resolved_313} from {candidates}"
    assert resolved_312 in candidates, f"Missing {resolved_312} from {candidates}"

    # Unresolvable launcher version (3.11) must not be added via launcher resolution.
    resolved_311 = r"C:\LauncherResolved\311\python.exe"
    assert resolved_311 not in candidates, f"Unresolved 3.11 should not be in {candidates}"

    # Launcher path itself must not appear.
    assert sentinel_launcher not in candidates

    # No garbage JSON strings from unhandled calls.
    assert not any(c.startswith("{") for c in candidates), f"JSON garbage in {candidates}"

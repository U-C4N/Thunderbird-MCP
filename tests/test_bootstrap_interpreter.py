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


def test_launcher_resolves_versions():
    """The py launcher is resolved to concrete paths, not passed as-is."""
    from tbmcp.bootstrap import candidate_interpreters

    resolved_313 = r"C:\Python313\python.exe"
    resolved_312 = r"C:\Python312\python.exe"

    def run(argv):
        if len(argv) >= 2 and argv[0] == "py" and argv[1] == "-3.13":
            return 0, f"{resolved_313}\n"
        if len(argv) >= 2 and argv[0] == "py" and argv[1] == "-3.12":
            return 0, f"{resolved_312}\n"
        if len(argv) >= 2 and argv[0] == "py" and argv[1] == "-3.11":
            return 1, "not found"  # Simulate missing version
        # For interpreter_ok calls (load test)
        return 0, json.dumps({"ok": True})

    candidates = candidate_interpreters(run=run)
    # Should include resolved paths, not the "py -X.Y" strings
    assert resolved_313 in candidates
    assert resolved_312 in candidates
    assert "py -3.13" not in candidates
    assert "py -3.12" not in candidates
    assert "py -3.11" not in candidates

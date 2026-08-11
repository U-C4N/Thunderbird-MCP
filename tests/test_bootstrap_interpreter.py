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

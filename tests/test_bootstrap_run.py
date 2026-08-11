# tests/test_bootstrap_run.py
"""The orchestration: fixed step order, idempotent, and honest about failure."""

from __future__ import annotations

import json

import pytest

from tbmcp.bootstrap import Options, Report, Step, _step_addon, _step_clients, _step_verify, bootstrap

STEPS = ["interpreter", "venv", "install", "imports", "binaries", "launcher",
         "addon", "clients", "verify"]


@pytest.fixture(autouse=True)
def _no_stray_interpreters(monkeypatch):
    """Keep interpreter discovery to `sys.executable` alone.

    `candidate_interpreters` shells out to a `py` launcher and probes `python3.x` names
    on PATH. Whether those exist is a fact about the machine running the suite, not
    about bootstrap's logic — and a `Recorder` that raises on calls it wasn't built for
    (see below) needs that fact pinned down, or the same test fails on one machine and
    passes on another.
    """
    monkeypatch.setattr("shutil.which", lambda name: None)


class Recorder:
    """Every subprocess succeeds; imports work first try.

    A call this fake was not built to answer raises rather than falling through to a
    success-shaped `(0, "")`: a step invoking the wrong interpreter, or shelling out
    with the wrong argv, must show up as a test failure, not disappear into a silent
    no-op that happens to look fine.
    """

    def __init__(self):
        self.calls: list[list[str]] = []

    def run(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        if "-c" in argv:
            source = argv[argv.index("-c") + 1]
            if "sqlite3" in source:
                return 0, json.dumps({"ok": True, "version": "3.14"})
            if "__import__" in source:
                return 0, json.dumps({"ok": True})
            if "packages_distributions" in source:
                return 0, json.dumps({"dist": None})
        raise AssertionError(f"Recorder was not built to answer this call: {argv}")


def test_reports_every_step_in_order(tmp_path):
    recorder = Recorder()
    report = bootstrap(Options(venv=tmp_path / "venv", dry_run=True), run=recorder.run)
    assert [step.name for step in report.steps] == STEPS
    assert report.ok is True
    assert report.next_command is None


def test_dry_run_creates_nothing(tmp_path):
    venv = tmp_path / "venv"
    bootstrap(Options(venv=venv, dry_run=True), run=Recorder().run)
    assert not venv.exists()


def test_failure_stops_and_names_the_next_command(tmp_path):
    def run(argv):
        return 1, "blocked"

    report = bootstrap(Options(venv=tmp_path / "venv", dry_run=True), run=run)
    assert report.ok is False
    assert report.steps[0].status == "failed"
    assert report.next_command
    assert len(report.steps) == 1


def test_skip_addon_marks_it_skipped(tmp_path):
    report = bootstrap(
        Options(venv=tmp_path / "venv", dry_run=True, skip_addon=True), run=Recorder().run
    )
    statuses = {step.name: step.status for step in report.steps}
    assert statuses["addon"] == "skipped"


def test_json_is_machine_readable(tmp_path):
    report = bootstrap(Options(venv=tmp_path / "venv", dry_run=True), run=Recorder().run)
    payload = json.loads(report.to_json())
    assert payload["ok"] is True
    assert payload["version"] == "1.2.0"
    assert [s["name"] for s in payload["steps"]] == STEPS
    assert set(payload) == {"ok", "version", "launcher", "steps", "next_command"}


# --------------------------------------------------------------- deferred-import status


def test_blocked_import_is_reported_as_skipped_not_ok_or_failed(tmp_path):
    """A blocked import must never read as `ok` — that is the exact lie this exists to catch."""

    def run(argv):
        argv = list(argv)
        if "-c" in argv:
            source = argv[argv.index("-c") + 1]
            if "sqlite3" in source:
                return 0, json.dumps({"ok": True, "version": "3.14"})
            if "__import__" in source:
                return 0, json.dumps(
                    {"ok": False, "name": "somepkg", "path": None, "message": "boom"}
                )
        return 0, ""

    report = bootstrap(Options(venv=tmp_path / "venv", dry_run=True), run=run)
    statuses = {step.name: step.status for step in report.steps}
    assert statuses["imports"] == "skipped"
    assert statuses["imports"] != "ok"
    assert statuses["imports"] != "failed"
    assert report.ok is True  # binaries had nothing to repair against a dry run; run continues


# --------------------------------------------------------------- exact argv, real subprocess


def test_addon_step_shells_out_with_yes_and_never_imports_tbmcp(tmp_path):
    """`--yes` is load-bearing: without it `install-addon` calls `input()` and hangs an agent."""
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return 0, "installed"

    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")
    status, _detail = _step_addon(Options(dry_run=False), {"venv_python": venv_python}, run)

    assert status == "ok"
    assert calls == [[venv_python, "-m", "tbmcp", "install-addon", "--yes"]]


def test_clients_step_shells_out_with_requested_clients(tmp_path):
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return 0, "configured"

    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")
    options = Options(dry_run=False, clients=("claude-code", "codex"))
    status, _detail = _step_clients(options, {"venv_python": venv_python}, run)

    assert status == "ok"
    assert calls == [[venv_python, "-m", "tbmcp", "setup", "claude-code", "codex"]]


def test_verify_step_shells_out_to_doctor_json(tmp_path):
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return 0, json.dumps({"ok": True})

    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")
    status, _detail = _step_verify(Options(dry_run=False), {"venv_python": venv_python}, run)

    assert status == "ok"
    assert calls == [[venv_python, "-m", "tbmcp", "doctor", "--json"]]

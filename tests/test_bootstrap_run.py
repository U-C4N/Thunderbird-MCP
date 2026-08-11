# tests/test_bootstrap_run.py
"""The orchestration: fixed step order, idempotent, and honest about failure."""

from __future__ import annotations

import json

from tbmcp.bootstrap import Options, Report, Step, bootstrap

STEPS = ["interpreter", "venv", "install", "imports", "binaries", "launcher",
         "addon", "clients", "verify"]


class Recorder:
    """Every subprocess succeeds; imports work first try."""

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
        return 0, ""


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

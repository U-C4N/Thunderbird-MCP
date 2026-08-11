# tests/test_bootstrap_run.py
"""The orchestration: fixed step order, idempotent, and honest about failure."""

from __future__ import annotations

import json
import sys

import pytest

from tbmcp.bootstrap import (
    Options,
    Report,
    Step,
    _step_addon,
    _step_clients,
    _step_imports,
    _step_verify,
    bootstrap,
)
from tbmcp.bootstrap import venv_python as _venv_python_path

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
    no-op that happens to look fine. That includes `argv[0]`, not just the `-c`
    source — matching only on source content would let a step probe a wrong or
    nonexistent interpreter path and still see a success-shaped payload back, which is
    exactly the failure mode this fake exists to catch. The `interpreter` step tries
    candidate interpreters (here, just `sys.executable`, since the module's own
    interpreter-discovery is neutralised by `_no_stray_interpreters`); `imports` and
    `binaries` must always be called with the venv's python, never the system one.
    """

    def __init__(self, venv_python, system_python=None):
        self.calls: list[list[str]] = []
        self.venv_python = str(venv_python)
        self.system_python = str(system_python or sys.executable)

    def run(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        interpreter = argv[0] if argv else None
        if argv[1:] == ["-m", "tbmcp", "detect-clients"]:
            if interpreter != self.venv_python:
                raise AssertionError(
                    f"detect-clients ran under {interpreter!r}, expected the venv's "
                    f"python {self.venv_python!r}"
                )
            return 0, json.dumps([])
        if "-c" in argv:
            source = argv[argv.index("-c") + 1]
            if "sqlite3" in source:
                if interpreter not in (self.system_python, self.venv_python):
                    raise AssertionError(
                        f"interpreter_ok probed {interpreter!r}, expected the chosen "
                        f"interpreter {self.system_python!r} or the venv's python "
                        f"{self.venv_python!r}"
                    )
                return 0, json.dumps({"ok": True, "version": "3.14"})
            if "__import__" in source:
                if interpreter != self.venv_python:
                    raise AssertionError(
                        f"import probe used {interpreter!r}, expected the venv's "
                        f"python {self.venv_python!r}"
                    )
                return 0, json.dumps({"ok": True})
            if "packages_distributions" in source:
                if interpreter != self.venv_python:
                    raise AssertionError(
                        f"distribution lookup used {interpreter!r}, expected the "
                        f"venv's python {self.venv_python!r}"
                    )
                return 0, json.dumps({"dist": None})
        raise AssertionError(f"Recorder was not built to answer this call: {argv}")


def test_reports_every_step_in_order(tmp_path):
    recorder = Recorder(_venv_python_path(tmp_path / "venv"))
    report = bootstrap(Options(venv=tmp_path / "venv", dry_run=True), run=recorder.run)
    assert [step.name for step in report.steps] == STEPS
    assert report.ok is True
    assert report.next_command is None


def test_dry_run_creates_nothing(tmp_path):
    venv = tmp_path / "venv"
    bootstrap(Options(venv=venv, dry_run=True), run=Recorder(_venv_python_path(venv)).run)
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
    recorder = Recorder(_venv_python_path(tmp_path / "venv"))
    report = bootstrap(
        Options(venv=tmp_path / "venv", dry_run=True, skip_addon=True), run=recorder.run
    )
    statuses = {step.name: step.status for step in report.steps}
    assert statuses["addon"] == "skipped"


def test_json_is_machine_readable(tmp_path):
    recorder = Recorder(_venv_python_path(tmp_path / "venv"))
    report = bootstrap(Options(venv=tmp_path / "venv", dry_run=True), run=recorder.run)
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


# ------------------------------------------------------- interpreter identity, not just argv shape


def test_imports_step_probes_the_venvs_python_not_the_system_one(tmp_path):
    """A step that probed the wrong interpreter must make the fake raise, not succeed quietly.

    `state` carries both the venv's python (what `imports` is supposed to use) and the
    system interpreter (what `interpreter` chose) under different keys, so a step that
    reached for the wrong one would produce a different `argv[0]` — not a `KeyError` —
    and this fake is built to catch exactly that.
    """
    venv_py = str(_venv_python_path(tmp_path / "venv"))
    system_py = sys.executable

    def run(argv):
        argv = list(argv)
        if argv[0] != venv_py:
            raise AssertionError(f"probed {argv[0]!r}, expected the venv's python {venv_py!r}")
        return 0, json.dumps({"ok": True})

    state = {"venv_python": venv_py, "interpreter": system_py}
    status, _detail = _step_imports(Options(dry_run=True), state, run)
    assert status == "ok"


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


# ------------------------------------------------------------ auto-detected clients


def test_clients_step_detects_and_registers_when_none_requested(tmp_path):
    """The headline promise: no `--clients` still registers whatever is installed."""
    calls: list[list[str]] = []

    def run(argv):
        argv = list(argv)
        calls.append(argv)
        if argv[-1] == "detect-clients":
            return 0, json.dumps(["claude-code", "codex"])
        return 0, ""

    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")
    status, detail = _step_clients(Options(dry_run=False), {"venv_python": venv_python}, run)

    assert status == "ok"
    assert "claude-code" in detail and "codex" in detail
    assert calls == [
        [venv_python, "-m", "tbmcp", "detect-clients"],
        [venv_python, "-m", "tbmcp", "setup", "claude-code", "codex"],
    ]


def test_clients_step_skips_not_ok_when_nothing_detected(tmp_path):
    """No `--clients` and nothing found must read as `skipped`, never `ok`."""

    def run(argv):
        argv = list(argv)
        if argv[-1] == "detect-clients":
            return 0, json.dumps([])
        raise AssertionError(f"nothing was detected; must not register anyway: {argv}")

    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")
    status, detail = _step_clients(Options(dry_run=False), {"venv_python": venv_python}, run)

    assert status == "skipped"
    assert status != "ok"
    assert "no" in detail.lower() and "detect" in detail.lower()


def test_clients_step_dry_run_may_detect_but_never_registers(tmp_path):
    calls: list[list[str]] = []

    def run(argv):
        argv = list(argv)
        calls.append(argv)
        if argv[-1] == "detect-clients":
            return 0, json.dumps(["claude-code"])
        raise AssertionError(f"--dry-run must never register anything: {argv}")

    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")
    status, detail = _step_clients(Options(dry_run=True), {"venv_python": venv_python}, run)

    assert status == "ok"
    assert "claude-code" in detail
    assert calls == [[venv_python, "-m", "tbmcp", "detect-clients"]]


def test_clients_step_with_explicit_clients_skips_detection_entirely(tmp_path):
    """An explicit `--clients` must register exactly that, with no detection call at all."""

    def run(argv):
        argv = list(argv)
        if argv[-1] == "detect-clients":
            raise AssertionError("explicit --clients must not trigger detection")
        return 0, "configured"

    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")
    options = Options(dry_run=False, clients=("zed",))
    status, _detail = _step_clients(options, {"venv_python": venv_python}, run)

    assert status == "ok"


def test_verify_step_shells_out_to_doctor_json(tmp_path):
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return 0, json.dumps({"ok": True})

    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")
    status, _detail = _step_verify(Options(dry_run=False), {"venv_python": venv_python}, run)

    assert status == "ok"
    assert calls == [[venv_python, "-m", "tbmcp", "doctor", "--json"]]

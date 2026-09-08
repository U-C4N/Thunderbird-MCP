# tests/test_bootstrap_run.py
"""The orchestration: fixed step order, idempotent, and honest about failure."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

from tbmcp.bootstrap import (
    Options,
    _detected_clients,
    _step_addon,
    _step_clients,
    _step_imports,
    _step_venv,
    _step_verify,
    bootstrap,
)
from tbmcp.bootstrap import venv_python as _venv_python_path

STEPS = [
    "interpreter",
    "venv",
    "install",
    "imports",
    "binaries",
    "launcher",
    "addon",
    "clients",
    "verify",
]


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
    # A dry run establishes nothing — no step actually ran `verify` — so it must
    # never read the same as a real success. See test_dry_run_is_never_reported_ok.
    assert report.ok is False
    assert report.next_command is not None


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


def test_venv_step_fails_when_the_created_interpreter_cannot_load_extensions(tmp_path):
    """I8: `python -m venv` exiting 0 says the tool ran, not that the interpreter it
    produced can load `sqlite3`/`ssl`/`ctypes` — the same gap `interpreter_ok` exists
    to close for candidate selection. A reused venv was already load-tested; a freshly
    created one must be too, not trusted on a bare exit code.
    """
    venv_dir = tmp_path / "venv"
    python_path = _venv_python_path(venv_dir)

    def run(argv):
        argv = list(argv)
        if "-m" in argv and "venv" in argv:
            python_path.parent.mkdir(parents=True, exist_ok=True)
            python_path.write_text("not a real interpreter", encoding="utf-8")
            return 0, ""
        if "-c" in argv:
            # The load test on the interpreter this "created" — deliberately unhealthy.
            return 1, "ImportError: DLL load failed while importing _sqlite3"
        raise AssertionError(f"unexpected call: {argv}")

    state = {"venv": venv_dir, "interpreter": "python", "launcher": None}
    status, detail = _step_venv(Options(dry_run=False), state, run)

    assert status == "failed"
    assert "sqlite3" in detail or "ssl" in detail or "ctypes" in detail


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
    assert payload["ok"] is False  # dry run: see test_dry_run_is_never_reported_ok
    assert payload["version"] == "1.3.0"
    assert [s["name"] for s in payload["steps"]] == STEPS
    assert set(payload) == {"ok", "version", "launcher", "steps", "next_command"}


def test_dry_run_is_never_reported_ok(tmp_path):
    """I1: a dry run must not be byte-shaped identically to a real success in the
    five-key JSON contract an agent parses. `ok` means "verified working"; a dry run
    never runs `verify` (it is always `skipped`), so `ok` must never be `true`,
    no matter how cleanly every individual step reads.
    """
    recorder = Recorder(_venv_python_path(tmp_path / "venv"))
    report = bootstrap(Options(venv=tmp_path / "venv", dry_run=True), run=recorder.run)
    assert report.ok is False
    assert all(step.status in ("ok", "skipped") for step in report.steps)
    statuses = {step.name: step.status for step in report.steps}
    assert statuses["verify"] == "skipped"
    # The next command must be something that actually does the install — not a
    # remedy for a failure that never happened.
    assert report.next_command.startswith("python bootstrap.py")
    assert "--dry-run" not in report.next_command


def test_clean_dry_run_text_does_not_read_as_stopped(tmp_path):
    """New breakage #2: `to_text()` used to print "Stopped. Next: ..." for *every*
    `ok: false` report, so a dry run that did exactly what was asked looked
    byte-for-byte like a genuine failure in the human-readable output too.
    """
    recorder = Recorder(_venv_python_path(tmp_path / "venv"))
    report = bootstrap(Options(venv=tmp_path / "venv", dry_run=True), run=recorder.run)
    assert report.ok is False
    assert not any(step.status == "failed" for step in report.steps)
    text = report.to_text()
    assert "Stopped." not in text
    assert report.next_command in text


def test_failed_dry_run_text_still_says_stopped(tmp_path):
    """The other half: a dry run that genuinely hit a `failed` step must still read
    as stopped, not as a clean dry-run completion."""

    def run(argv):
        return 1, "no usable interpreter"

    report = bootstrap(Options(venv=tmp_path / "venv", dry_run=True), run=run)
    assert report.ok is False
    assert any(step.status == "failed" for step in report.steps)
    text = report.to_text()
    assert "Stopped." in text


def test_real_run_reaching_the_end_is_reported_ok(tmp_path):
    """The flip side of the above: a run that is *not* a dry run, and that hits no
    `failed` step, must still be able to report `ok: true` — the dry-run fix must
    not make every run unconditionally `ok: false`.
    """

    def run(argv):
        argv = list(argv)
        if argv[1:] == ["-m", "tbmcp", "detect-clients"]:
            return 0, json.dumps([])
        if "-c" in argv:
            source = argv[argv.index("-c") + 1]
            if "sqlite3" in source:
                return 0, json.dumps({"ok": True, "version": "3.14"})
            if "__import__" in source:
                return 0, json.dumps({"ok": True})
        return 0, json.dumps({"ok": True})

    report = bootstrap(Options(venv=tmp_path / "venv", dry_run=False, skip_addon=True), run=run)
    assert report.ok is True
    assert report.next_command is None


# --------------------------------------------------------------- deferred-import status


def test_blocked_import_is_reported_as_skipped_not_ok_or_failed(tmp_path):
    """A blocked import must never read as `ok` — that is the exact lie this exists to catch.

    Exercises `_step_imports` directly with `dry_run=False`, so it actually probes
    (see `test_imports_step_does_not_probe_under_dry_run` below for the dry-run
    case, where nothing is probed at all).
    """
    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")

    def run(argv):
        argv = list(argv)
        if "-c" in argv:
            source = argv[argv.index("-c") + 1]
            if "__import__" in source:
                return 0, json.dumps(
                    {"ok": False, "name": "somepkg", "path": None, "message": "boom"}
                )
        return 0, ""

    status, detail = _step_imports(Options(dry_run=False), {"venv_python": venv_python}, run)
    assert status == "skipped"
    assert status != "ok"
    assert status != "failed"
    assert "somepkg" in detail


def test_imports_step_does_not_probe_under_dry_run(tmp_path):
    """I3: nothing was installed under `--dry-run`, so nothing should be probed —
    probing a venv that was never created can only report a failure that has
    nothing to do with the actual package.
    """
    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")

    def run(argv):
        raise AssertionError(f"--dry-run must not probe anything: {argv}")

    status, detail = _step_imports(Options(dry_run=True), {"venv_python": venv_python}, run)
    assert status == "skipped"
    assert "venv" in detail.lower()


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
    status, _detail = _step_imports(Options(dry_run=False), state, run)
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


def test_detected_clients_returns_none_on_nonzero_exit():
    """`None` — could not run — not `()` — ran and found nothing. See I2: conflating
    the two let `_step_clients` claim a fact about the machine it never checked."""

    def run(argv):
        return 1, "detect-clients crashed"

    assert _detected_clients("python", run) is None


def test_detected_clients_returns_none_on_empty_output():
    def run(argv):
        return 0, ""

    assert _detected_clients("python", run) is None


def test_detected_clients_returns_none_on_malformed_json():
    def run(argv):
        return 0, "[this is not json"

    assert _detected_clients("python", run) is None


def test_detected_clients_returns_empty_tuple_on_a_genuine_empty_list():
    """The one case that really is "ran cleanly, found nothing": a well-formed `[]`."""

    def run(argv):
        return 0, json.dumps([])

    assert _detected_clients("python", run) == ()


def test_detected_clients_skips_junk_lines_before_the_json():
    def run(argv):
        return 0, 'warning: something noisy\nDeprecationWarning: whatever\n["zed"]'

    assert _detected_clients("python", run) == ("zed",)


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
    """I1: a dry run that detected something real still established nothing —
    `skipped`, not `ok`, the same status the project already uses everywhere else
    for a deferred outcome.
    """
    calls: list[list[str]] = []

    def run(argv):
        argv = list(argv)
        calls.append(argv)
        if argv[-1] == "detect-clients":
            return 0, json.dumps(["claude-code"])
        raise AssertionError(f"--dry-run must never register anything: {argv}")

    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")
    status, detail = _step_clients(Options(dry_run=True), {"venv_python": venv_python}, run)

    assert status == "skipped"
    assert status != "ok"
    assert "claude-code" in detail
    assert calls == [[venv_python, "-m", "tbmcp", "detect-clients"]]


def test_clients_step_says_it_could_not_check_when_detection_cannot_run(tmp_path):
    """I2: when detection cannot run at all (a nonexistent venv python under
    `--dry-run`, before any venv exists, shells out to `FileNotFoundError` and a
    non-zero exit), the step must say that detection did not run — never assert a
    fact about the machine ("no MCP clients detected") that it never checked.
    """

    def run(argv):
        return 127, "FileNotFoundError: no such file"

    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")
    status, detail = _step_clients(Options(dry_run=True), {"venv_python": venv_python}, run)

    assert status == "skipped"
    assert "no mcp clients detected" not in detail.lower()
    assert "could not" in detail.lower()


def test_clients_step_in_a_real_run_reports_a_crash_not_a_missing_venv(tmp_path):
    """New breakage #5: by the time `clients` runs in a real (non-dry-run) bootstrap,
    `venv` and `install` have already reported `ok` — a working venv necessarily
    exists. If `detect-clients` still comes back unusable here, that is because it
    crashed or produced junk, not because there was "no working venv to run it in
    yet" (that phrasing is true only under `--dry-run`, before any venv is built).
    """

    def run(argv):
        return 1, "Traceback (most recent call last): ... ImportError"

    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")
    status, detail = _step_clients(Options(dry_run=False), {"venv_python": venv_python}, run)

    assert status == "skipped"
    assert "no working venv" not in detail.lower()
    assert "could not" in detail.lower()


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


def test_verify_step_fails_when_doctor_exits_zero_but_reports_not_ok(tmp_path):
    """C1: `doctor --json` exiting 0 must not be enough on its own. This is exactly
    the bug that let `verify` certify a server that never worked — `doctor --json`
    used to always exit 0 regardless of what it found, and `_step_verify` trusted
    only the exit code.
    """

    def run(argv):
        # A real subprocess exiting 0 while its own report says the chain is broken —
        # the state the pre-fix `cmd_doctor --json` always produced.
        return 0, json.dumps({"ok": False, "bridge": {"connected": False}})

    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")
    status, _detail = _step_verify(Options(dry_run=False), {"venv_python": venv_python}, run)

    assert status == "failed"
    assert status != "ok"


def test_verify_step_fails_when_doctor_output_is_not_json(tmp_path):
    def run(argv):
        return 0, "not json at all"

    venv_python = str(tmp_path / "venv" / "Scripts" / "python.exe")
    status, _detail = _step_verify(Options(dry_run=False), {"venv_python": venv_python}, run)
    assert status == "failed"


# --------------------------------------------------------------------- next_command


def test_remedy_for_binaries_names_a_real_distribution_and_venv_pip():
    """C3: the remedy must name the resolved distribution (never the raw module),
    a real version bound (never a `<older>` placeholder), and the venv's own pip
    (never bare `pip`, which is not on PATH here).
    """
    from tbmcp.bootstrap import ImportFailure, _remedy

    venv_python = r"C:\venv\Scripts\python.exe"
    state = {
        "venv": pathlib.Path(r"C:\venv"),
        "venv_python": venv_python,
        "binaries_failure": ImportFailure(
            module="_cffi_backend", path=None, message="blocked", dist="cffi", floor="2.1.0"
        ),
    }
    command = _remedy("binaries", "detail", Options(), state)

    assert "cffi" in command
    assert "_cffi_backend" not in command
    assert "<older>" not in command
    assert venv_python in command
    assert not command.startswith("pip ")


def test_remedy_for_binaries_without_a_resolved_distribution_does_not_fabricate_one():
    """When no distribution could be resolved at all (an empty-module failure),
    the remedy must not invent a fake `target==<older>` command — there is nothing
    real to name."""
    from tbmcp.bootstrap import ImportFailure, _remedy

    venv_python = r"C:\venv\Scripts\python.exe"
    state = {
        "venv": pathlib.Path(r"C:\venv"),
        "venv_python": venv_python,
        "binaries_failure": ImportFailure(module="", path=None, message="probe exited 1: "),
    }
    command = _remedy("binaries", "detail", Options(), state)

    assert "<older>" not in command
    assert "target" not in command
    assert venv_python in command


def test_remedy_for_addon_clients_verify_uses_the_venv_python_not_bare_tbmcp():
    """C4: the fall-through default used to be the bare string `tbmcp doctor` —
    unrunnable, since the bootstrap venv is never added to PATH. It must name the
    venv's own python explicitly, same as the fix already applied to `venv`/`install`.
    """
    from tbmcp.bootstrap import _remedy

    venv_python = r"C:\venv\Scripts\python.exe"
    state = {"venv": pathlib.Path(r"C:\venv"), "venv_python": venv_python}
    for step_name in ("addon", "clients", "verify"):
        command = _remedy(step_name, "some failure detail", Options(), state)
        assert command.startswith('"' + venv_python), command
        assert not command.startswith("tbmcp ")


@pytest.mark.parametrize(
    "step_name", ["interpreter", "venv", "install", "binaries", "addon", "clients", "verify"]
)
def test_no_remedy_ever_names_a_bare_tbmcp_or_pip(step_name):
    """The property the review asked for directly: whatever failed, the single next
    command must never start with a bare `tbmcp` or bare `pip` — neither is on PATH
    in the state that emits a `next_command` at all."""
    from tbmcp.bootstrap import ImportFailure, _remedy

    venv_python = r"C:\venv\Scripts\python.exe"
    state = {
        "venv": pathlib.Path(r"C:\venv"),
        "interpreter": r"C:\Python314\python.exe",
        "venv_python": venv_python,
        "binaries_failure": ImportFailure(
            module="_cffi_backend", path=None, message="x", dist="cffi", floor="1.0.0"
        ),
    }
    command = _remedy(step_name, "some failure detail", Options(), state)
    assert not command.startswith("tbmcp ")
    assert not command.startswith("pip ")
    assert command != "tbmcp doctor"


# ------------------------------------------------------------------- I5: no unhandled crash


def test_a_steps_own_bug_still_produces_valid_json_not_a_traceback():
    """I5: a step raising something other than `BootstrapError` (an `OSError`,
    `KeyError`, whatever) used to escape `bootstrap()` entirely, so `--json` gave no
    JSON at all — the one output contract a calling agent depends on.
    """

    def broken_run(argv):
        raise KeyError("boom")

    report = bootstrap(Options(venv=pathlib.Path(r"C:\venv"), dry_run=False), run=broken_run)
    assert report.ok is False
    assert report.steps[0].status == "failed"
    assert "KeyError" in report.steps[0].detail
    # Must still serialise: this is the actual contract check.
    payload = json.loads(report.to_json())
    assert set(payload) == {"ok", "version", "launcher", "steps", "next_command"}

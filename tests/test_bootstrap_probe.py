"""The probe reports which module failed, taken from the exception, not its text.

The Turkish tail in these fixtures is verbatim from the machine this was built for:
matching on OS text would have worked in English and nowhere else.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from tbmcp.bootstrap import ImportFailure, probe_import

BLOCKED_PATH = r"C:\venv\Lib\site-packages\_cffi_backend.cp314-win_amd64.pyd"
BLOCKED_MESSAGE = (
    "DLL load failed while importing _cffi_backend: Uygulama Denetimi ilkesi bu dosyayi engelledi."
)
BLOCKED = json.dumps(
    {"ok": False, "name": "_cffi_backend", "path": BLOCKED_PATH, "message": BLOCKED_MESSAGE}
)


def test_success_returns_none():
    def run(argv):
        return 0, json.dumps({"ok": True})

    assert probe_import("python", run=run) is None


def test_failure_reports_module_and_path():
    def run(argv):
        return 0, BLOCKED

    failure = probe_import("python", run=run)
    assert failure == ImportFailure(
        module="_cffi_backend", path=BLOCKED_PATH, message=BLOCKED_MESSAGE
    )


def test_probe_targets_the_requested_module():
    seen = {}

    def run(argv):
        seen["argv"] = list(argv)
        return 0, json.dumps({"ok": True})

    probe_import("/usr/bin/python3", target="tbmcp.daemon", run=run)
    assert seen["argv"][0] == "/usr/bin/python3"
    assert "tbmcp.daemon" in seen["argv"]


def test_unparseable_output_is_a_failure_not_a_crash():
    def run(argv):
        return 1, "Traceback (most recent call last): ..."

    failure = probe_import("python", run=run)
    assert failure is not None
    assert failure.module == ""
    assert "Traceback" in failure.message


# ------------------------------------------------------------- deferred minors 2/3/4


def test_empty_output_carries_the_exit_status():
    """Deferred minor 2: no JSON at all (the probe crashed silently) must not read
    as a blank, unattributable failure — the exit status is the only signal left,
    and losing it is what let `_step_binaries` build the unrunnable remedy in C3
    (an empty `module` fed straight into the old `_remedy("binaries", ...)`).
    """

    def run(argv):
        return 137, ""

    failure = probe_import("python", run=run)
    assert failure is not None
    assert failure.module == ""
    assert "137" in failure.message


def test_valid_json_preceded_by_junk_lines_is_still_parsed():
    """Deferred minor 4: `_detected_clients` already had this coverage (four cases);
    `probe_import` only had the plain-non-JSON case. Warnings printed to stdout
    before the probe's own JSON line must not break parsing.
    """

    def run(argv):
        return 0, "site-packages warning: noisy\nDeprecationWarning: whatever\n" + BLOCKED

    failure = probe_import("python", run=run)
    assert failure == ImportFailure(
        module="_cffi_backend", path=BLOCKED_PATH, message=BLOCKED_MESSAGE
    )


def test_systemexit_during_import_is_reported_not_swallowed(tmp_path):
    """Deferred minor 3: a module that calls `sys.exit()` at import time (a C
    extension, a broken `sitecustomize.py`) must not leave the probe silent.
    `_PROBE` used to only catch `ImportError`/`Exception`; `SystemExit` derives
    from `BaseException` alone, so it escaped uncaught, the subprocess printed no
    JSON, and `probe_import` fell through to an empty, unattributable failure —
    exactly the state `_step_binaries` cannot build a remedy for (C3).

    Runs the real `_PROBE` script against a real subprocess, not a fake `run` — the
    bug lived inside the probe script's own exception handling, which a fake `run`
    cannot exercise.
    """
    (tmp_path / "explodes_at_import.py").write_text("raise SystemExit(3)\n", encoding="utf-8")

    def run(argv):
        env = dict(os.environ, PYTHONPATH=str(tmp_path))
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    failure = probe_import(sys.executable, target="explodes_at_import", run=run)
    assert failure is not None
    assert failure.module == ""  # SystemExit carries no import name
    assert "SystemExit" in failure.message

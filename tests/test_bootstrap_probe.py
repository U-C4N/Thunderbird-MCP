"""The probe reports which module failed, taken from the exception, not its text.

The Turkish tail in these fixtures is verbatim from the machine this was built for:
matching on OS text would have worked in English and nowhere else.
"""

from __future__ import annotations

import json

from tbmcp.bootstrap import ImportFailure, probe_import

BLOCKED_PATH = r"C:\venv\Lib\site-packages\_cffi_backend.cp314-win_amd64.pyd"
BLOCKED_MESSAGE = (
    "DLL load failed while importing _cffi_backend: "
    "Uygulama Denetimi ilkesi bu dosyayi engelledi."
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

# tests/test_bootstrap_repair.py
"""Repair walks a distribution's version down until the import works, or gives up."""

from __future__ import annotations

import json

from tbmcp.bootstrap import Repair, repair_imports, write_constraints


class FakeEnv:
    """A pretend interpreter: cffi is blocked until it drops below `unblocks_below`."""

    def __init__(self, unblocks_below: str | None = "2.1.0", versions=("2.1.1", "2.1.0", "2.0.0")):
        self.versions = list(versions)
        self.current = self.versions[0]
        self.unblocks_below = unblocks_below
        self.installs: list[str] = []

    def _blocked(self) -> bool:
        if self.unblocks_below is None:
            return True
        return self.versions.index(self.current) <= self.versions.index(self.unblocks_below) - 1

    def run(self, argv):
        argv = list(argv)
        if "-c" in argv and "import json, sys" in argv[argv.index("-c") + 1]:
            if self._blocked():
                return 0, json.dumps(
                    {"ok": False, "name": "_cffi_backend", "path": "/x/_cffi_backend.pyd",
                     "message": "DLL load failed while importing _cffi_backend: engellendi"}
                )
            return 0, json.dumps({"ok": True})
        if "packages_distributions" in " ".join(argv):
            return 0, json.dumps({"dist": "cffi"})
        if "PackageNotFoundError" in " ".join(argv):
            return 0, json.dumps({"version": self.current})
        if "install" in argv:
            spec = argv[-1]
            self.installs.append(spec)
            ceiling = spec.split("<")[1]
            lower = [v for v in self.versions if self.versions.index(v) > self.versions.index(ceiling)]
            if not lower:
                return 1, "ERROR: no matching distribution"
            self.current = lower[0]
            return 0, f"Successfully installed cffi-{self.current}"
        return 0, ""


def test_downgrades_until_the_import_works():
    env = FakeEnv(unblocks_below="2.1.0")
    repairs, failure = repair_imports("python", run=env.run)
    assert failure is None
    assert repairs == [Repair(dist="cffi", from_version="2.1.1", to_version="2.1.0")]


def test_gives_up_and_names_the_module():
    env = FakeEnv(unblocks_below=None)
    repairs, failure = repair_imports("python", run=env.run, max_attempts=3)
    assert failure is not None
    assert failure.module == "_cffi_backend"
    assert len(env.installs) <= 3


def test_healthy_environment_is_untouched():
    env = FakeEnv(unblocks_below="2.1.1")
    repairs, failure = repair_imports("python", run=env.run)
    assert (repairs, failure, env.installs) == ([], None, [])


def test_constraints_record_the_repaired_versions(tmp_path):
    written = write_constraints(tmp_path, [Repair("cffi", "2.1.1", "2.0.0")])
    assert written is not None
    assert written.read_text(encoding="utf-8").strip() == "cffi==2.0.0"


def test_no_repairs_writes_no_constraints(tmp_path):
    assert write_constraints(tmp_path, []) is None

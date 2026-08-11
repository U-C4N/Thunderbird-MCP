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
                    {
                        "ok": False,
                        "name": "_cffi_backend",
                        "path": "/x/_cffi_backend.pyd",
                        "message": "DLL load failed while importing _cffi_backend: engellendi",
                    }
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
            lower = [
                v for v in self.versions if self.versions.index(v) > self.versions.index(ceiling)
            ]
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
    """`max_attempts=3` must be what stops the walk, not the fake running out of
    releases to offer. The default `FakeEnv` only carries three versions, so pip
    exhausts its own releases at the same point the bound would bite anyway — raise
    `max_attempts` to 99 against that fixture and this test still passes, which
    means it was never defending the bound at all. Six versions and an exact count
    close that gap: with `unblocks_below=None` every one of the first three
    downgrades succeeds, so only the bound — not a `pip install` failure — can be
    why a fourth is never attempted.
    """
    env = FakeEnv(
        unblocks_below=None,
        versions=("2.5.0", "2.4.0", "2.3.0", "2.2.0", "2.1.0", "2.0.0"),
    )
    _repairs, failure = repair_imports("python", run=env.run, max_attempts=3)
    assert failure is not None
    assert failure.module == "_cffi_backend"
    assert failure.dist == "cffi"
    assert len(env.installs) == 3


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


class TwoDistFakeEnv:
    """cffi blocks first; once cffi clears, a second distribution blocks in its place."""

    def __init__(self):
        self.state = {
            "cffi": {"module": "_cffi_backend", "versions": ["2.1.1", "2.1.0"]},
            "second": {"module": "_second_backend", "versions": ["1.5.0", "1.4.0"]},
        }
        self.current = {"cffi": "2.1.1", "second": "1.5.0"}
        self.installs: list[str] = []

    def _blocked_dist(self):
        for dist, meta in self.state.items():
            if self.current[dist] != meta["versions"][-1]:
                return dist
        return None

    def run(self, argv):
        argv = list(argv)
        if "-c" in argv and "import json, sys" in argv[argv.index("-c") + 1]:
            blocked = self._blocked_dist()
            if blocked is None:
                return 0, json.dumps({"ok": True})
            module = self.state[blocked]["module"]
            return 0, json.dumps(
                {
                    "ok": False,
                    "name": module,
                    "path": f"/x/{module}.pyd",
                    "message": f"DLL load failed while importing {module}: engellendi",
                }
            )
        if "packages_distributions" in " ".join(argv):
            module = argv[-1]
            for dist, meta in self.state.items():
                if meta["module"] == module:
                    return 0, json.dumps({"dist": dist})
            return 0, json.dumps({"dist": None})
        if "PackageNotFoundError" in " ".join(argv):
            dist = argv[-1]
            return 0, json.dumps({"version": self.current.get(dist)})
        if "install" in argv:
            spec = argv[-1]
            self.installs.append(spec)
            dist, ceiling = spec.split("<")
            versions = self.state[dist]["versions"]
            lower = [v for v in versions if versions.index(v) > versions.index(ceiling)]
            if not lower:
                return 1, "ERROR: no matching distribution"
            self.current[dist] = lower[0]
            return 0, f"Successfully installed {dist}-{self.current[dist]}"
        return 0, ""


def test_continues_to_a_second_distribution_after_fixing_the_first(tmp_path):
    env = TwoDistFakeEnv()
    repairs, failure = repair_imports("python", run=env.run)
    assert failure is None
    assert repairs == [
        Repair(dist="cffi", from_version="2.1.1", to_version="2.1.0"),
        Repair(dist="second", from_version="1.5.0", to_version="1.4.0"),
    ]
    written = write_constraints(tmp_path, repairs)
    assert written.read_text(encoding="utf-8") == "cffi==2.1.0\nsecond==1.4.0\n"


class ChainFakeEnv:
    """Each distribution's single downgrade "fixes" it but exposes the next one blocked.

    None of them ever get the import fully working — used to prove the distinct-
    distribution bound stops the walk instead of chasing the chain forever.
    """

    def __init__(self, chain=("d1", "d2", "d3", "d4", "d5")):
        self.chain = list(chain)
        self.modules = {dist: f"_{dist}_backend" for dist in self.chain}
        self.fixed: set[str] = set()
        self.installs: list[str] = []

    def _blocked(self):
        for dist in self.chain:
            if dist not in self.fixed:
                return dist
        return None

    def run(self, argv):
        argv = list(argv)
        if "-c" in argv and "import json, sys" in argv[argv.index("-c") + 1]:
            blocked = self._blocked()
            if blocked is None:
                return 0, json.dumps({"ok": True})
            module = self.modules[blocked]
            return 0, json.dumps(
                {
                    "ok": False,
                    "name": module,
                    "path": f"/x/{module}.pyd",
                    "message": f"DLL load failed while importing {module}: engellendi",
                }
            )
        if "packages_distributions" in " ".join(argv):
            module = argv[-1]
            for dist, mod in self.modules.items():
                if mod == module:
                    return 0, json.dumps({"dist": dist})
            return 0, json.dumps({"dist": None})
        if "PackageNotFoundError" in " ".join(argv):
            dist = argv[-1]
            version = "1.0.0" if dist in self.fixed else "2.0.0"
            return 0, json.dumps({"version": version})
        if "install" in argv:
            spec = argv[-1]
            self.installs.append(spec)
            dist = spec.split("<")[0]
            self.fixed.add(dist)
            return 0, f"Successfully installed {dist}-1.0.0"
        return 0, ""


def test_max_dists_bound_stops_the_walk():
    env = ChainFakeEnv(chain=("d1", "d2", "d3", "d4", "d5"))
    repairs, failure = repair_imports("python", run=env.run, max_dists=3)
    assert failure is not None
    assert failure.module == "_d4_backend"
    assert [repair.dist for repair in repairs] == ["d1", "d2", "d3"]
    assert len(env.installs) == 3

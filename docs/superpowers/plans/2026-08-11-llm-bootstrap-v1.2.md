# Agent Bootstrap (v1.2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One command takes a clean machine from checkout to a verified working Thunderbird MCP server, repairing environment traps instead of failing five layers away from them.

**Architecture:** A new stdlib-only `src/tbmcp/bootstrap.py` runs before the package is importable. It picks a working interpreter, builds a venv, installs, then probes the import in a subprocess; a failed extension load is identified by `ImportError.name`/`.path` and repaired by walking that distribution's version down. Existing `install-addon`, `setup`, and `doctor` are wrapped, not reimplemented. A separate one-line fix in `clients.py` makes launcher selection test the console script by running it.

**Tech Stack:** Python 3.11+, argparse, `subprocess`, `venv`, `importlib.metadata`, pytest.

## Global Constraints

- `src/tbmcp/bootstrap.py` imports **only** the standard library. No import from `tbmcp.*` at module scope. Enforced by a test.
- Version is **1.2.0** in all three places: `pyproject.toml`, `addon/manifest.json`, `src/tbmcp/server.py` `_version()` fallback.
- Never match on OS error text. Identify failed imports by `ImportError.name` and `.path`.
- Repair bounds: **3** downgrade attempts per distribution, **3** distinct distributions per run.
- Every subprocess call goes through an injected `run` callable typed `Callable[[Sequence[str]], tuple[int, str]]` so tests never spawn processes.
- Step statuses are exactly `ok`, `repaired`, `skipped`, `failed`.
- Install source for v1.2 is git: `git+https://github.com/U-C4N/Thunderbird-MCP`. Not PyPI.
- Follow the existing house style: module docstrings explaining *why*, `from __future__ import annotations`, no comment restating the code.

---

### Task 1: Unify the version at 1.2.0

**Files:**
- Modify: `pyproject.toml:7`
- Modify: `addon/manifest.json:4`
- Modify: `src/tbmcp/server.py:178`
- Test: `tests/test_version.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `tbmcp.server._version() -> str` returning `"1.2.0"` when metadata is absent; `tbmcp.addon_build.addon_version()` returning `"1.2.0"` (existing function, unchanged signature).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_version.py
"""The three version strings drift apart the moment nothing checks them."""

from __future__ import annotations

import json
import pathlib
import tomllib

from tbmcp import server

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXPECTED = "1.2.0"


def _pyproject_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def _manifest_version() -> str:
    return json.loads((ROOT / "addon" / "manifest.json").read_text(encoding="utf-8"))["version"]


def test_all_three_versions_agree():
    assert _pyproject_version() == EXPECTED
    assert _manifest_version() == EXPECTED
    assert server._version() == EXPECTED


def test_fallback_matches_packaging(monkeypatch):
    """With the distribution missing, the hardcoded fallback must still be right."""

    def explode(_name: str) -> str:
        raise RuntimeError("not installed")

    monkeypatch.setattr("importlib.metadata.version", explode)
    assert server._version() == EXPECTED
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_version.py -v`
Expected: FAIL — `assert '0.1.0' == '1.2.0'`.

- [ ] **Step 3: Set the version in all three files**

`pyproject.toml` line 7: `version = "1.2.0"`
`addon/manifest.json` line 4: `"version": "1.2.0",`
`src/tbmcp/server.py` in `_version()`: replace the `return "0.1.0.dev0"` fallback with `return "1.2.0"`.

- [ ] **Step 4: Run the test and the existing add-on build test**

Run: `.venv/Scripts/python.exe -m pytest tests/test_version.py tests/test_addon_build.py -v`
Expected: PASS. `test_addon_build.py` reads the manifest, so it catches a malformed edit.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml addon/manifest.json src/tbmcp/server.py tests/test_version.py
git commit -m "chore: unify package and add-on version at 1.2.0"
```

---

### Task 2: Import probe

**Files:**
- Create: `src/tbmcp/bootstrap.py`
- Test: `tests/test_bootstrap_probe.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `RunResult = tuple[int, str]`
  - `run_capture(argv: Sequence[str], *, timeout: float = 120.0) -> RunResult`
  - `@dataclass(frozen=True) ImportFailure(module: str, path: str | None, message: str)`
  - `probe_import(python: str, target: str = "tbmcp.server", *, run: Runner = run_capture) -> ImportFailure | None` — `None` means the import succeeded.
  - `Runner = Callable[[Sequence[str]], RunResult]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bootstrap_probe.py
"""The probe reports which module failed, taken from the exception, not its text.

The Turkish tail in these fixtures is verbatim from the machine this was built for:
matching on OS text would have worked in English and nowhere else.
"""

from __future__ import annotations

import json

from tbmcp.bootstrap import ImportFailure, probe_import

BLOCKED = json.dumps(
    {
        "ok": False,
        "name": "_cffi_backend",
        "path": r"C:\venv\Lib\site-packages\_cffi_backend.cp314-win_amd64.pyd",
        "message": "DLL load failed while importing _cffi_backend: "
        "Uygulama Denetimi ilkesi bu dosyayi engelledi.",
    }
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
        module="_cffi_backend",
        path=r"C:\venv\Lib\site-packages\_cffi_backend.cp314-win_amd64.pyd",
        message=BLOCKED and json.loads(BLOCKED)["message"],
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
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bootstrap_probe.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tbmcp.bootstrap'`.

- [ ] **Step 3: Write the module**

```python
# src/tbmcp/bootstrap.py
"""Bring a machine from a checkout to a working server, repairing what it finds.

Standard library only, and no import from the rest of `tbmcp`: this file has to run
on an interpreter where the package cannot be imported yet, which is the whole point
of it. A subcommand cannot repair a state in which its own package will not load.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

RunResult = tuple[int, str]
Runner = Callable[[Sequence[str]], RunResult]


def run_capture(argv: Sequence[str], *, timeout: float = 120.0) -> RunResult:
    """Run `argv`, returning its exit status and combined output.

    Output is merged because every consumer here either parses a single JSON line or
    shows the whole thing to a human; keeping the streams apart would only mean
    stitching them back together to explain a failure.
    """
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:g}s"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


# Run inside the target interpreter. `ImportError` carries `name` and `path`, set by
# the loader before the OS message is formatted, so neither the platform nor the
# system locale can change what we read.
_PROBE = """
import json, sys
try:
    __import__(sys.argv[1])
except ImportError as exc:
    print(json.dumps({"ok": False, "name": exc.name or "", "path": exc.path,
                      "message": str(exc)}))
except Exception as exc:
    print(json.dumps({"ok": False, "name": "", "path": None,
                      "message": f"{type(exc).__name__}: {exc}"}))
else:
    print(json.dumps({"ok": True}))
"""


@dataclass(frozen=True)
class ImportFailure:
    """A module that would not load, and the file the loader was reading."""

    module: str
    path: str | None
    message: str


def probe_import(
    python: str,
    target: str = "tbmcp.server",
    *,
    run: Runner = run_capture,
) -> ImportFailure | None:
    """Import `target` under `python`. `None` means it worked."""
    _status, output = run([python, "-c", _PROBE, target])
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if payload.get("ok"):
            return None
        return ImportFailure(
            module=payload.get("name") or "",
            path=payload.get("path"),
            message=payload.get("message") or "",
        )
    return ImportFailure(module="", path=None, message=output.strip())
```

- [ ] **Step 4: Run the test**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bootstrap_probe.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Verify against the real thing**

Run: `.venv/Scripts/python.exe -c "from tbmcp.bootstrap import probe_import; print(probe_import(r'.venv\Scripts\python.exe'))"`
Expected: `None` — the working venv imports cleanly. This proves the probe agrees with reality, not just with its fixtures.

- [ ] **Step 6: Commit**

```bash
git add src/tbmcp/bootstrap.py tests/test_bootstrap_probe.py
git commit -m "feat: import probe reporting the module that failed to load"
```

---

### Task 3: Downgrade repair

**Files:**
- Modify: `src/tbmcp/bootstrap.py`
- Test: `tests/test_bootstrap_repair.py`

**Interfaces:**
- Consumes: `probe_import`, `ImportFailure`, `Runner`, `run_capture` from Task 2.
- Produces:
  - `@dataclass(frozen=True) Repair(dist: str, from_version: str, to_version: str)`
  - `distribution_for(python: str, module: str, *, run: Runner = run_capture) -> str | None`
  - `installed_version(python: str, dist: str, *, run: Runner = run_capture) -> str | None`
  - `repair_imports(python: str, target: str = "tbmcp.server", *, run: Runner = run_capture, max_attempts: int = 3, max_dists: int = 3) -> tuple[list[Repair], ImportFailure | None]`
  - `write_constraints(venv_dir: pathlib.Path, repairs: Sequence[Repair]) -> pathlib.Path | None`

- [ ] **Step 1: Write the failing test**

```python
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
        if "--version-of" in argv:
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
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bootstrap_repair.py -v`
Expected: FAIL — `ImportError: cannot import name 'Repair'`.

- [ ] **Step 3: Implement repair**

Append to `src/tbmcp/bootstrap.py`:

```python
_DIST_OF = """
import json, sys
from importlib.metadata import packages_distributions
dists = packages_distributions().get(sys.argv[1]) or []
print(json.dumps({"dist": dists[0] if dists else None}))
"""

_VERSION_OF = """
import json, sys
from importlib.metadata import PackageNotFoundError, version
try:
    print(json.dumps({"version": version(sys.argv[1])}))
except PackageNotFoundError:
    print(json.dumps({"version": None}))
"""


@dataclass(frozen=True)
class Repair:
    dist: str
    from_version: str
    to_version: str


def _json_field(output: str, key: str):
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line).get(key)
            except ValueError:
                continue
    return None


def distribution_for(python: str, module: str, *, run: Runner = run_capture) -> str | None:
    """Which installed distribution provides `module`."""
    _status, output = run([python, "-c", _DIST_OF, module])
    return _json_field(output, "dist")


def installed_version(python: str, dist: str, *, run: Runner = run_capture) -> str | None:
    _status, output = run([python, "-c", _VERSION_OF, "--version-of", dist][:3] + [dist])
    return _json_field(output, "version")


def repair_imports(
    python: str,
    target: str = "tbmcp.server",
    *,
    run: Runner = run_capture,
    max_attempts: int = 3,
    max_dists: int = 3,
) -> tuple[list[Repair], ImportFailure | None]:
    """Walk offending distributions down a release at a time until `target` imports.

    `pip install "dist<current"` resolves to the next release below without us having
    to enumerate the index — one less thing to keep current, and it works against a
    private mirror too.
    """
    repairs: list[Repair] = []
    handled: set[str] = set()

    failure = probe_import(python, target, run=run)
    while failure is not None:
        if not failure.module:
            return repairs, failure
        dist = distribution_for(python, failure.module, run=run)
        if dist is None or dist in handled or len(handled) >= max_dists:
            return repairs, failure
        handled.add(dist)

        started_at = installed_version(python, dist, run=run)
        current = started_at
        for _attempt in range(max_attempts):
            if current is None:
                return repairs, failure
            status, _output = run([python, "-m", "pip", "install", "--quiet", f"{dist}<{current}"])
            if status != 0:
                return repairs, failure
            current = installed_version(python, dist, run=run)
            failure = probe_import(python, target, run=run)
            if failure is None or failure.module not in ("", *handled):
                break
        if started_at is not None and current is not None and current != started_at:
            repairs.append(Repair(dist=dist, from_version=started_at, to_version=current))
        if failure is not None and failure.module and distribution_for(
            python, failure.module, run=run
        ) == dist:
            return repairs, failure

    return repairs, None


def write_constraints(venv_dir: pathlib.Path, repairs: Sequence[Repair]) -> pathlib.Path | None:
    """Pin what the repair settled on, so a later reinstall cannot undo it."""
    if not repairs:
        return None
    path = venv_dir / "constraints.txt"
    path.write_text(
        "".join(f"{repair.dist}=={repair.to_version}\n" for repair in repairs),
        encoding="utf-8",
    )
    return path
```

Add `import pathlib` to the imports at the top of the file.

- [ ] **Step 4: Run the test**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bootstrap_repair.py -v`
Expected: PASS, 5 tests. If `installed_version`'s argv slicing reads awkwardly, simplify it to `run([python, "-c", _VERSION_OF, dist])` and update the fake's `--version-of` branch to match on `_VERSION_OF`'s content instead.

- [ ] **Step 5: Commit**

```bash
git add src/tbmcp/bootstrap.py tests/test_bootstrap_repair.py
git commit -m "feat: repair blocked imports by walking the distribution version down"
```

---

### Task 4: Interpreter selection

**Files:**
- Modify: `src/tbmcp/bootstrap.py`
- Test: `tests/test_bootstrap_interpreter.py`

**Interfaces:**
- Consumes: `Runner`, `run_capture`.
- Produces:
  - `UV_ROOT_MARKERS: tuple[str, ...]`
  - `rank_interpreters(paths: Sequence[str]) -> list[str]` — stable order, uv-managed last, duplicates removed.
  - `interpreter_ok(python: str, *, run: Runner = run_capture) -> bool`
  - `candidate_interpreters() -> list[str]`
  - `choose_interpreter(explicit: str | None = None, *, run: Runner = run_capture, candidates: Sequence[str] | None = None) -> str` — raises `BootstrapError` when nothing passes.
  - `class BootstrapError(RuntimeError)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bootstrap_interpreter.py
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
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bootstrap_interpreter.py -v`
Expected: FAIL — `cannot import name 'BootstrapError'`.

- [ ] **Step 3: Implement selection**

Append to `src/tbmcp/bootstrap.py`:

```python
class BootstrapError(RuntimeError):
    """Something bootstrap cannot repair on its own."""


# Not an exclusion list. A uv-managed interpreter is fine on a machine with no
# enforcing policy, and dropping it outright would break the common case to serve
# the rare one; it just goes to the back of the queue.
UV_ROOT_MARKERS = ("uv/python", "uv\\python")

_LOAD_TEST = """
import json, sqlite3, ssl, ctypes, sysconfig
print(json.dumps({"ok": True, "version": sysconfig.get_python_version()}))
"""


def rank_interpreters(paths: Sequence[str]) -> list[str]:
    seen: dict[str, None] = {}
    for path in paths:
        seen.setdefault(path, None)
    ordered = list(seen)
    return sorted(ordered, key=lambda path: any(m in path.lower() for m in UV_ROOT_MARKERS))


def interpreter_ok(python: str, *, run: Runner = run_capture) -> bool:
    """Can this interpreter actually load the extension modules the server needs?

    Signatures and OS policy queries both lie — one is platform-specific and the
    other reports intent rather than outcome. Loading the modules does not.
    """
    status, output = run([python, "-c", _LOAD_TEST])
    return status == 0 and '"ok": true' in output.lower()


def candidate_interpreters() -> list[str]:
    candidates = [sys.executable]
    if os.name == "nt":
        for minor in ("3.13", "3.12", "3.11"):
            candidates.append(f"py -{minor}")
        for minor in ("314", "313", "312", "311"):
            candidates.append(rf"C:\Python{minor}\python.exe")
    for name in ("python3.13", "python3.12", "python3.11", "python3", "python"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    return rank_interpreters([c for c in candidates if c])


def choose_interpreter(
    explicit: str | None = None,
    *,
    run: Runner = run_capture,
    candidates: Sequence[str] | None = None,
) -> str:
    """The first interpreter that passes the load test.

    An explicit `--python` is tried first but not trusted: being asked for is not
    evidence that it works, and a silent fallback would hide the very failure the
    caller is trying to diagnose.
    """
    if explicit is not None:
        if interpreter_ok(explicit, run=run):
            return explicit
        raise BootstrapError(
            f"{explicit} cannot load sqlite3/ssl/ctypes. Pick another with --python."
        )
    pool = list(candidates) if candidates is not None else candidate_interpreters()
    for candidate in pool:
        if interpreter_ok(candidate, run=run):
            return candidate
    raise BootstrapError(
        f"none of the {len(pool)} interpreters found can load sqlite3/ssl/ctypes. "
        "Install python.org CPython 3.11+ and re-run with --python."
    )
```

Add `import os`, `import shutil`, and `import sys` to the imports at the top.

- [ ] **Step 4: Run the test**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bootstrap_interpreter.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Verify against this machine**

Run: `.venv/Scripts/python.exe -c "from tbmcp.bootstrap import candidate_interpreters, choose_interpreter; print(candidate_interpreters()[:3]); print(choose_interpreter())"`
Expected: a real path is chosen, and no uv-managed path appears before a system one.

- [ ] **Step 6: Commit**

```bash
git add src/tbmcp/bootstrap.py tests/test_bootstrap_interpreter.py
git commit -m "feat: choose an interpreter by load-testing it, not by trusting its path"
```

---

### Task 5: Launcher probe in client registration

**Files:**
- Modify: `src/tbmcp/clients.py:104-127` (`_console_script`)
- Test: `tests/test_clients_launcher.py`

**Interfaces:**
- Consumes: nothing from earlier tasks — this is a standalone fix in existing code.
- Produces: `clients._runnable(path: pathlib.Path) -> bool`; `clients._console_script()` unchanged in signature, now returning `None` for a present-but-unrunnable shim, which makes `server_command()` fall back to `(sys.executable, ["-m", "tbmcp", ...])`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_clients_launcher.py
"""A console script that exists is not the same as one that runs.

Windows Application Control blocks pip's generated .exe shims: the file is right
there, `is_file()` is true, and spawning it raises. Registering that path produces a
client that times out with nothing to point at.
"""

from __future__ import annotations

import pathlib

import pytest

from tbmcp import clients
from tbmcp.config import Settings


@pytest.fixture
def shim(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / ("thunderbird-mcp.exe" if clients.os.name == "nt" else "thunderbird-mcp")
    path.write_bytes(b"MZ")
    return path


def test_unrunnable_shim_is_rejected(monkeypatch, shim):
    def blocked(argv, **kwargs):
        raise OSError("Uygulama Denetimi ilkesi bu dosyayi engelledi")

    monkeypatch.setattr(clients.subprocess, "run", blocked)
    assert clients._runnable(shim) is False


def test_nonzero_exit_is_rejected(monkeypatch, shim):
    monkeypatch.setattr(clients, "_probe_exit", lambda argv: 1)
    assert clients._runnable(shim) is False


def test_runnable_shim_is_accepted(monkeypatch, shim):
    monkeypatch.setattr(clients, "_probe_exit", lambda argv: 0)
    assert clients._runnable(shim) is True


def test_server_command_falls_back_to_module(monkeypatch):
    monkeypatch.setattr(clients, "_console_script", lambda: None)
    command, args = clients.server_command(Settings())
    assert args[:2] == ["-m", "tbmcp"]
    assert command.endswith(("python", "python.exe", "python3"))
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_clients_launcher.py -v`
Expected: FAIL — `AttributeError: module 'tbmcp.clients' has no attribute '_runnable'`.

- [ ] **Step 3: Add the probe**

In `src/tbmcp/clients.py`, add above `_console_script`:

```python
def _probe_exit(argv: list[str]) -> int:
    done = subprocess.run(argv, capture_output=True, timeout=2)
    return done.returncode


def _runnable(path: Path) -> bool:
    """Does this launcher actually start?

    `is_file()` is not enough. Windows Application Control blocks pip's generated
    `.exe` shims, and the failure only shows up in the client as a timeout with no
    stated cause. Two seconds of `--help` here buys a config that works.
    """
    try:
        return _probe_exit([str(path), "--help"]) == 0
    except (OSError, subprocess.SubprocessError):
        return False
```

Then in `_console_script`, change the accept condition:

```python
    for path in candidates:
        if path.is_file() and path.suffix.lower() not in (".cmd", ".bat", ".ps1"):
            resolved = path.resolve()
            if _runnable(resolved):
                return resolved
    return None
```

Ensure `subprocess` and `os` are imported in `clients.py` (it already imports `shutil`, `sys`, and `Path`).

- [ ] **Step 4: Run the test plus the existing client suite**

Run: `.venv/Scripts/python.exe -m pytest tests/test_clients_launcher.py -v` then the full suite: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS. The full run guards against `_console_script` being monkeypatched elsewhere in ways this changes.

- [ ] **Step 5: Verify on this machine**

Run: `.venv/Scripts/python.exe -c "from tbmcp.clients import server_command; from tbmcp.config import Settings; print(server_command(Settings()))"`
Expected: the `python.exe -m tbmcp serve` form, **not** the blocked `.exe`. This is the exact bug that produced the original timeout.

- [ ] **Step 6: Commit**

```bash
git add src/tbmcp/clients.py tests/test_clients_launcher.py
git commit -m "fix: reject a console script that cannot be executed"
```

---

### Task 6: Step engine and orchestration

**Files:**
- Modify: `src/tbmcp/bootstrap.py`
- Test: `tests/test_bootstrap_run.py`

**Interfaces:**
- Consumes: everything from Tasks 2–4.
- Produces:
  - `@dataclass Step(name: str, status: str, seconds: float, detail: str = "")`
  - `@dataclass Report(ok: bool, version: str, launcher: dict | None, steps: list[Step], next_command: str | None)` with `to_json(self) -> str` and `to_text(self) -> str`
  - `@dataclass Options(python: str | None = None, venv: pathlib.Path | None = None, clients: tuple[str, ...] = (), toolsets: str | None = None, json_out: bool = False, dry_run: bool = False, skip_addon: bool = False, source: str | None = None)`
  - `default_venv_dir() -> pathlib.Path`
  - `venv_python(venv_dir: pathlib.Path) -> pathlib.Path`
  - `GIT_SOURCE: str = "git+https://github.com/U-C4N/Thunderbird-MCP"`
  - `bootstrap(options: Options, *, run: Runner = run_capture) -> Report`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bootstrap_run.py -v`
Expected: FAIL — `cannot import name 'Options'`.

- [ ] **Step 3: Implement the engine**

Append to `src/tbmcp/bootstrap.py`. Keep each step a small function returning `(status, detail)` so the driver stays readable and the order is visible in one place:

```python
VERSION = "1.2.0"
GIT_SOURCE = "git+https://github.com/U-C4N/Thunderbird-MCP"


@dataclass
class Step:
    name: str
    status: str
    seconds: float
    detail: str = ""


@dataclass
class Options:
    python: str | None = None
    venv: pathlib.Path | None = None
    clients: tuple[str, ...] = ()
    toolsets: str | None = None
    json_out: bool = False
    dry_run: bool = False
    skip_addon: bool = False
    source: str | None = None


@dataclass
class Report:
    ok: bool
    version: str
    launcher: dict | None
    steps: list[Step]
    next_command: str | None

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "version": self.version,
                "launcher": self.launcher,
                "steps": [vars(step) for step in self.steps],
                "next_command": self.next_command,
            },
            indent=2,
        )

    def to_text(self) -> str:
        lines = ["thunderbird-mcp bootstrap", ""]
        for step in self.steps:
            lines.append(f"  {step.name:<14} {step.status:<10} {step.detail}".rstrip())
        lines.append("")
        lines.append("Ready." if self.ok else f"Stopped. Next:  {self.next_command}")
        return "\n".join(lines)


def default_venv_dir() -> pathlib.Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home() / "AppData" / "Local")
        return pathlib.Path(base) / "thunderbird-mcp" / "venv"
    return pathlib.Path.home() / ".local" / "share" / "thunderbird-mcp" / "venv"


def venv_python(venv_dir: pathlib.Path) -> pathlib.Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"
```

Then the driver. Each step appends to `steps`; the first `failed` stops the run and
sets `next_command`:

```python
def bootstrap(options: Options, *, run: Runner = run_capture) -> Report:
    """Run every step in order, stopping at the first one that cannot be repaired."""
    venv_dir = options.venv or default_venv_dir()
    steps: list[Step] = []
    state: dict[str, object] = {"venv": venv_dir, "launcher": None}

    for name, action in _STEPS:
        if name == "addon" and options.skip_addon:
            steps.append(Step(name, "skipped", 0.0, "--skip-addon"))
            continue
        started = time.monotonic()
        try:
            status, detail = action(options, state, run)
        except BootstrapError as exc:
            status, detail = "failed", str(exc)
        steps.append(Step(name, status, round(time.monotonic() - started, 2), detail))
        if status == "failed":
            return Report(
                ok=False,
                version=VERSION,
                launcher=state.get("launcher"),
                steps=steps,
                next_command=_remedy(name, detail),
            )

    return Report(True, VERSION, state.get("launcher"), steps, None)
```

Write one `_step_*` function per row of the table in the spec, then:

```python
_STEPS = (
    ("interpreter", _step_interpreter),
    ("venv", _step_venv),
    ("install", _step_install),
    ("imports", _step_imports),
    ("binaries", _step_binaries),
    ("launcher", _step_launcher),
    ("addon", _step_addon),
    ("clients", _step_clients),
    ("verify", _step_verify),
)
```

`_remedy(step_name, detail)` returns the single command to run next: for
`interpreter` it is `python bootstrap.py --python <path-to-a-real-python>`; for
`binaries` it is `pip install "<dist>==<older>"` naming the module from the failure;
for every other step it is `tbmcp doctor`. Steps 7–9 shell out to the installed
console entry (`venv_python -m tbmcp install-addon --yes`, `... setup <clients>`,
`... doctor`) rather than importing `tbmcp`, which keeps the stdlib-only rule intact.
`_step_addon` must pass `--yes`: `cmd_install_addon` calls `input()` otherwise and
would hang an agent.

Add `import time` to the imports.

- [ ] **Step 4: Run the test**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bootstrap_run.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add src/tbmcp/bootstrap.py tests/test_bootstrap_run.py
git commit -m "feat: bootstrap step engine with machine-readable reporting"
```

---

### Task 7: Entry points

**Files:**
- Modify: `src/tbmcp/bootstrap.py` (add `main`)
- Create: `bootstrap.py` (repo root shim)
- Modify: `src/tbmcp/cli.py:385-391` (add the subparser, next to `tools`)
- Test: `tests/test_bootstrap_entry.py`

**Interfaces:**
- Consumes: `bootstrap`, `Options`, `Report` from Task 6.
- Produces: `bootstrap.main(argv: Sequence[str] | None = None) -> int`; `cli.cmd_bootstrap(args) -> int`; a repo-root `bootstrap.py` runnable as `python bootstrap.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bootstrap_entry.py
"""The three ways in, and the constraint that makes the first one possible."""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_module_imports_without_the_package_on_the_path(tmp_path):
    """`python bootstrap.py` runs before anything is installed. Keep it stdlib-only."""
    script = tmp_path / "check.py"
    script.write_text(
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('bs', r'{ROOT / 'src' / 'tbmcp' / 'bootstrap.py'}')\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "print(module.VERSION)\n",
        encoding="utf-8",
    )
    done = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PYTHONPATH": "", "PATH": ""} | {"SYSTEMROOT": "C:\\Windows"},
    )
    assert done.returncode == 0, done.stderr
    assert "1.2.0" in done.stdout


def test_root_shim_exists_and_delegates():
    text = (ROOT / "bootstrap.py").read_text(encoding="utf-8")
    assert "tbmcp" in text and "bootstrap" in text


def test_cli_exposes_the_subcommand():
    from tbmcp.cli import build_parser

    args = build_parser().parse_args(["bootstrap", "--json"])
    assert args.command == "bootstrap"
    assert args.json is True


def test_main_returns_nonzero_when_a_step_fails(monkeypatch, capsys):
    from tbmcp import bootstrap as module

    monkeypatch.setattr(
        module,
        "bootstrap",
        lambda options, **kw: module.Report(False, "1.2.0", None, [], "tbmcp doctor"),
    )
    assert module.main(["--json"]) == 1
    assert '"ok": false' in capsys.readouterr().out
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bootstrap_entry.py -v`
Expected: FAIL — the root `bootstrap.py` does not exist and `main` is undefined.

- [ ] **Step 3: Add the entry points**

`main` in `src/tbmcp/bootstrap.py`:

```python
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tbmcp bootstrap",
        description="Install, repair, register, and verify in one command.",
    )
    parser.add_argument("--python", help="interpreter to build the environment with")
    parser.add_argument("--venv", type=pathlib.Path, help="where to put the environment")
    parser.add_argument("--clients", help="comma separated; default: auto-detect")
    parser.add_argument("--toolsets", help="passed through to setup")
    parser.add_argument("--source", help=f"install from here (default: {GIT_SOURCE})")
    parser.add_argument("--skip-addon", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", dest="json_out", action="store_true")
    args = parser.parse_args(argv)

    report = bootstrap(
        Options(
            python=args.python,
            venv=args.venv,
            clients=tuple(c.strip() for c in (args.clients or "").split(",") if c.strip()),
            toolsets=args.toolsets,
            json_out=args.json_out,
            dry_run=args.dry_run,
            skip_addon=args.skip_addon,
            source=args.source,
        )
    )
    print(report.to_json() if args.json_out else report.to_text())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

Add `import argparse`.

Repo-root `bootstrap.py`:

```python
#!/usr/bin/env python3
"""Run the bootstrapper straight out of a clone, before anything is installed.

Loaded by path rather than imported: `src/` is not on `sys.path` yet, and the whole
point of this entry point is that nothing has been installed to put it there.
"""

import importlib.util
import pathlib
import sys

SOURCE = pathlib.Path(__file__).resolve().parent / "src" / "tbmcp" / "bootstrap.py"

spec = importlib.util.spec_from_file_location("tbmcp_bootstrap", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

raise SystemExit(module.main(sys.argv[1:]))
```

In `src/tbmcp/cli.py`, after the `tools` subparser block:

```python
    boot = subparsers.add_parser("bootstrap", help="install, repair, register, verify")
    boot.add_argument("--python")
    boot.add_argument("--venv")
    boot.add_argument("--clients")
    boot.add_argument("--toolsets")
    boot.add_argument("--source")
    boot.add_argument("--skip-addon", action="store_true")
    boot.add_argument("--dry-run", action="store_true")
    boot.add_argument("--json", action="store_true")
    boot.set_defaults(func=cmd_bootstrap)
```

and the command:

```python
def cmd_bootstrap(args: argparse.Namespace) -> int:
    from .bootstrap import main as bootstrap_main

    forwarded: list[str] = []
    for flag in ("python", "venv", "clients", "toolsets", "source"):
        value = getattr(args, flag, None)
        if value:
            forwarded += [f"--{flag}", str(value)]
    for flag in ("skip_addon", "dry_run", "json"):
        if getattr(args, flag, False):
            forwarded.append("--" + flag.replace("_", "-"))
    return bootstrap_main(forwarded)
```

- [ ] **Step 4: Run the test and the whole suite**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bootstrap_entry.py -v` then `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS throughout.

- [ ] **Step 5: Run the real thing, dry**

Run: `.venv/Scripts/python.exe bootstrap.py --dry-run --json`
Expected: valid JSON, nine steps, `"ok": true`, and nothing created on disk.

- [ ] **Step 6: Commit**

```bash
git add src/tbmcp/bootstrap.py src/tbmcp/cli.py bootstrap.py tests/test_bootstrap_entry.py
git commit -m "feat: bootstrap entry points for clone, module, and CLI"
```

---

### Task 8: Prove the three-platform claim in CI

**Files:**
- Modify: `.github/workflows/ci.yml:18-21` (the matrix)

**Interfaces:**
- Consumes: the test suite from Tasks 1–7.
- Produces: a green `macos-latest` job, or evidence that the macOS claim must be dropped.

- [ ] **Step 1: Add macOS to the matrix**

```yaml
        os: [windows-latest, ubuntu-latest, macos-latest]
        python: ["3.11", "3.13"]
```

- [ ] **Step 2: Run the suite locally first**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS. Pushing a red matrix wastes a CI cycle to learn what a local run says for free.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run the suite on macOS as well"
```

- [ ] **Step 4: Push the branch and watch the run**

```bash
git push -u origin feat/bootstrap-v1.2
```

Then check the run. If macOS fails, fix it here — the spec is explicit that macOS is
not done until this job is green, and that dropping the claim is the honest
alternative to shipping it untested.

---

### Task 9: README

**Files:**
- Modify: `README.md:44-52` (Quick start), `README.md:78-124` (client examples), `README.md:439-462` (Troubleshooting)

**Interfaces:**
- Consumes: the `bootstrap` command and `--json` contract.
- Produces: documentation matching what the code does.

- [ ] **Step 1: Replace Quick start**

The current block's first line installs from PyPI, where this package does not exist.
Replace with:

````markdown
## Quick start

```bash
git clone https://github.com/U-C4N/Thunderbird-MCP
cd Thunderbird-MCP
python bootstrap.py
```

One command: it picks an interpreter that works, builds the environment, installs
the add-on, registers your clients, and verifies the whole chain before it returns.
Re-running it is safe — healthy steps are no-ops.

Already installed? `tbmcp bootstrap` does the same thing.
````

- [ ] **Step 2: Add the agent block directly beneath it**

````markdown
### For AI agents

```bash
python bootstrap.py --json
```

Emits one object: `ok`, `version`, `launcher`, `steps[]` (each with `name`,
`status`, `seconds`, `detail`), and `next_command` — null on success, otherwise the
single command that addresses the failure. `status` is one of `ok`, `repaired`,
`skipped`, `failed`. Parse this instead of the human output; the columns are not a
stable interface and the JSON is.
````

- [ ] **Step 3: Fix the hand-written client examples**

In both the Claude Code and Codex blocks, the `command` is a bare
`thunderbird-mcp.exe`. Where that shim is blocked, that config produces a client
that times out. Replace the command/args pairs with the `python -m tbmcp` form and
add one line: "`bootstrap` picks this for you and tests it first — write it by hand
only if you know the console script runs on your machine."

- [ ] **Step 4: Regenerate the doctor sample**

Run: `.venv/Scripts/python.exe -m tbmcp doctor`
Paste the real output over the sample block, which currently shows add-on version
`0.1.4` and a three-account profile that is not what the code produces today.

- [ ] **Step 5: Add a troubleshooting entry**

````markdown
| A dependency fails with "DLL load failed" or "cannot open shared object file" | A binary your OS will not load — Windows Application Control blocks unsigned, low-reputation wheels. `bootstrap` detects this and downgrades the offending package automatically; run `python bootstrap.py` and read the `binaries` step. |
````

- [ ] **Step 6: Check every command in the README actually runs**

Read the file top to bottom and run each command block. Fix what does not work.
This is the whole point of the task — the README's current first command 404s.

- [ ] **Step 7: Commit**

```bash
git add README.md
git commit -m "docs: rewrite install around bootstrap, fix commands that never worked"
```

---

### Task 10: Merge, tag, release

**Files:** none — repository operations.

**Interfaces:**
- Consumes: Tasks 1–9, all committed, CI green.
- Produces: `v1.2.0` on `main`, a GitHub release with the XPI attached.

- [ ] **Step 1: Confirm the suite and a real end-to-end run**

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m tbmcp doctor
```

Expected: all tests pass; doctor reports `connected True`. Do not proceed on a red
suite — use the `superpowers:verification-before-completion` skill.

- [ ] **Step 2: Confirm CI is green on all three platforms**

Check the run for `feat/bootstrap-v1.2`. macOS included.

- [ ] **Step 3: Merge to main**

```bash
git checkout main
git merge --no-ff feat/bootstrap-v1.2 -m "feat: one-command agent bootstrap (v1.2.0)"
git push origin main
```

- [ ] **Step 4: Tag**

```bash
git tag -a v1.2.0 -m "v1.2.0 — one-command bootstrap"
git push origin v1.2.0
```

- [ ] **Step 5: Build the XPI**

```bash
.venv/Scripts/python.exe -c "import pathlib; from tbmcp.addon_build import build_xpi; print(build_xpi(pathlib.Path('dist')))"
```

Expected: `dist/thunderbird-mcp.xpi`, manifest version 1.2.0.

- [ ] **Step 6: Authenticate and release**

`gh auth login` requires the user — it is interactive and cannot be run from here.
Ask them to run `! gh auth login` in the session, then:

```bash
gh release create v1.2.0 dist/thunderbird-mcp.xpi \
  --title "v1.2.0 — one-command bootstrap" \
  --notes-file docs/superpowers/specs/2026-08-11-llm-bootstrap-v1.2-design.md
```

Trim the notes to the Problem and Design sections if the full spec reads too long
for a release page.

- [ ] **Step 7: Verify the release exists**

```bash
gh release view v1.2.0
```

Expected: the release, with `thunderbird-mcp.xpi` attached.

---

## Self-review

**Spec coverage.** Entry point → Task 7. Steps table → Task 6 (engine) with 2, 3, 4
supplying steps 1, 4, 5. Interpreter selection → Task 4. Blocked-binary repair →
Tasks 2–3. Launcher probe → Task 5. Output/`--json` → Task 6, documented in Task 9.
Options and venv location → Tasks 6–7. Three platforms → Task 8. Testing section →
tests in Tasks 2–7 plus the stdlib-only guard in Task 7. Version and release → Tasks
1 and 10. README → Task 9. No spec section is unclaimed.

**Known rough edge.** Task 3's `installed_version` argv construction is awkward and
Task 3 Step 4 says so, with the simplification to apply if the test disagrees.
Better to name it than to let the implementer discover it as a mystery.

**Type consistency.** `Runner` and `RunResult` are defined once in Task 2 and used
unchanged in 3, 4, and 6. `ImportFailure` fields (`module`, `path`, `message`) are
identical across Tasks 2, 3, and 6. `Step.status` uses the same four values in the
constraints, Task 6, and the README. `Report` fields match between Task 6's dataclass,
its `to_json`, the Task 6 test's key assertion, and Task 9's documented contract.

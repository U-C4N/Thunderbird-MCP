"""Bring a machine from a checkout to a working server, repairing what it finds.

Standard library only, and no import from the rest of `tbmcp`: this file has to run
on an interpreter where the package cannot be imported yet, which is the whole point
of it. A subcommand cannot repair a state in which its own package will not load.
"""

from __future__ import annotations

import json
import pathlib
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


_DIST_OF = """
import json
import sys
from importlib.metadata import packages_distributions
dists = packages_distributions().get(sys.argv[1]) or []
print(json.dumps({"dist": dists[0] if dists else None}))
"""

_VERSION_OF = """
import json
import sys
from importlib.metadata import PackageNotFoundError, version
try:
    print(json.dumps({"version": version(sys.argv[1])}))
except PackageNotFoundError:
    print(json.dumps({"version": None}))
"""


@dataclass(frozen=True)
class Repair:
    """A distribution walked down from one version to another to unblock an import."""

    dist: str
    from_version: str
    to_version: str


def _json_field(output: str, key: str):
    """Pull `key` out of the last JSON object line in subprocess output."""
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
    """The version of `dist` currently installed under `python`, or `None`."""
    _status, output = run([python, "-c", _VERSION_OF, dist])
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
    private mirror too. Each distribution gets at most `max_attempts` downgrades, and
    at most `max_dists` distinct distributions are touched per run; either bound
    running out ends the walk with the failure that remained, never a loop.
    """
    repairs: list[Repair] = []
    handled: set[str] = set()

    failure = probe_import(python, target, run=run)
    while failure is not None:
        if not failure.module:
            return repairs, failure
        dist = distribution_for(python, failure.module, run=run)
        if dist is None or dist in handled:
            return repairs, failure
        if len(handled) >= max_dists:
            return repairs, failure
        handled.add(dist)

        started_at = installed_version(python, dist, run=run)
        current = started_at
        for _attempt in range(max_attempts):
            if current is None:
                break
            status, _output = run(
                [python, "-m", "pip", "install", "--quiet", f"{dist}<{current}"]
            )
            if status != 0:
                break
            current = installed_version(python, dist, run=run)
            failure = probe_import(python, target, run=run)
            if failure is None:
                break
            if distribution_for(python, failure.module, run=run) != dist:
                break

        if started_at is not None and current is not None and current != started_at:
            repairs.append(Repair(dist=dist, from_version=started_at, to_version=current))

        if failure is not None:
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

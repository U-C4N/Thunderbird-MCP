"""Bring a machine from a checkout to a working server, repairing what it finds.

Standard library only, and no import from the rest of `tbmcp`: this file has to run
on an interpreter where the package cannot be imported yet, which is the whole point
of it. A subcommand cannot repair a state in which its own package will not load.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
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

        if (
            failure is not None
            and failure.module
            and distribution_for(python, failure.module, run=run) == dist
        ):
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
    if status != 0:
        return False
    return _json_field(output, "ok") is True


def candidate_interpreters(*, run: Runner = run_capture) -> list[str]:
    candidates = [sys.executable]
    if os.name == "nt":
        py_launcher = shutil.which("py")
        if py_launcher:
            for minor in ("3.13", "3.12", "3.11"):
                status, output = run([py_launcher, f"-{minor}", "-c", "import sys; print(sys.executable)"])
                if status == 0:
                    resolved = output.strip().split('\n')[-1]
                    if resolved:
                        candidates.append(resolved)
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
    pool = list(candidates) if candidates is not None else candidate_interpreters(run=run)
    for candidate in pool:
        if interpreter_ok(candidate, run=run):
            return candidate
    raise BootstrapError(
        f"none of the {len(pool)} interpreters found can load sqlite3/ssl/ctypes. "
        "Install python.org CPython 3.11+ and re-run with --python."
    )


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


def _clone_source() -> str | None:
    """The checkout this file lives in, if it is part of one; `None` otherwise.

    A bare `bootstrap.py` handed to an agent has no such checkout — it must pull the
    package from git. But run from inside the repo, installing `GIT_SOURCE` would
    throw away whatever local changes are the reason for testing here at all.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    pyproject = root / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    if 'name = "thunderbird-mcp"' in text:
        return str(root)
    return None


def _step_interpreter(options: Options, state: dict, run: Runner) -> tuple[str, str]:
    python = choose_interpreter(options.python, run=run)
    state["interpreter"] = python
    return "ok", python


def _step_venv(options: Options, state: dict, run: Runner) -> tuple[str, str]:
    """Reuse a venv that already loads; otherwise create one, unless `--dry-run`."""
    venv_dir: pathlib.Path = state["venv"]
    python_path = venv_python(venv_dir)
    if python_path.is_file() and interpreter_ok(str(python_path), run=run):
        state["venv_python"] = str(python_path)
        return "ok", f"reusing existing venv at {venv_dir}"
    if options.dry_run:
        state["venv_python"] = str(python_path)
        return "ok", f"would create venv at {venv_dir}"
    status, output = run([state["interpreter"], "-m", "venv", str(venv_dir)])
    if status != 0:
        return "failed", f"could not create venv at {venv_dir}: {output.strip()}"
    state["venv_python"] = str(python_path)
    return "ok", f"created venv at {venv_dir}"


def _step_install(options: Options, state: dict, run: Runner) -> tuple[str, str]:
    """Install the package into the venv, preferring an explicit source, then a local clone."""
    source = options.source or _clone_source() or GIT_SOURCE
    if options.dry_run:
        return "ok", f"would install {source}"
    venv_dir: pathlib.Path = state["venv"]
    argv = [state["venv_python"], "-m", "pip", "install", "--quiet", source]
    constraints = venv_dir / "constraints.txt"
    if constraints.is_file():
        argv += ["-c", str(constraints)]
    status, output = run(argv)
    if status != 0:
        return "failed", f"pip install {source} failed: {output.strip()}"
    return "ok", f"installed {source}"


def _step_imports(options: Options, state: dict, run: Runner) -> tuple[str, str]:
    """Probe `tbmcp.server`; a blocked import is not a failure yet — `binaries` repairs it.

    `"ok"` would tell a reader the import worked when it did not; `"failed"` would halt
    the driver before `binaries` gets a chance to repair it. `"skipped"` claims nothing
    and stops nothing, which is exactly what a deferred outcome is.
    """
    failure = probe_import(state["venv_python"], run=run)
    state["import_failure"] = failure
    if failure is None:
        return "ok", "tbmcp.server imports cleanly"
    return "skipped", f"{failure.module or 'tbmcp.server'} failed to import; handing off to binaries"


def _step_binaries(options: Options, state: dict, run: Runner) -> tuple[str, str]:
    failure: ImportFailure | None = state.get("import_failure")
    if failure is None:
        return "skipped", "imports already clean"
    if options.dry_run:
        return "skipped", f"dry-run: would attempt to repair {failure.module or 'unknown'}"
    python = state["venv_python"]
    repairs, remaining = repair_imports(python, run=run)
    state["repairs"] = repairs
    write_constraints(state["venv"], repairs)
    if remaining is not None:
        module = remaining.module or "target"
        return "failed", f"{module} import still blocked after repair attempts: {remaining.message}"
    if repairs:
        names = ", ".join(f"{r.dist} {r.from_version}->{r.to_version}" for r in repairs)
        return "repaired", names
    return "ok", "import resolved without a downgrade"


def _step_launcher(options: Options, state: dict, run: Runner) -> tuple[str, str]:
    python = str(state["venv_python"])
    state["launcher"] = {"command": python, "args": ["-m", "tbmcp"]}
    return "ok", f"{python} -m tbmcp"


def _step_addon(options: Options, state: dict, run: Runner) -> tuple[str, str]:
    """`--yes` is mandatory: `cmd_install_addon` calls `input()` without it and would hang."""
    if options.dry_run:
        return "ok", "dry-run: would install the bridge add-on"
    status, output = run([state["venv_python"], "-m", "tbmcp", "install-addon", "--yes"])
    if status != 0:
        return "failed", output.strip() or "install-addon failed"
    return "ok", output.strip() or "add-on installed"


def _step_clients(options: Options, state: dict, run: Runner) -> tuple[str, str]:
    if not options.clients:
        return "skipped", "no clients requested"
    if options.dry_run:
        return "ok", f"dry-run: would configure {', '.join(options.clients)}"
    argv = [state["venv_python"], "-m", "tbmcp", "setup", *options.clients]
    if options.toolsets:
        argv += ["--toolsets", options.toolsets]
    status, output = run(argv)
    if status != 0:
        return "failed", output.strip() or "client setup failed"
    return "ok", output.strip() or f"configured {', '.join(options.clients)}"


def _step_verify(options: Options, state: dict, run: Runner) -> tuple[str, str]:
    if options.dry_run:
        return "ok", "dry-run: skipping post-install verification"
    status, output = run([state["venv_python"], "-m", "tbmcp", "doctor", "--json"])
    if status != 0:
        return "failed", output.strip() or "doctor reported a problem"
    return "ok", output.strip() or "doctor: healthy"


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


def _remedy(step_name: str, detail: str, options: Options, state: dict) -> str:
    """The single next command to run, given where the run stopped.

    `tbmcp doctor` only works once the package is actually installed into the venv —
    naming it for a `venv` or `install` failure would hand back a command that cannot
    run. Those two get a command built from what already exists at that point: the
    interpreter that was chosen, or the venv that was created.
    """
    if step_name == "interpreter":
        return "python bootstrap.py --python <path-to-a-real-python>"
    if step_name == "venv":
        interpreter = state.get("interpreter") or "<path-to-a-real-python>"
        return f'"{interpreter}" -m venv "{state["venv"]}"'
    if step_name == "install":
        python = state.get("venv_python") or str(venv_python(state["venv"]))
        source = options.source or _clone_source() or GIT_SOURCE
        return f'"{python}" -m pip install "{source}"'
    if step_name == "binaries":
        module = detail.split(" ", 1)[0] if detail else "<dist>"
        return f'pip install "{module}==<older>"'
    return "tbmcp doctor"


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
                next_command=_remedy(name, detail, options, state),
            )

    return Report(True, VERSION, state.get("launcher"), steps, None)

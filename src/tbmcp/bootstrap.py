"""Bring a machine from a checkout to a working server, repairing what it finds.

Standard library only, and no import from the rest of `tbmcp`: this file has to run
on an interpreter where the package cannot be imported yet, which is the whole point
of it. A subcommand cannot repair a state in which its own package will not load.
"""

from __future__ import annotations

import argparse
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
#
# The second handler is `BaseException`, not `Exception`: a C extension or a broken
# `sitecustomize.py` can call `sys.exit()` during import, and `SystemExit` derives
# from `BaseException` alone. Missing it here meant the subprocess exited with
# nothing on stdout, and `probe_import` fell through to an empty, unattributable
# failure — exactly the state a repair remedy cannot be built from.
_PROBE = """
import json, sys
try:
    __import__(sys.argv[1])
except ImportError as exc:
    print(json.dumps({"ok": False, "name": exc.name or "", "path": exc.path,
                      "message": str(exc)}))
except BaseException as exc:
    print(json.dumps({"ok": False, "name": "", "path": None,
                      "message": f"{type(exc).__name__}: {exc}"}))
else:
    print(json.dumps({"ok": True}))
"""


@dataclass(frozen=True)
class ImportFailure:
    """A module that would not load, and the file the loader was reading.

    `dist` and `floor` are filled in by `repair_imports` once it has resolved which
    installed distribution owns the blocked module and what version was last known
    to be installed — the two facts a remedy needs to name a real, runnable
    downgrade instead of guessing. `probe_import` alone cannot know either, so it
    always leaves them `None`.
    """

    module: str
    path: str | None
    message: str
    dist: str | None = None
    floor: str | None = None


def probe_import(
    python: str,
    target: str = "tbmcp.server",
    *,
    run: Runner = run_capture,
) -> ImportFailure | None:
    """Import `target` under `python`. `None` means it worked."""
    status, output = run([python, "-c", _PROBE, target])
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
    # No JSON line at all: the probe crashed before it could report, or the
    # interpreter itself could not be started. The exit status is the only signal
    # left, so carry it rather than returning a blank, unattributable message.
    detail = output.strip() or "no output"
    return ImportFailure(module="", path=None, message=f"probe exited {status}: {detail}")


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

    A returned failure carries `dist` and `floor` whenever they were resolved along
    the way, so a caller can build `pip install "dist<floor"` — a bound already
    known to resolve — instead of naming a module where a distribution belongs, or
    inventing a version that does not exist.
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
            floor = installed_version(python, dist, run=run)
            return repairs, ImportFailure(
                module=failure.module,
                path=failure.path,
                message=failure.message,
                dist=dist,
                floor=floor,
            )
        handled.add(dist)

        started_at = installed_version(python, dist, run=run)
        current = started_at
        # Tracks which distribution the *current* failure resolves to, so the
        # post-loop check below can reuse it instead of asking `distribution_for`
        # a second time for the same answer.
        resolved_dist: str | None = dist
        for _attempt in range(max_attempts):
            if current is None:
                break
            status, _output = run([python, "-m", "pip", "install", "--quiet", f"{dist}<{current}"])
            if status != 0:
                break
            current = installed_version(python, dist, run=run)
            failure = probe_import(python, target, run=run)
            if failure is None:
                resolved_dist = None
                break
            resolved_dist = (
                distribution_for(python, failure.module, run=run) if failure.module else None
            )
            if resolved_dist != dist:
                break

        if started_at is not None and current is not None and current != started_at:
            repairs.append(Repair(dist=dist, from_version=started_at, to_version=current))

        if failure is not None and failure.module and resolved_dist == dist:
            return repairs, ImportFailure(
                module=failure.module,
                path=failure.path,
                message=failure.message,
                dist=dist,
                floor=current,
            )

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
                status, output = run(
                    [py_launcher, f"-{minor}", "-c", "import sys; print(sys.executable)"]
                )
                if status == 0:
                    resolved = output.strip().split("\n")[-1]
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
    tools: str | None = None
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
        if self.ok:
            lines.append("Ready.")
        elif any(step.status == "failed" for step in self.steps):
            lines.append(f"Stopped. Next:  {self.next_command}")
        else:
            # `ok` is deliberately never `true` for a dry run (nothing was verified),
            # but a dry run that hit no failed step did everything it was asked to —
            # that is not "Stopped.", which would read as the same outcome as a real
            # failure.
            lines.append(f"Dry run complete; nothing was changed. Next:  {self.next_command}")
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
    """Reuse a venv that already loads; otherwise create one, unless `--dry-run`.

    The venv it just built gets the same load test as a reused one before either is
    trusted: a zero exit from `python -m venv` says the tool ran, not that the
    interpreter it produced can actually load `sqlite3`/`ssl`/`ctypes` — the same gap
    `interpreter_ok` exists to close for candidate selection in the first place.
    """
    venv_dir: pathlib.Path = state["venv"]
    python_path = venv_python(venv_dir)
    if python_path.is_file() and interpreter_ok(str(python_path), run=run):
        state["venv_python"] = str(python_path)
        return "ok", f"reusing existing venv at {venv_dir}"
    if options.dry_run:
        state["venv_python"] = str(python_path)
        return "skipped", f"dry-run: would create venv at {venv_dir}"
    status, output = run([state["interpreter"], "-m", "venv", str(venv_dir)])
    if status != 0:
        return "failed", f"could not create venv at {venv_dir}: {output.strip()}"
    if not interpreter_ok(str(python_path), run=run):
        return (
            "failed",
            f"created venv at {venv_dir}, but its python cannot load sqlite3/ssl/ctypes",
        )
    state["venv_python"] = str(python_path)
    return "ok", f"created venv at {venv_dir}"


def _step_install(options: Options, state: dict, run: Runner) -> tuple[str, str]:
    """Install the package into the venv, preferring an explicit source, then a local clone."""
    source = options.source or _clone_source() or GIT_SOURCE
    if options.dry_run:
        return "skipped", f"dry-run: would install {source}"
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

    Under `--dry-run` nothing is probed at all: the venv this would run against was
    never created (`venv` reported `skipped`, not `ok`), so probing it can only ever
    report a failure that has nothing to do with the actual package.
    """
    if options.dry_run:
        return "skipped", "dry-run: would probe tbmcp.server (no venv was created to probe)"
    failure = probe_import(state["venv_python"], run=run)
    state["import_failure"] = failure
    if failure is None:
        return "ok", "tbmcp.server imports cleanly"
    return (
        "skipped",
        f"{failure.module or 'tbmcp.server'} failed to import; handing off to binaries",
    )


def _step_binaries(options: Options, state: dict, run: Runner) -> tuple[str, str]:
    if options.dry_run:
        return "skipped", "dry-run: imports were not probed, so there is nothing to repair"
    failure: ImportFailure | None = state.get("import_failure")
    if failure is None:
        return "skipped", "imports already clean"
    python = state["venv_python"]
    repairs, remaining = repair_imports(python, run=run)
    state["repairs"] = repairs
    state["binaries_failure"] = remaining
    write_constraints(state["venv"], repairs)
    if remaining is not None:
        module = remaining.module or "target"
        return "failed", f"{module} import still blocked after repair attempts: {remaining.message}"
    if repairs:
        names = ", ".join(f"{r.dist} {r.from_version}->{r.to_version}" for r in repairs)
        return "repaired", names
    return "ok", "import resolved without a downgrade"


def _step_launcher(options: Options, state: dict, run: Runner) -> tuple[str, str]:
    """Defaulting straight to `python -m tbmcp` is an intentional deviation from the
    design's "probe the console script, fall back to `-m tbmcp`" (that probe already
    lives in `clients._console_script`, which every client writer goes through) —
    `-m tbmcp` always works once the package is installed, and the README documents
    the simplification. But the step still has to earn its `ok`: it runs `-m tbmcp
    --help` before claiming one, instead of naming an interpreter untested (or, under
    `--dry-run`, one that was never created) and calling that `ok`.
    """
    python = str(state["venv_python"])
    state["launcher"] = {"command": python, "args": ["-m", "tbmcp"]}
    if options.dry_run:
        return "skipped", f"dry-run: would launch via {python} -m tbmcp"
    status, output = run([python, "-m", "tbmcp", "--help"])
    if status != 0:
        return "failed", output.strip() or f"{python} -m tbmcp did not run"
    return "ok", f"{python} -m tbmcp"


def _step_addon(options: Options, state: dict, run: Runner) -> tuple[str, str]:
    """`--yes` is mandatory: `cmd_install_addon` calls `input()` without it and would hang."""
    if options.dry_run:
        return "skipped", "dry-run: would install the bridge add-on"
    status, output = run([state["venv_python"], "-m", "tbmcp", "install-addon", "--yes"])
    if status != 0:
        return "failed", output.strip() or "install-addon failed"
    return "ok", output.strip() or "add-on installed"


def _detected_clients(python: str, run: Runner) -> tuple[str, ...] | None:
    """Ask the installed package which clients it can see, via `detect-clients`.

    Shelling out through the venv's interpreter is the only option: this module may
    not import `tbmcp.clients` even indirectly, since it has to run before the
    package is importable at all.

    Returns `None` when detection could not run or its output could not be read —
    a nonexistent venv python (`FileNotFoundError` under `--dry-run`, before any
    venv exists), a crash, unparsable output — as distinct from `()`, which means
    detection ran cleanly and genuinely found nothing. Collapsing the two used to
    let `_step_clients` assert "no MCP clients detected on this machine" when
    detection had not looked at the machine at all.
    """
    status, output = run([python, "-m", "tbmcp", "detect-clients"])
    if status != 0:
        return None
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("["):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, list):
                return tuple(str(item) for item in data)
    return None


def _step_clients(options: Options, state: dict, run: Runner) -> tuple[str, str]:
    """Register with the clients asked for, or with whatever `detect-clients` finds.

    Detection only ever fills in for an *absent* `--clients`, never overrides one
    that was given: asking for a specific client must not silently grow or shrink to
    match what happens to be installed. It runs even under `--dry-run` because it
    only reads the machine — the write it would lead to is what `--dry-run` actually
    gates. (In practice, under a bootstrap `--dry-run` where no venv exists yet,
    detection cannot run either — see `_detected_clients` — and this reports that
    honestly instead of claiming the machine was checked.)
    """
    clients = options.clients
    detected = False
    if not clients:
        detected = True
        found = _detected_clients(state["venv_python"], run)
        if found is None:
            if options.dry_run:
                # No venv exists yet under --dry-run (`venv` reported `skipped`), so
                # this could never have run in the first place.
                return (
                    "skipped",
                    "could not run client detection (no working venv to run it in yet)",
                )
            # A real run only reaches here with a working, load-tested venv already
            # in hand — `_detected_clients` returning `None` here means detect-clients
            # itself crashed or produced output that could not be parsed, not that
            # there was nothing to run it in.
            return (
                "skipped",
                "could not run client detection (detect-clients crashed or produced no usable output)",
            )
        clients = found
        if not clients:
            return (
                "skipped",
                "detect-clients ran and found no MCP clients installed on this machine",
            )

    if options.dry_run:
        how = "detected" if detected else "requested"
        return "skipped", f"dry-run: would configure {', '.join(clients)} ({how})"

    argv = [state["venv_python"], "-m", "tbmcp", "setup", *clients]
    if options.toolsets:
        argv += ["--toolsets", options.toolsets]
    if options.tools:
        argv += ["--tools", options.tools]
    status, output = run(argv)
    if status != 0:
        return "failed", output.strip() or "client setup failed"
    return "ok", output.strip() or f"configured {', '.join(clients)}"


def _step_verify(options: Options, state: dict, run: Runner) -> tuple[str, str]:
    """Run `doctor --json` (which itself makes a live `tb_status` tool call) and
    require its own verdict, not just a zero exit status.

    An exit code alone is not enough: this is the step that certifies "a verified
    working MCP server", so it has to read what `doctor` actually found rather than
    trust that a caller wired the exit code correctly. `doctor --json` reports its
    own `"ok"` boolean for exactly this reason.
    """
    if options.dry_run:
        return "skipped", "dry-run: skipping post-install verification"
    argv = [state["venv_python"], "-m", "tbmcp", "doctor", "--json"]
    if options.toolsets:
        argv += ["--toolsets", options.toolsets]
    if options.tools:
        argv += ["--tools", options.tools]
    status, output = run(argv)
    payload = _json_field_report(output)
    if status != 0 or payload is None or not payload.get("ok"):
        return "failed", output.strip() or "doctor reported a problem"
    tool_call = payload.get("tbStatusCall")
    if isinstance(tool_call, dict) and tool_call.get("skipped"):
        # A configuration without `tb_status` registered is supported, not a broken
        # chain — `doctor`'s own `ok` already accounts for it (see `_doctor_ok`).
        # Say so here too, rather than claiming a check that never ran.
        return (
            "ok",
            "doctor: healthy (bridge connected; tb_status not checked — it is not registered)",
        )
    return "ok", "doctor: healthy (bridge connected, tb_status tool call verified)"


def _json_field_report(output: str) -> dict | None:
    """Parse `doctor --json`'s pretty-printed report out of possibly noisy output.

    `doctor --json` prints one indented (multi-line) JSON object and nothing else to
    stdout; `run_capture` merges stdout+stderr as `stdout + stderr`, so any logging
    noise (stderr) lands *after* the JSON, never inside it. `raw_decode` parses the
    object from the start and ignores whatever trails it, so that noise cannot break
    parsing the way a `splitlines()` / `startswith("{")` scan would against output
    that spans more than one line.
    """
    try:
        payload, _end = json.JSONDecoder().raw_decode(output.lstrip())
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


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

    Never a bare `tbmcp` or bare `pip`: neither is on PATH until a venv exists *and*
    is activated, which is not the state a failing step leaves behind. `interpreter`
    is the one genuine exception — nothing has been chosen yet, so there is no venv
    python to build a command from. Every other branch is built from what `state`
    actually holds at the point the run stopped: the interpreter that was chosen,
    the venv that was created, or (from `imports` onward) the venv's own python,
    which by then exists.
    """
    python = state.get("venv_python") or str(venv_python(state["venv"]))
    if step_name == "interpreter":
        return "python bootstrap.py --python <path-to-a-real-python>"
    if step_name == "venv":
        interpreter = state.get("interpreter") or "<path-to-a-real-python>"
        return f'"{interpreter}" -m venv "{state["venv"]}"'
    if step_name == "install":
        source = options.source or _clone_source() or GIT_SOURCE
        return f'"{python}" -m pip install "{source}"'
    if step_name == "binaries":
        failure: ImportFailure | None = state.get("binaries_failure")
        if failure is not None and failure.dist:
            if failure.floor:
                return f'"{python}" -m pip install "{failure.dist}<{failure.floor}"'
            return f'"{python}" -m pip install "{failure.dist}" --force-reinstall'
        # No distribution could be resolved at all (an empty-module failure) — there
        # is nothing to name a downgrade for. Re-running the probe directly at least
        # shows the real error instead of a fabricated command.
        return f'"{python}" -c "import tbmcp.server"'
    return f'"{python}" -m tbmcp doctor'


def _rerun_without_dry_run(options: Options) -> str:
    """What a `--dry-run` that reached the end should actually be run as, to make
    what it described real. Mirrors the flags `tbmcp bootstrap` forwards to this
    module (`cli.cmd_bootstrap`), minus the two that only affect *how* this reports."""
    parts = ["python bootstrap.py"]
    if options.python:
        parts.append(f'--python "{options.python}"')
    if options.venv:
        parts.append(f'--venv "{options.venv}"')
    if options.clients:
        parts.append(f"--clients {','.join(options.clients)}")
    # Quoted like the paths above, because both of these accept whitespace that the
    # parsers then strip: `--tools "a, b"` is valid input, and rendering it bare puts
    # `b` back on the command line as a stray positional. The rerun line is the one
    # thing a dry run exists to produce, so it has to survive being pasted.
    if options.toolsets:
        parts.append(f'--toolsets "{options.toolsets}"')
    if options.tools:
        parts.append(f'--tools "{options.tools}"')
    if options.source:
        parts.append(f'--source "{options.source}"')
    if options.skip_addon:
        parts.append("--skip-addon")
    return " ".join(parts)


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
        except Exception as exc:
            # A step's own bug must not deny the caller the one output contract it
            # depends on (`--json`). Losing this to an unhandled traceback is the
            # exact failure mode I5 in the review names: an agent asking for
            # `--json` getting no JSON at all.
            status, detail = "failed", f"{type(exc).__name__}: {exc}"
        steps.append(Step(name, status, round(time.monotonic() - started, 2), detail))
        if status == "failed":
            return Report(
                ok=False,
                version=VERSION,
                launcher=state.get("launcher"),
                steps=steps,
                next_command=_remedy(name, detail, options, state),
            )

    if options.dry_run:
        # A dry run never runs `verify` (or anything else that would establish a
        # fact), so it must never look identical to a real success in the five-key
        # JSON contract an agent parses. `ok` here means "verified working"; a dry
        # run verified nothing, so it is never `true` — regardless of how clean
        # every individual step's `skipped`/`ok` reads.
        return Report(
            ok=False,
            version=VERSION,
            launcher=state.get("launcher"),
            steps=steps,
            next_command=_rerun_without_dry_run(options),
        )

    return Report(True, VERSION, state.get("launcher"), steps, None)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tbmcp bootstrap",
        description="Install, repair, register, and verify in one command.",
    )
    parser.add_argument("--python", help="interpreter to build the environment with")
    parser.add_argument("--venv", type=pathlib.Path, help="where to put the environment")
    parser.add_argument("--clients", help="comma separated; default: auto-detect installed clients")
    parser.add_argument("--toolsets", help="passed through to setup")
    parser.add_argument("--tools", help="passed through to setup")
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
            tools=args.tools,
            json_out=args.json_out,
            dry_run=args.dry_run,
            skip_addon=args.skip_addon,
            source=args.source,
        )
    )
    print(report.to_json() if args.json_out else report.to_text())
    if args.dry_run:
        # `ok` stays `false` for every dry run on purpose (see `Report.to_json`'s
        # docstring-equivalent note in `bootstrap()`) — nothing was verified. But a
        # dry run that completed every step it was asked to is not a failure an agent
        # should see reflected in the exit code; only a step that actually failed
        # should do that.
        return 1 if any(step.status == "failed" for step in report.steps) else 0
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

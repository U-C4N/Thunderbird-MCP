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

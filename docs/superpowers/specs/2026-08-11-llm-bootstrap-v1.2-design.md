# One-command bootstrap for agent-driven installs (v1.2)

Status: approved 2026-08-11

## Problem

An LLM agent installing this project today fails in ways that point nowhere near the
cause. Every failure below was observed on a real Windows 11 machine on 2026-08-11,
in the order an agent would hit them:

1. `uv tool install thunderbird-mcp` — the package is not on PyPI (404). The first
   command in the README cannot work.
2. Installing from source with `uv tool install .` succeeds, but the interpreter uv
   downloads (python-build-standalone) is unsigned, and Windows Smart App Control
   blocks its own `_sqlite3` DLL. The install reports success; the program cannot run.
3. Under a signed system interpreter, two wheels are blocked the same way:
   `cffi==2.1.1` and `pywin32==312`. Neighbouring unsigned wheels (`pydantic_core`,
   `rpds`, `websockets`) load fine, so this is per-file reputation, not signing.
4. The pip-generated `thunderbird-mcp.exe` console shim is blocked too, so `setup`
   writes a client config that can never start.
5. The failure surfaces as `MCP error -32001: Request timed out` in the client —
   five layers away from the blocked DLL.

None of this is a bug in the project's own code. It is an install path that assumes
its environment cooperates, and reports success at each step that did not.

The unifying insight: **a subcommand of the package cannot repair a state in which
the package will not import.** Bootstrap has to be able to run before, and
independently of, a working install.

## Goals

- One command an agent runs to go from clean checkout to a verified working server.
- Idempotent: safe to re-run, no-op when already healthy.
- Machine-readable output so an agent parses results instead of scraping prose.
- Every failure reports the one command that would fix it.
- Windows, macOS, and Linux.

## Non-goals

- Publishing to PyPI. v1.2 installs from git.
- Working around Smart App Control specifically. SAC is one instance of a general
  class — a binary that will not load — and is handled by the general mechanism.
- Any change to the daemon/add-on wire protocol.

## Design

### Entry point

`src/tbmcp/bootstrap.py`: standard library only, no imports from the rest of the
package, runnable three ways —

```
python bootstrap.py            # from a fresh clone, nothing installed yet
python -m tbmcp.bootstrap      # once the package is importable
tbmcp bootstrap                # thin CLI subcommand delegating to the same module
```

The stdlib-only constraint is what makes the first form work, and the first form is
what breaks the chicken-and-egg. It is a hard constraint on this file, enforced by a
test that imports it with the package directory absent from `sys.path`.

A repo-root `bootstrap.py` is a two-line shim that execs
`src/tbmcp/bootstrap.py`, so a clone needs no path knowledge.

### Steps

Fixed order, each step detects and may repair. Each records name, status
(`ok` / `repaired` / `skipped` / `failed`), duration, and on failure exactly one
remediation command.

| # | Step | Repair |
|---|------|--------|
| 1 | `interpreter` | Rank candidates, load-test each, pick the first that passes. |
| 2 | `venv` | Create at the resolved location; reuse if present and healthy. |
| 3 | `install` | `pip install` from the local clone if run inside one, else from the git URL. |
| 4 | `imports` | Import `tbmcp.server` in the venv. This is where a blocked binary surfaces. |
| 5 | `binaries` | Downgrade the offending distribution until the import succeeds. |
| 6 | `launcher` | Probe the console script by executing it; fall back to `python -m tbmcp`. |
| 7 | `addon` | Existing `install-addon`. |
| 8 | `clients` | Existing `setup`, using the probed launcher. |
| 9 | `verify` | `doctor`, then a live `tb_status` tool call through the full chain. |

Steps 1–6 are new. Steps 7–9 wrap existing code and must not duplicate it.

### Interpreter selection (step 1)

Candidates, in preference order: an explicit `--python`; the interpreter running
bootstrap; `py -3.13`/`-3.12`/`-3.11` via the launcher on Windows; `python3.13`…
`python3.11` and `python3` on PATH; well-known install roots (`C:\PythonXYZ`,
`/usr/bin`, Homebrew prefixes).

Candidates whose path lies under a uv-managed Python root
(`%APPDATA%\uv\python`, `~/.local/share/uv/python`) are ranked last rather than
excluded — they are fine on machines without an enforcing policy, and excluding them
outright would break the common case to serve the rare one.

Each candidate is load-tested in a subprocess: `import sqlite3, ssl, ctypes` plus
`sysconfig` reporting. Passing that is necessary and sufficient; we never inspect
signatures or query OS policy, both of which are platform-specific and lie.

### Blocked-binary repair (steps 4–5)

Detection keys on Python's own message, never the OS text. `ImportError` from a
failed extension load always reads:

```
DLL load failed while importing <module>: <OS message, in the system locale>
```

The prefix is Python's and is English regardless of locale; the tail was Turkish on
the observed machine. The parser extracts `<module>` from the prefix and ignores the
tail entirely. The equivalent Linux/macOS forms (`cannot open shared object file`,
`Library not loaded`) are parsed from the same function.

Repair maps module → distribution via `importlib.metadata.packages_distributions()`,
then walks the version down with `pip install "<dist><<current>"`, which resolves to
the next release below without needing to enumerate the index. After each attempt the
import is retried in a fresh subprocess. Bounded at three attempts per distribution
and three distinct distributions per run; exceeding either is a `failed` step naming
the module, not an infinite loop.

Whatever versions the repair settles on are written to a `constraints.txt` inside the
venv and passed to subsequent `pip install` calls, so a later reinstall does not
silently undo the repair.

### Launcher probe (step 6)

`clients._console_script()` currently accepts a path because `is_file()` is true. It
must also be *runnable*: execute the candidate with `--help`, a two-second timeout,
and require exit status 0. `OSError`, non-zero exit, or timeout all demote it to
`python -m tbmcp`. This one change fixes the observed failure at its source — every
client registration flows through `server_command()`.

### Output

Human output stays the current aligned-columns style. `--json` emits one object:

```json
{
  "ok": true,
  "version": "1.2.0",
  "launcher": {"command": "...", "args": ["serve"]},
  "steps": [{"name": "interpreter", "status": "ok", "seconds": 0.4, "detail": "..."}],
  "next_command": null
}
```

`next_command` is null on success and a single copy-pasteable string on failure.
`--dry-run` reports what each step would do and writes nothing, matching `setup`.

### Options

`--python`, `--venv`, `--clients` (default: auto-detect installed clients),
`--toolsets`, `--json`, `--dry-run`, `--skip-addon`. Toolset and safety flags are
forwarded to `setup` unchanged.

The venv lives at `%LOCALAPPDATA%\thunderbird-mcp\venv` on Windows and
`~/.local/share/thunderbird-mcp/venv` elsewhere — outside the clone, so the clone is
disposable and re-running from a different directory finds the same environment.

## Testing

Unit tests, no OS policy required:

- `DLL load failed` parser against Turkish, English, and German OS tails, plus the
  Linux and macOS message forms.
- Downgrade planner: bounds respected, gives up with a named module, emits
  constraints.
- Interpreter ranking: uv-managed ranked last, explicit `--python` wins, a candidate
  failing its load test is skipped.
- Launcher probe: non-zero exit, `OSError`, and timeout each fall back to `-m`.
- `bootstrap.py` imports with the package absent from `sys.path`.
- Step order and idempotency: a second run reports every step `ok`, changes nothing.

End-to-end on Windows is verified by hand on the development machine. CI gains
`macos-latest` alongside the existing `windows-latest` and `ubuntu-latest`, so the
three-platform claim rests on CI rather than assertion. macOS is not considered done
until that job is green.

## Version and release

`0.1.0` (package) and `0.2.0` (add-on) are unified at **1.2.0**. Three places carry
it: `pyproject.toml`, `addon/manifest.json`, and the `server.py:178` fallback, which
currently hardcodes a stale `0.1.0.dev0`. A test asserts all three agree, so they
cannot drift again.

README fixes, all of them current inaccuracies rather than polish:

- Quick start installs from git and calls `bootstrap`; the PyPI line goes.
- Hand-written client examples show the probed launcher, not the bare `.exe`.
- The `doctor` sample output is regenerated from a real run.
- A short "For AI agents" block: clone, one command, `--json` contract.
- A troubleshooting entry for blocked binaries, describing the general symptom.

Release: tag `v1.2.0`, GitHub release with the built XPI attached. This needs
`gh auth login`, which the user must run; it is the one step that cannot be
automated from here.

## Risks

- **macOS is unverified locally.** CI is the only evidence. If the macOS job fails
  late, the honest options are fixing it or dropping the macOS claim — not shipping
  it untested.
- **Downgrading can find no working version.** Bounded and reported rather than
  retried forever; the user is told which module and interpreter to change.
- **`pip index`-free downgrade walks one release at a time.** Slower than jumping to
  a known-good version, but it needs no index parsing and no pinned knowledge that
  would rot.

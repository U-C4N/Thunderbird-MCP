#!/usr/bin/env python3
"""Cross-layer consistency checks.

Three layers have to agree on method names, and nothing in Python or JavaScript
notices when they stop agreeing — a mismatch only shows up as a confusing runtime
error at whichever layer is asked first. This script compares them.

    python tools/check_consistency.py [--fix]

`--fix` rewrites addon/manifest.json's background.scripts list and the
PREREGISTERED array in addon/background/handlers/privileged.js.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
ADDON = ROOT / "addon"
TOOLS = ROOT / "src" / "tbmcp" / "tools"

# Methods the daemon answers itself; no add-on handler exists for them.
DAEMON_LOCAL = {"daemon.status", "daemon.events", "daemon.waitForThunderbird", "daemon.shutdown"}

CALL_RE = re.compile(r'call\(\s*\n?\s*"([a-zA-Z][\w.]*)"')
DEFINE_RE = re.compile(r'tbxRegistry\.define\(\s*"([^"]+)"')
MODULE_KEY_RE = re.compile(r'TBX_MODULES\[\s*"([^"]+)"\s*\]')
MODULE_NAME_RE = re.compile(r'TBX_MODULE_NAMES\.push\(\s*"([^"]+)"\s*\)')
PREREG_RE = re.compile(r"const PREREGISTERED = \[(.*?)\];", re.DOTALL)


def python_calls() -> dict[str, set[str]]:
    """Bridge methods each toolset asks for."""
    out: dict[str, set[str]] = {}
    for path in sorted(TOOLS.glob("*.py")):
        if path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8")
        found = {m for m in CALL_RE.findall(text) if "." in m}
        if found:
            out[path.stem] = found
    return out


def addon_methods() -> set[str]:
    found: set[str] = set()
    for path in sorted((ADDON / "background" / "handlers").glob("*.js")):
        text = path.read_text(encoding="utf-8")
        found |= {m for m in DEFINE_RE.findall(text) if not m.startswith("<")}
    return found


def privileged_keys() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in sorted((ADDON / "experiment" / "modules").glob("*.js")):
        text = path.read_text(encoding="utf-8")
        out[path.stem] = set(MODULE_KEY_RE.findall(text))
    return out


def preregistered() -> set[str]:
    text = (ADDON / "background" / "handlers" / "privileged.js").read_text(encoding="utf-8")
    match = PREREG_RE.search(text)
    if not match:
        raise SystemExit("could not find the PREREGISTERED array in privileged.js")
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def expected_scripts() -> list[str]:
    """background.scripts in dependency order.

    Shared globals first (log, registry, capabilities), then handlers in whatever
    order — they only register into the registry — then events and transport, then
    main.js last because it is what starts everything.
    """
    background = ADDON / "background"
    lead = ["background/log.js", "background/registry.js", "background/capabilities.js"]
    handlers = sorted(
        f"background/handlers/{p.name}" for p in (background / "handlers").glob("*.js")
    )
    tail = ["background/events.js", "background/transport.js", "background/main.js"]
    present = {
        f"background/{p.relative_to(background).as_posix()}" for p in background.rglob("*.js")
    }
    ordered = [*lead, *handlers, *tail]
    missing = present - set(ordered)
    if missing:
        raise SystemExit(f"unclassified background scripts: {sorted(missing)}")
    absent = [name for name in ordered if name not in present]
    if absent:
        raise SystemExit(f"listed scripts that do not exist: {absent}")
    return ordered


def main() -> int:
    fix = "--fix" in sys.argv
    problems: list[str] = []
    changes: list[str] = []

    handlers = addon_methods()
    modules = privileged_keys()
    all_privileged = {key for keys in modules.values() for key in keys}
    prereg = preregistered()

    # 1. Every method a Python tool calls must exist somewhere.
    for toolset, methods in python_calls().items():
        for method in sorted(methods):
            if method in DAEMON_LOCAL:
                continue
            if method.startswith("x."):
                bare = method[2:]
                if bare not in all_privileged:
                    problems.append(
                        f"{toolset}.py calls {method} but no privileged module defines {bare!r}"
                    )
                elif method[2:] not in prereg:
                    problems.append(f"{method} is implemented but missing from PREREGISTERED")
            elif method not in handlers:
                problems.append(f"{toolset}.py calls {method} but no add-on handler defines it")

    # 2. PREREGISTERED must match the modules exactly. A preregistered name with no
    #    implementation fails at the wrong layer with a misleading message.
    stale = sorted(prereg - all_privileged)
    unlisted = sorted(all_privileged - prereg)
    if stale:
        problems.append(f"PREREGISTERED lists unimplemented methods: {stale}")
    if unlisted:
        problems.append(f"privileged methods missing from PREREGISTERED: {unlisted}")

    if fix and (stale or unlisted):
        path = ADDON / "background" / "handlers" / "privileged.js"
        text = path.read_text(encoding="utf-8")
        # Grouped by module so a diff stays legible, but every entry keeps its
        # trailing comma — a missing one between groups is a JavaScript syntax
        # error that only surfaces when Thunderbird refuses to load the add-on.
        lines: list[str] = []
        for _name, keys in sorted(modules.items()):
            if not keys:
                continue
            buffer = "    "
            for key in sorted(keys):
                item = f'"{key}",'
                if len(buffer) + len(item) + 1 > 96:
                    lines.append(buffer.rstrip())
                    buffer = "    "
                buffer += item + " "
            lines.append(buffer.rstrip())
        replacement = "const PREREGISTERED = [\n" + "\n".join(lines) + "\n  ];"
        text = PREREG_RE.sub(lambda _m: replacement, text, count=1)
        path.write_text(text, encoding="utf-8")
        changes.append(f"rewrote PREREGISTERED ({len(all_privileged)} methods)")

    # 3. Manifest background.scripts must list exactly the files that exist.
    manifest_path = ADDON / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    listed = list(manifest["background"]["scripts"])
    wanted = expected_scripts()
    if listed != wanted:
        problems.append(
            "manifest background.scripts is out of date"
            + (
                f"; missing {sorted(set(wanted) - set(listed))}"
                if set(wanted) - set(listed)
                else ""
            )
            + (f"; extra {sorted(set(listed) - set(wanted))}" if set(listed) - set(wanted) else "")
            + ("; order differs" if set(listed) == set(wanted) else "")
        )
        if fix:
            manifest["background"]["scripts"] = wanted
            manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            changes.append(f"rewrote background.scripts ({len(wanted)} files)")

    # 4. Toolset modules must not postpone annotations.
    for path in sorted(TOOLS.glob("*.py")):
        if path.name.startswith("_"):
            continue
        # Match a real import statement, not a comment mentioning one.
        if re.search(
            r"^\s*from\s+__future__\s+import\s+.*annotations",
            path.read_text(encoding="utf-8"),
            re.MULTILINE,
        ):
            problems.append(
                f"{path.name} uses `from __future__ import annotations`, which turns "
                "Gate(...) into a string the SDK cannot resolve"
            )

    # 5. Every privileged module must announce itself, or it loads invisibly.
    for name, _keys in modules.items():
        text = (ADDON / "experiment" / "modules" / f"{name}.js").read_text(encoding="utf-8")
        pushed = MODULE_NAME_RE.findall(text)
        if name not in pushed:
            problems.append(f"{name}.js does not push {name!r} onto TBX_MODULE_NAMES")

    # 6. Syntax. This check exists because an earlier version of --fix emitted an
    #    array with a missing comma, and every name-level check above still passed
    #    while Thunderbird would have refused to load the add-on at all.
    node = shutil.which("node")
    if node:
        for path in sorted(ADDON.rglob("*.js")):
            proc = subprocess.run(
                [node, "--check", str(path)], capture_output=True, text=True, check=False
            )
            if proc.returncode != 0:
                first = (proc.stderr or "").strip().splitlines()
                problems.append(
                    f"{path.relative_to(ROOT)} is not valid JavaScript: "
                    f"{first[-1] if first else 'node --check failed'}"
                )
    else:
        print("note: node is not on PATH, so JavaScript syntax was not checked")

    for path in sorted(ADDON.rglob("*.json")):
        raw = path.read_bytes()
        # PowerShell's `Set-Content -Encoding utf8` writes a BOM on 5.1, and both
        # Gecko's manifest parser and json.loads reject it. Easy to introduce, and the
        # resulting error ("Invalid XPI") points nowhere near the cause.
        if raw.startswith(b"\xef\xbb\xbf"):
            problems.append(
                f"{path.relative_to(ROOT)} starts with a UTF-8 BOM; rewrite it without one"
            )
            continue
        try:
            json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            problems.append(f"{path.relative_to(ROOT)} is not valid JSON: {exc}")

    print(f"handlers:           {len(handlers)} methods")
    print(f"privileged modules: {len(modules)} files, {len(all_privileged)} methods")
    print(f"preregistered:      {len(prereg)}")
    for change in changes:
        print(f"FIXED  {change}")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nall layers agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

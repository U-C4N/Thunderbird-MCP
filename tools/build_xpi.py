#!/usr/bin/env python3
"""Build the bridge XPI from a checkout.

    python tools/build_xpi.py [output-dir]

Thin wrapper around `tbmcp.addon_build`, which is also what `tbmcp install-addon`
uses, so the packaging path is identical whether you build here or ship a wheel.
"""

from __future__ import annotations

import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from tbmcp.addon_build import addon_id, addon_source_dir, addon_version, build_xpi


def main() -> int:
    dest = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "build")
    src = addon_source_dir()
    xpi = build_xpi(dest, src=src)

    print(f"source      {src}")
    print(f"add-on      {addon_id(src)} {addon_version(src)}")
    print(f"package     {xpi}  ({xpi.stat().st_size:,} bytes)")
    with zipfile.ZipFile(xpi) as zf:
        names = zf.namelist()
        implementation = zf.read("experiment/implementation.js").decode("utf-8")
    print("contents:")
    for name in names:
        assert "\\" not in name, f"backslash in entry name: {name!r}"
        print(f"  {name}")
    modules = [
        line.split('"')[1]
        for line in implementation.splitlines()
        if line.startswith("TBX_MODULE_NAMES.push(")
    ]
    print(f"privileged modules spliced in: {', '.join(modules) or 'NONE'}")
    print(f"implementation.js: {len(implementation.splitlines())} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

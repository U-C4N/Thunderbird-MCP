#!/usr/bin/env python3
"""Run the bootstrapper straight out of a clone, before anything is installed.

Loaded by path rather than imported: `src/` is not on `sys.path` yet, and the whole
point of this entry point is that nothing has been installed to put it there. The
loaded module is registered in `sys.modules` before it runs — dataclasses resolve
their own annotations against that entry, and skipping it leaves the lookup with
nothing to find.
"""

import importlib.util
import pathlib
import sys

SOURCE = pathlib.Path(__file__).resolve().parent / "src" / "tbmcp" / "bootstrap.py"

spec = importlib.util.spec_from_file_location("tbmcp_bootstrap", SOURCE)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

raise SystemExit(module.main(sys.argv[1:]))

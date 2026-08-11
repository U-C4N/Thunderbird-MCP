# tests/test_bootstrap_entry.py
"""The three ways in, and the constraint that makes the first one possible."""

from __future__ import annotations

import os
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
        "sys.modules[spec.name] = module\n"
        "spec.loader.exec_module(module)\n"
        "print(module.VERSION)\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = ""  # nothing from the caller's environment puts tbmcp on the path
    done = subprocess.run(
        # `-S` matters as much as PYTHONPATH: `sys.executable` here is this repo's own
        # dev venv, which has tbmcp installed into its site-packages. Without `-S` an
        # `import tbmcp` slipped into bootstrap.py would resolve anyway and this test
        # would never catch it.
        [sys.executable, "-I", "-S", str(script)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
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


def test_cli_exposes_detect_clients_for_bootstrap_to_shell_out_to():
    from tbmcp.cli import build_parser

    args = build_parser().parse_args(["detect-clients"])
    assert args.command == "detect-clients"
    assert args.func.__name__ == "cmd_detect_clients"


def test_main_returns_nonzero_when_a_step_fails(monkeypatch, capsys):
    from tbmcp import bootstrap as module

    monkeypatch.setattr(
        module,
        "bootstrap",
        lambda options, **kw: module.Report(False, "1.2.0", None, [], "tbmcp doctor"),
    )
    assert module.main(["--json"]) == 1
    assert '"ok": false' in capsys.readouterr().out

# tests/test_bootstrap_entry.py
"""The three ways in, and the constraint that makes the first one possible."""

from __future__ import annotations

import json
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


def test_root_shim_actually_runs_and_produces_the_five_key_contract(tmp_path):
    """I6: the previous test only read the shim's *text* — delete its entire body
    and it would still pass, since both words also appear in the docstring alone.
    `python bootstrap.py`, the first command in the README and the one that breaks
    the chicken-and-egg (nothing is installed yet), was not exercised by any test.
    This actually runs it, out of a clean cwd, and checks the real output contract.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    done = subprocess.run(
        [sys.executable, "-I", "-S", str(ROOT / "bootstrap.py"), "--dry-run", "--json"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=60,
    )
    assert done.returncode in (0, 1), done.stderr
    payload = json.loads(done.stdout)
    assert set(payload) == {"ok", "version", "launcher", "steps", "next_command"}
    assert payload["version"] == "1.2.0"
    assert payload["steps"]  # ran the real step sequence, not a stub


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


def test_main_exits_zero_for_a_clean_dry_run(monkeypatch, capsys):
    """New breakage #2 from the final re-review: `ok` stays `false` for every dry run
    on purpose (nothing was verified), but a dry run that hit no `failed` step did
    everything it was asked to. An agent reading the exit code must not see that as a
    failure the way it would see a real `Stopped.` run.
    """
    from tbmcp import bootstrap as module

    steps = [module.Step("interpreter", "ok", 0.0, "chose python")]
    monkeypatch.setattr(
        module,
        "bootstrap",
        lambda options, **kw: module.Report(False, "1.2.0", None, steps, "python bootstrap.py"),
    )
    assert module.main(["--dry-run", "--json"]) == 0
    out = capsys.readouterr().out
    assert '"ok": false' in out  # the JSON contract itself must not regress


def test_main_still_exits_nonzero_when_a_dry_run_step_failed(monkeypatch, capsys):
    """The other half of #2: a dry run that genuinely hit a `failed` step keeps
    exiting 1 — only a dry run that completed cleanly gets the exit-0 treatment.
    """
    from tbmcp import bootstrap as module

    steps = [
        module.Step("interpreter", "failed", 0.0, "no usable interpreter found"),
    ]
    monkeypatch.setattr(
        module,
        "bootstrap",
        lambda options, **kw: module.Report(
            False, "1.2.0", None, steps, "python bootstrap.py --python <path>"
        ),
    )
    assert module.main(["--dry-run", "--json"]) == 1
    assert '"ok": false' in capsys.readouterr().out

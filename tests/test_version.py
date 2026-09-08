"""The three version strings drift apart the moment nothing checks them."""

from __future__ import annotations

import json
import pathlib
import tomllib

from tbmcp import server

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXPECTED = "1.3.0"


def _pyproject_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def _manifest_version() -> str:
    return json.loads((ROOT / "addon" / "manifest.json").read_text(encoding="utf-8"))["version"]


def test_all_three_versions_agree():
    assert _pyproject_version() == EXPECTED
    assert _manifest_version() == EXPECTED
    assert server._version() == EXPECTED


def test_fallback_matches_packaging(monkeypatch):
    """With the distribution missing, the hardcoded fallback must still be right."""

    def explode(_name: str) -> str:
        raise RuntimeError("not installed")

    monkeypatch.setattr("importlib.metadata.version", explode)
    assert server._version() == EXPECTED

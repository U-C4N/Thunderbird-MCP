"""The XPI build.

Two of these guard against traps that cost real debugging time against Thunderbird
153, and both fail silently at the Python level:

- backslash separators in zip entry names (what .NET produces on Windows) make
  Thunderbird reject the package with ERROR_CORRUPT_FILE and a
  NS_ERROR_FILE_NOT_FOUND on the first nested path;
- a privileged module that is not spliced in loads nothing, with no error anywhere.
"""

from __future__ import annotations

import pathlib
import zipfile

import pytest

from tbmcp.addon_build import (
    MODULE_MARKER,
    addon_id,
    addon_source_dir,
    addon_version,
    assemble_implementation,
    build_xpi,
)


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> tuple[pathlib.Path, list[str], str]:
    dest = tmp_path_factory.mktemp("xpi")
    xpi = build_xpi(dest)
    with zipfile.ZipFile(xpi) as zf:
        names = zf.namelist()
        implementation = zf.read("experiment/implementation.js").decode("utf-8")
    return xpi, names, implementation


def test_entry_names_use_forward_slashes(built) -> None:
    _, names, _ = built
    for name in names:
        assert "\\" not in name, f"backslash in {name!r} — Thunderbird will reject this"


def test_manifest_is_first(built) -> None:
    _, names, _ = built
    assert names[0] == "manifest.json"


def test_sources_that_are_only_assembled_do_not_ship(built) -> None:
    _, names, _ = built
    assert "experiment/core.js" not in names
    assert not [n for n in names if n.startswith("experiment/modules/")]
    assert "experiment/implementation.js" in names


def test_every_privileged_module_is_spliced(built) -> None:
    _, _, implementation = built
    modules = sorted(p.stem for p in (addon_source_dir() / "experiment" / "modules").glob("*.js"))
    assert modules, "no privileged modules found at all"
    for name in modules:
        assert f'TBX_MODULE_NAMES.push("{name}")' in implementation, f"{name} was not spliced"
    assert MODULE_MARKER not in implementation, "the splice marker survived into the output"


def test_every_background_script_ships(built) -> None:
    """A background script listed in the manifest but missing from the package stops
    the whole add-on loading."""
    import json

    _, names, _ = built
    manifest = json.loads((addon_source_dir() / "manifest.json").read_text(encoding="utf-8"))
    for script in manifest["background"]["scripts"]:
        assert script in names, f"{script} is in the manifest but not in the package"


def test_manifest_permissions_are_known_to_thunderbird() -> None:
    """An unknown permission is not a no-op.

    Thunderbird 155 logs `Error processing permissions.17: Value "messages.tags"`
    at install time; the real names are messagesTags and messagesTagsList, which
    are already listed.
    """
    import json

    manifest = json.loads((addon_source_dir() / "manifest.json").read_text(encoding="utf-8"))
    assert "messages.tags" not in manifest["permissions"]
    assert {"messagesTags", "messagesTagsList"} <= set(manifest["permissions"])


def test_build_is_reproducible(tmp_path) -> None:
    first = build_xpi(tmp_path / "a")
    second = build_xpi(tmp_path / "b")
    assert first.name == second.name, "content hash differs between identical builds"
    assert first.read_bytes() == second.read_bytes()


def test_rebuilding_the_same_content_is_a_no_op(tmp_path) -> None:
    first = build_xpi(tmp_path)
    stamp = first.stat().st_mtime_ns
    again = build_xpi(tmp_path)
    assert again == first
    assert again.stat().st_mtime_ns == stamp


def test_filename_carries_version_and_hash(built) -> None:
    xpi, _, _ = built
    assert xpi.name.startswith(f"tbmcp-bridge-{addon_version()}-")
    assert xpi.suffix == ".xpi"
    # Gecko caches jar files by path, so a changed package must not reuse a name.
    assert len(xpi.stem.rsplit("-", 1)[-1]) == 12


def test_addon_identity() -> None:
    assert addon_id() == "bridge@thunderbird-mcp"
    assert addon_version().count(".") >= 1


def test_missing_marker_is_an_error(tmp_path) -> None:
    source = tmp_path / "addon"
    (source / "experiment" / "modules").mkdir(parents=True)
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    (source / "experiment" / "core.js").write_text("// no marker", encoding="utf-8")
    (source / "experiment" / "modules" / "x.js").write_text(
        'TBX_MODULE_NAMES.push("x");', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="marker"):
        assemble_implementation(source)


def test_a_module_that_does_not_announce_itself_is_rejected(tmp_path) -> None:
    """Silent loading is the worst failure mode here, so the build refuses it."""
    source = tmp_path / "addon"
    (source / "experiment" / "modules").mkdir(parents=True)
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    (source / "experiment" / "core.js").write_text(MODULE_MARKER, encoding="utf-8")
    (source / "experiment" / "modules" / "quiet.js").write_text(
        'TBX_MODULES["quiet.thing"] = async () => 1;', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="TBX_MODULE_NAMES"):
        assemble_implementation(source)


def test_the_globals_the_sandbox_lacks_are_imported(built) -> None:
    """Two things the privileged half needs and the ext-*.js sandbox does not inject.

    A bare `setTimeout` is a ReferenceError there, which surfaces only as a
    capability that never answers; and without `ExtensionError` every failure
    reaches the background page as "An unexpected error occurred".
    """
    _, _, implementation = built
    assert "resource://gre/modules/Timer.sys.mjs" in implementation
    assert "resource://gre/modules/ExtensionUtils.sys.mjs" in implementation


def test_nothing_type_checks_a_date_by_instance(built) -> None:
    """Thunderbird hands the privileged half objects minted in its own realms.

    `value instanceof Date` is false for every Date gloda produces, which is how
    each search hit came back with `date: null`.
    """
    _, _, implementation = built
    assert "instanceof Date" not in implementation

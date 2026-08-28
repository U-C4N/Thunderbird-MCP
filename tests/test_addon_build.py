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


# --------------------------------------------------------- privileged sandbox rules
#
# The privileged half runs in the ext-*.js sandbox that
# ExtensionCommon.sys.mjs::_createExtGlobal builds: system principal,
# `wantGlobalProperties: ["ChromeUtils"]`, and an explicit Object.assign of
# Services / Cc / Ci / Cu / Cr / IOUtils / PathUtils / XPCOMUtils. It is not a DOM
# and it is not the same realm as the modules it talks to. Both facts have already
# cost a release: each rule below marks a bug that shipped, produced no error the
# Python layer could see, and silently disabled a whole toolset.


def test_the_privileged_half_imports_its_own_timers(built) -> None:
    """`setTimeout` is a DOM global and the sandbox has no DOM.

    Calling it threw ReferenceError inside every H.withTimeout(), which rejected the
    deadline before the work it guarded had started — taking out gloda search,
    conversation lookup and the calendar/filters/junk deadlines at once.
    """
    _, _, implementation = built
    assert "resource://gre/modules/Timer.sys.mjs" in implementation, (
        "the privileged half must import setTimeout/clearTimeout from Timer.sys.mjs; "
        "there is no timer function on the ext-*.js sandbox global"
    )


def test_the_privileged_half_does_not_brand_check_across_realms(built) -> None:
    """`instanceof` is false for objects minted in another realm.

    Gloda builds its Date objects in the shared system global, so `value instanceof
    Date` in the sandbox was false for every one of them and every search hit came
    back with `date: null` — which also flattened conversation ordering.
    """
    _, _, implementation = built
    assert 'typeof value.getTime === "function"' in implementation, (
        "dates coming back from gloda must be duck-typed, not brand-checked; a Date "
        "built in another realm fails instanceof"
    )


def test_privileged_errors_survive_the_api_boundary(built) -> None:
    """ExtensionCommon.normalizeError keeps a message only for a plain object, an
    ExtensionError, or an error the extension's principal subsumes.

    A plain `new Error` raised here is none of those, so it reached the caller as
    "An unexpected error occurred" with the real text left in the Error Console.
    That is what made the timer bug above take three days to find.
    """
    _, _, implementation = built
    assert "resource://gre/modules/ExtensionUtils.sys.mjs" in implementation, (
        "errors leaving invoke() must be wrapped in ExtensionError, or their message "
        "is replaced with a generic string before anyone sees it"
    )

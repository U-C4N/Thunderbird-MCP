"""Building the XPI.

Two things here are not optional, both learned the hard way against Thunderbird 153:

1. Zip entry names must use forward slashes. `nsIZipReader` takes them literally,
   so a `\\`-separated entry (what .NET's `ZipFile.CreateFromDirectory` produces on
   Windows) makes the install fail with `ERROR_CORRUPT_FILE` and a
   `NS_ERROR_FILE_NOT_FOUND` on the first nested path.
2. Gecko caches jar files by path, so reinstalling changed content at the same
   filename can re-read the stale zip. Output names carry a content hash.

The privileged half is assembled here too: `experiment/core.js` plus every
`experiment/modules/*.js`, spliced at the `===TBX_MODULES===` marker. See the
comment in core.js for why this is a build step rather than a runtime loader.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import zipfile

MODULE_MARKER = "/* ===TBX_MODULES=== (build_xpi.py splices experiment/modules/*.js here) */"

#: Sources that exist only to be assembled; they must not ship as separate files.
EXCLUDED = ("experiment/core.js",)
EXCLUDED_DIRS = ("experiment/modules",)
SKIP_NAMES = {".DS_Store", "Thumbs.db"}
SKIP_SUFFIXES = {".xpi", ".pyc", ".md"}


def addon_source_dir() -> pathlib.Path:
    """The add-on sources, whether we are in a checkout or an installed wheel."""
    packaged = pathlib.Path(__file__).parent / "_addon"
    if (packaged / "manifest.json").is_file():
        return packaged
    checkout = pathlib.Path(__file__).resolve().parents[2] / "addon"
    if (checkout / "manifest.json").is_file():
        return checkout
    raise FileNotFoundError(
        "could not find the add-on sources; expected tbmcp/_addon or <repo>/addon"
    )


def assemble_implementation(src: pathlib.Path) -> str:
    """core.js + modules/*.js, in a fixed order so the output is reproducible."""
    core_path = src / "experiment" / "core.js"
    core = core_path.read_text(encoding="utf-8")
    if MODULE_MARKER not in core:
        raise ValueError(f"{core_path} has no ===TBX_MODULES=== marker")

    modules_dir = src / "experiment" / "modules"
    module_files = sorted(modules_dir.glob("*.js")) if modules_dir.is_dir() else []
    if not module_files:
        raise ValueError(f"no privileged modules found in {modules_dir}")

    chunks: list[str] = []
    for path in module_files:
        body = path.read_text(encoding="utf-8")
        if "TBX_MODULE_NAMES.push(" not in body:
            raise ValueError(
                f"{path.name} never calls TBX_MODULE_NAMES.push(...) — it would load "
                "invisibly and be impossible to diagnose"
            )
        chunks.append(
            f"/* ---------------------------------------------------------------\n"
            f" * experiment/modules/{path.name}\n"
            f" * --------------------------------------------------------------- */\n"
            f"{body.rstrip()}\n"
        )
    return core.replace(MODULE_MARKER, "\n".join(chunks))


def _entries(src: pathlib.Path) -> list[tuple[str, bytes]]:
    """Every file that goes into the XPI, as (arcname, bytes)."""
    out: list[tuple[str, bytes]] = []
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        arcname = path.relative_to(src).as_posix()
        if (
            path.name in SKIP_NAMES
            or path.suffix in SKIP_SUFFIXES
            or "__pycache__" in path.parts
            or arcname in EXCLUDED
            or any(arcname.startswith(d + "/") for d in EXCLUDED_DIRS)
        ):
            continue
        out.append((arcname, path.read_bytes()))
    out.append(("experiment/implementation.js", assemble_implementation(src).encode("utf-8")))
    # manifest.json first, then a stable order.
    out.sort(key=lambda pair: (pair[0] != "manifest.json", pair[0]))
    return out


def addon_version(src: pathlib.Path | None = None) -> str:
    src = src or addon_source_dir()
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    return str(manifest.get("version", "0"))


def addon_id(src: pathlib.Path | None = None) -> str:
    src = src or addon_source_dir()
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    gecko = (manifest.get("browser_specific_settings") or {}).get("gecko") or {}
    return str(gecko.get("id") or "bridge@thunderbird-mcp")


def build_xpi(dest_dir: pathlib.Path, *, src: pathlib.Path | None = None) -> pathlib.Path:
    """Write `<dest_dir>/tbmcp-bridge-<version>-<hash>.xpi` and return its path."""
    src = src or addon_source_dir()
    entries = _entries(src)

    digest = hashlib.sha256()
    for arcname, data in entries:
        digest.update(arcname.encode("utf-8"))
        digest.update(data)
    short = digest.hexdigest()[:12]

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"tbmcp-bridge-{addon_version(src)}-{short}.xpi"
    if dest.exists():
        return dest

    tmp = dest.with_suffix(".xpi.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, data in entries:
            assert "\\" not in arcname, arcname
            # A fixed timestamp keeps the archive byte-identical for identical input.
            info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    tmp.replace(dest)
    return dest

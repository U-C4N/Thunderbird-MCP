"""Pack an add-on directory into an XPI.

Gecko's nsIZipReader takes entry names literally, so separators MUST be forward
slashes. PowerShell's [IO.Compression.ZipFile]::CreateFromDirectory on .NET
Framework writes backslashes, which makes the XPI unreadable (ERROR_CORRUPT_FILE
/ NS_ERROR_FILE_NOT_FOUND on nested paths). Always build with this script.
"""

from __future__ import annotations

import pathlib
import sys
import zipfile

SKIP_NAMES = {".DS_Store", "Thumbs.db"}
SKIP_SUFFIXES = {".xpi", ".pyc"}


def build(src: pathlib.Path, dest: pathlib.Path) -> pathlib.Path:
    if not (src / "manifest.json").is_file():
        raise SystemExit(f"no manifest.json in {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    files = [
        p
        for p in sorted(src.rglob("*"))
        if p.is_file()
        and p.name not in SKIP_NAMES
        and p.suffix not in SKIP_SUFFIXES
        and "__pycache__" not in p.parts
    ]
    # manifest.json first: harmless, but matches what other tooling produces.
    files.sort(key=lambda p: (p.relative_to(src).as_posix() != "manifest.json",))
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            arcname = path.relative_to(src).as_posix()  # forward slashes, always
            assert "\\" not in arcname, arcname
            zf.writestr(zipfile.ZipInfo(arcname), path.read_bytes(), zipfile.ZIP_DEFLATED)
    return dest


def main() -> int:
    src = pathlib.Path(sys.argv[1]).resolve()
    dest = pathlib.Path(sys.argv[2]).resolve()
    out = build(src, dest)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    print(f"built {out} ({out.stat().st_size} bytes)")
    for name in names:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

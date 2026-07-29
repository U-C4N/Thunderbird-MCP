"""Locating Thunderbird on disk, and reading what it stores there.

Two jobs:

1. Find the profile directory, so the daemon can drop its pairing file where the
   add-on will look for it, and so `doctor` can explain what it found.
2. Read a few things straight off disk. This is the fallback path for when
   Thunderbird is closed — it is strictly read-only, and never touches a file
   Thunderbird might be writing.

Profile discovery follows `profiles.ini`, where a `Default=` inside an
`[Install<HASH>]` section is authoritative (that is the dedicated profile of the
installed build) and `Default=1` inside a `[Profile<N>]` section is the legacy
fallback. On the machine this was developed against those two disagree, so the
order matters.
"""

from __future__ import annotations

import configparser
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BRIDGE_FILE = "tbmcp-bridge.json"


@dataclass(frozen=True)
class ThunderbirdProfile:
    path: Path
    name: str
    is_default: bool
    root: Path
    """The directory holding profiles.ini — the packaging-specific install root."""

    @property
    def bridge_file(self) -> Path:
        return self.path / BRIDGE_FILE

    @property
    def addon_status_file(self) -> Path:
        """What the add-on wrote at startup; the only channel left when the bridge
        itself is broken. See `addon/background/main.js`."""
        return self.path / "tbmcp-addon-status.json"

    @property
    def prefs_js(self) -> Path:
        return self.path / "prefs.js"

    @property
    def gloda_db(self) -> Path:
        return self.path / "global-messages-db.sqlite"

    @property
    def address_book_db(self) -> Path:
        return self.path / "abook.sqlite"

    @property
    def calendar_db(self) -> Path:
        return self.path / "calendar-data" / "local.sqlite"


def candidate_roots() -> list[Path]:
    """Every place a Thunderbird `profiles.ini` may live on this platform."""
    home = Path.home()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        roots = [Path(appdata) / "Thunderbird"] if appdata else []
        return [*roots, home / "AppData" / "Roaming" / "Thunderbird"]
    if sys.platform == "darwin":
        return [
            home / "Library" / "Thunderbird",
            home / "Library" / "Application Support" / "Thunderbird",
        ]
    return [
        home / ".thunderbird",
        # Snap and Flatpak relocate the whole tree.
        home / "snap" / "thunderbird" / "common" / ".thunderbird",
        home / ".var" / "app" / "org.mozilla.Thunderbird" / ".thunderbird",
    ]


def _parse_profiles_ini(root: Path) -> list[ThunderbirdProfile]:
    ini_path = root / "profiles.ini"
    if not ini_path.is_file():
        return []
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    # profiles.ini is written by Gecko and can contain duplicate keys after
    # profile juggling; strict=False tolerates that.
    parser.read(ini_path, encoding="utf-8")

    def resolve(raw: str, is_relative: bool) -> Path:
        cleaned = raw.replace("/", os.sep)
        return (root / cleaned) if is_relative else Path(cleaned)

    # An [InstallXXXX] Default= wins: it is the profile the installed build uses.
    install_default: str | None = None
    for section in parser.sections():
        if section.lower().startswith("install") and parser.has_option(section, "Default"):
            install_default = parser.get(section, "Default")
            break

    profiles: list[ThunderbirdProfile] = []
    legacy_default: Path | None = None
    for section in parser.sections():
        if not re.fullmatch(r"Profile\d+", section):
            continue
        raw_path = parser.get(section, "Path", fallback=None)
        if not raw_path:
            continue
        is_relative = parser.get(section, "IsRelative", fallback="1").strip() == "1"
        path = resolve(raw_path, is_relative)
        name = parser.get(section, "Name", fallback=path.name)
        if parser.get(section, "Default", fallback="").strip() == "1":
            legacy_default = path
        profiles.append(ThunderbirdProfile(path=path, name=name, is_default=False, root=root))

    chosen: Path | None = None
    if install_default:
        chosen = resolve(install_default, True)
    elif legacy_default:
        chosen = legacy_default
    elif profiles:
        chosen = profiles[0].path

    return [
        ThunderbirdProfile(
            path=p.path,
            name=p.name,
            is_default=(chosen is not None and p.path == chosen),
            root=p.root,
        )
        for p in profiles
    ]


def list_profiles() -> list[ThunderbirdProfile]:
    """All profiles found across every candidate root, default first."""
    found: list[ThunderbirdProfile] = []
    for root in candidate_roots():
        for profile in _parse_profiles_ini(root):
            if profile.path.is_dir() and all(p.path != profile.path for p in found):
                found.append(profile)
    found.sort(key=lambda p: (not p.is_default, p.name))
    return found


def find_profile(explicit: str | os.PathLike[str] | None = None) -> ThunderbirdProfile | None:
    """Resolve the profile to operate on.

    `explicit` (from `--profile` or `TBMCP_PROFILE`) may be a directory path or a
    profile name from profiles.ini.
    """
    explicit = explicit or os.environ.get("TBMCP_PROFILE") or None
    profiles = list_profiles()
    if explicit:
        as_path = Path(explicit).expanduser()
        if as_path.is_dir():
            root = as_path.parent.parent
            return ThunderbirdProfile(path=as_path, name=as_path.name, is_default=False, root=root)
        for profile in profiles:
            if profile.name == str(explicit):
                return profile
        return None
    return profiles[0] if profiles else None


# --------------------------------------------------------------------------- prefs

_PREF_LINE = re.compile(
    r"""^\s*(?:user_)?pref\(\s*(["'])(?P<name>.*?)\1\s*,\s*(?P<value>.*?)\s*\)\s*;\s*$"""
)


def _decode_pref_value(raw: str) -> Any:
    raw = raw.strip()
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw and (raw[0] in "\"'"):
        quote = raw[0]
        body = raw[1:-1] if raw.endswith(quote) and len(raw) >= 2 else raw[1:]
        # Gecko writes JS string escapes; only these actually occur in prefs.js.
        return (
            body.replace("\\\\", "\x00")
            .replace('\\"', '"')
            .replace("\\'", "'")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\x00", "\\")
        )
    try:
        return int(raw, 10)
    except ValueError:
        return raw


def read_prefs(prefs_js: Path) -> dict[str, Any]:
    """Parse a `prefs.js` / `user.js` into a plain dict.

    Read-only by design. Writing prefs.js behind a running Thunderbird loses data,
    because Thunderbird rewrites the whole file from memory on shutdown — preference
    *writes* always go through the add-on.
    """
    if not prefs_js.is_file():
        return {}
    out: dict[str, Any] = {}
    for line in prefs_js.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _PREF_LINE.match(line)
        if match:
            out[match.group("name")] = _decode_pref_value(match.group("value"))
    return out


# ------------------------------------------------------------------- sqlite reads


def open_readonly(db_path: Path, *, copy_if_locked: bool = True) -> sqlite3.Connection:
    """Open a Thunderbird sqlite file without disturbing a running Thunderbird.

    `mode=ro` plus a short busy timeout is enough for the WAL databases most of the
    time. When Windows refuses the handle outright we fall back to a snapshot copy,
    which is what makes `tbmcp doctor` and the offline read path reliable.
    """
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    uri = f"file:{db_path.as_posix()}?mode=ro&immutable=0"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        if not copy_if_locked:
            raise
    tmp_dir = Path(tempfile.mkdtemp(prefix="tbmcp-"))
    snapshot = tmp_dir / db_path.name
    shutil.copy2(db_path, snapshot)
    for sidecar in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + sidecar)
        if side.is_file():
            shutil.copy2(side, snapshot.with_name(snapshot.name + sidecar))
    conn = sqlite3.connect(snapshot, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class ProfileSnapshot:
    """What we can tell about a profile without Thunderbird's help."""

    profile: ThunderbirdProfile
    prefs: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, profile: ThunderbirdProfile) -> ProfileSnapshot:
        return cls(profile=profile, prefs=read_prefs(profile.prefs_js))

    def accounts(self) -> list[dict[str, Any]]:
        """Accounts reconstructed from `mail.account.*` / `mail.server.*` prefs."""
        order = str(self.prefs.get("mail.accountmanager.accounts", "")).split(",")
        default = self.prefs.get("mail.accountmanager.defaultaccount")
        out: list[dict[str, Any]] = []
        for key in (k.strip() for k in order):
            if not key:
                continue
            server_key = self.prefs.get(f"mail.account.{key}.server")
            identity_keys = [
                i.strip()
                for i in str(self.prefs.get(f"mail.account.{key}.identities", "")).split(",")
                if i.strip()
            ]
            entry: dict[str, Any] = {
                "accountKey": key,
                "isDefault": key == default,
                "serverKey": server_key,
                "identityKeys": identity_keys,
            }
            if server_key:
                prefix = f"mail.server.{server_key}."
                entry["server"] = {
                    name[len(prefix) :]: value
                    for name, value in self.prefs.items()
                    if name.startswith(prefix)
                }
            entry["identities"] = [
                {
                    name[len(f"mail.identity.{ident}.") :]: value
                    for name, value in self.prefs.items()
                    if name.startswith(f"mail.identity.{ident}.")
                }
                for ident in identity_keys
            ]
            out.append(entry)
        return out

    def outgoing_servers(self) -> list[dict[str, Any]]:
        keys = [
            k.strip() for k in str(self.prefs.get("mail.smtpservers", "")).split(",") if k.strip()
        ]
        return [
            {
                "serverKey": key,
                **{
                    name[len(f"mail.smtpserver.{key}.") :]: value
                    for name, value in self.prefs.items()
                    if name.startswith(f"mail.smtpserver.{key}.")
                },
            }
            for key in keys
        ]

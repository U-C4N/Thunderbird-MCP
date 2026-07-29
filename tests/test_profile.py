"""Profile discovery and on-disk parsing.

The profiles.ini test is not hypothetical: on the machine this was developed
against, the `[InstallD78BF5DD33499EC2]` section points at
`fw0oundg.default-release` while `[Profile1]` carries `Default=1` and points at a
different, stale profile. Preferring the wrong one would make the daemon write its
pairing file where Thunderbird never looks, and the failure would look like "the
add-on is not installed".
"""

from __future__ import annotations

import pathlib

from tbmcp.profile import ProfileSnapshot, ThunderbirdProfile, _parse_profiles_ini, read_prefs


def _write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestProfilesIni:
    def test_install_section_beats_legacy_default(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "Profiles" / "aaaa.default-release").mkdir(parents=True)
        (tmp_path / "Profiles" / "bbbb.default").mkdir(parents=True)
        _write(
            tmp_path / "profiles.ini",
            """
[InstallD78BF5DD33499EC2]
Default=Profiles/aaaa.default-release
Locked=1

[Profile0]
Name=default-release
IsRelative=1
Path=Profiles/aaaa.default-release

[Profile1]
Name=default
IsRelative=1
Path=Profiles/bbbb.default
Default=1

[General]
StartWithLastProfile=1
Version=2
""".strip(),
        )
        profiles = _parse_profiles_ini(tmp_path)
        assert len(profiles) == 2
        default = [p for p in profiles if p.is_default]
        assert len(default) == 1
        assert default[0].path.name == "aaaa.default-release"

    def test_legacy_default_is_used_when_no_install_section(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "Profiles" / "only.default").mkdir(parents=True)
        _write(
            tmp_path / "profiles.ini",
            "[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/only.default\nDefault=1\n",
        )
        profiles = _parse_profiles_ini(tmp_path)
        assert profiles[0].is_default

    def test_absolute_paths_are_honoured(self, tmp_path: pathlib.Path) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        _write(
            tmp_path / "profiles.ini",
            f"[Profile0]\nName=x\nIsRelative=0\nPath={elsewhere}\nDefault=1\n",
        )
        assert _parse_profiles_ini(tmp_path)[0].path == elsewhere

    def test_missing_ini_is_not_an_error(self, tmp_path: pathlib.Path) -> None:
        assert _parse_profiles_ini(tmp_path) == []

    def test_duplicate_keys_are_tolerated(self, tmp_path: pathlib.Path) -> None:
        # Gecko has been known to leave duplicates behind after profile juggling.
        _write(
            tmp_path / "profiles.ini",
            "[Profile0]\nName=a\nName=a\nIsRelative=1\nPath=Profiles/a\nDefault=1\n",
        )
        assert _parse_profiles_ini(tmp_path)[0].name == "a"


class TestPrefs:
    def test_types_and_escapes(self, tmp_path: pathlib.Path) -> None:
        prefs = read_prefs(
            _write(
                tmp_path / "prefs.js",
                r"""
// Mozilla User Preferences
user_pref("mail.pane_config.dynamic", 2);
user_pref("mail.biff.play_sound", true);
user_pref("mail.server.server1.hostname", "mail.example.com");
user_pref("mail.identity.id1.htmlSigText", "line one\nline \"two\"");
user_pref("path", "C:\\Users\\someone");
user_pref("negative", -1);
pref("defaults.only", "kept");
""".strip(),
            )
        )
        assert prefs["mail.pane_config.dynamic"] == 2
        assert prefs["mail.biff.play_sound"] is True
        assert prefs["mail.server.server1.hostname"] == "mail.example.com"
        assert prefs["mail.identity.id1.htmlSigText"] == 'line one\nline "two"'
        assert prefs["path"] == r"C:\Users\someone"
        assert prefs["negative"] == -1
        assert prefs["defaults.only"] == "kept"

    def test_comments_and_blank_lines_are_ignored(self, tmp_path: pathlib.Path) -> None:
        prefs = read_prefs(_write(tmp_path / "prefs.js", '\n// user_pref("commented.out", 1);\n\n'))
        assert prefs == {}

    def test_missing_file_is_empty(self, tmp_path: pathlib.Path) -> None:
        assert read_prefs(tmp_path / "nope.js") == {}


class TestSnapshot:
    def _snapshot(self, tmp_path: pathlib.Path) -> ProfileSnapshot:
        _write(
            tmp_path / "prefs.js",
            "\n".join(
                [
                    'user_pref("mail.accountmanager.accounts", "account1,account2");',
                    'user_pref("mail.accountmanager.defaultaccount", "account1");',
                    'user_pref("mail.account.account1.server", "server1");',
                    'user_pref("mail.account.account1.identities", "id1");',
                    'user_pref("mail.account.account2.server", "server2");',
                    'user_pref("mail.server.server1.hostname", "imap.example.com");',
                    'user_pref("mail.server.server1.type", "imap");',
                    'user_pref("mail.server.server1.port", 993);',
                    'user_pref("mail.server.server2.type", "none");',
                    'user_pref("mail.identity.id1.useremail", "me@example.com");',
                    'user_pref("mail.identity.id1.fullName", "Me");',
                    'user_pref("mail.smtpservers", "smtp1");',
                    'user_pref("mail.smtpserver.smtp1.hostname", "smtp.example.com");',
                    'user_pref("mail.smtpserver.smtp1.port", 587);',
                ]
            ),
        )
        profile = ThunderbirdProfile(
            path=tmp_path, name="test", is_default=True, root=tmp_path.parent
        )
        return ProfileSnapshot.load(profile)

    def test_accounts_are_reconstructed_from_prefs(self, tmp_path: pathlib.Path) -> None:
        accounts = self._snapshot(tmp_path).accounts()
        assert [a["accountKey"] for a in accounts] == ["account1", "account2"]
        first = accounts[0]
        assert first["isDefault"] is True
        assert first["server"]["hostname"] == "imap.example.com"
        assert first["server"]["port"] == 993
        assert first["identities"][0]["useremail"] == "me@example.com"
        # The Local Folders account has no identities, and that is not an error.
        assert accounts[1]["identities"] == []

    def test_outgoing_servers(self, tmp_path: pathlib.Path) -> None:
        servers = self._snapshot(tmp_path).outgoing_servers()
        assert servers == [{"serverKey": "smtp1", "hostname": "smtp.example.com", "port": 587}]

    def test_bridge_file_lives_in_the_profile(self, tmp_path: pathlib.Path) -> None:
        profile = ThunderbirdProfile(path=tmp_path, name="t", is_default=True, root=tmp_path)
        assert profile.bridge_file == tmp_path / "tbmcp-bridge.json"
        assert profile.prefs_js.name == "prefs.js"
        assert profile.calendar_db.parent.name == "calendar-data"

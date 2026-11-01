"""Create an isolated Thunderbird profile, install this checkout, run native checks.

Example (Windows):
  python tools/live_profile.py --binary "C:/Program Files/Mozilla Thunderbird/thunderbird.exe"

Requires an editable development install. The chosen binary is never downloaded
or replaced. A fresh directory under build/ is retained with reports and test
mail. Every privileged request verifies the exact temporary profile first.
Cleanup quits that profile or terminates only the process handle we launched;
it never enumerates or stops other Thunderbird processes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

from tbmcp.addon_build import addon_id, build_xpi
from tbmcp.addon_install import INSTALL_SCRIPT
from tbmcp.marionette import Marionette

CHECKOUT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class LiveProfile:
    def __init__(self, *, binary: Path, root: Path | None = None) -> None:
        self.binary = binary.resolve(strict=True)
        self.root = (root or CHECKOUT / "build" / ("live-" + secrets.token_hex(6))).resolve()
        if self.root.exists():
            raise ValueError("Use a fresh test directory; existing profiles are never reused.")
        if not self.binary.is_file():
            raise ValueError("--binary must name a Thunderbird executable.")
        self.profile = self.root / "profile"
        self.session_file = self.root / "session.json"
        self.port = free_port()
        self.smtp_port = free_port()
        while self.smtp_port == self.port:
            self.smtp_port = free_port()
        self.process: subprocess.Popen | None = None

    def _prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=False)
        self.profile.mkdir()
        mail = self.profile / "Mail" / "Local Folders"
        mail.mkdir(parents=True)
        prefs = {
            "marionette.port": self.port,
            "mail.provider.enabled": False,
            "mail.provider.suppress_dialog_on_startup": True,
            "mail.shell.checkDefaultClient": False,
            "mailnews.start_page.enabled": False,
            "app.update.enabled": False,
            "datareporting.policy.dataSubmissionEnabled": False,
            "toolkit.telemetry.enabled": False,
            "mailnews.database.global.indexer.enabled": False,
            "mail.accountmanager.accounts": "account1",
            "mail.accountmanager.defaultaccount": "account1",
            "mail.accountmanager.localfoldersserver": "server1",
            "mail.account.account1.server": "server1",
            "mail.account.account1.identities": "id1",
            "mail.server.server1.type": "none",
            "mail.server.server1.hostname": "Local Folders",
            "mail.server.server1.userName": "nobody",
            "mail.server.server1.name": "Isolated fixture mail",
            "mail.server.server1.directory": str(mail),
            "mail.identity.id1.fullName": "MCP fixture",
            "mail.identity.id1.useremail": "sender@example.invalid",
            "mail.identity.id1.draft_folder": "mailbox://nobody@Local%20Folders/Drafts",
            "mail.identity.id1.fcc_folder": "mailbox://nobody@Local%20Folders/Sent",
            "mail.identity.id1.stationery_folder": "mailbox://nobody@Local%20Folders/Templates",
            "mail.identity.id1.compose_html": False,
            "mail.identity.id1.smtpServer": "smtp1",
            "mail.smtpservers": "smtp1",
            "mail.smtp.defaultserver": "smtp1",
            "mail.smtpserver.smtp1.hostname": "127.0.0.1",
            "mail.smtpserver.smtp1.port": self.smtp_port,
            "mail.smtpserver.smtp1.authMethod": 1,
            "mail.smtpserver.smtp1.try_ssl": 0,
            "mail.smtpserver.smtp1.username": "sender@example.invalid",
            "mailnews.send_default_charset": "UTF-8",
            "mailnews.sendformat.auto_downgrade": False,
        }
        (self.profile / "user.js").write_text(
            "\n".join(
                f"user_pref({json.dumps(key)}, {json.dumps(value)});"
                for key, value in prefs.items()
            )
            + "\n",
            encoding="utf-8",
        )

    def _connection(self, timeout: float = 3) -> Marionette:
        connection = Marionette(port=self.port, timeout=timeout)
        try:
            connection.start_chrome_session()
            actual = connection.execute(
                'arguments[arguments.length-1](Services.dirsvc.get("ProfD",Ci.nsIFile).path);'
            )
            if Path(actual).resolve() != self.profile:
                raise RuntimeError(
                    "Refusing automation: the connected profile is not this fixture."
                )
            return connection
        except BaseException:
            connection.close()
            raise

    def chrome(self, script: str, args: list | None = None):
        connection = self._connection(timeout=90)
        try:
            return connection.execute(script, args or [])
        finally:
            connection.close()

    def start(self) -> Path:
        self._prepare()
        options = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            "env": {**os.environ, "MOZ_REMOTE_ALLOW_SYSTEM_ACCESS": "1"},
        }
        if sys.platform == "win32":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            options["startupinfo"] = startup
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        self.process = subprocess.Popen(
            [
                str(self.binary),
                "-no-remote",
                "-profile",
                str(self.profile),
                "-marionette",
                "-remote-allow-system-access",
            ],
            **options,
        )
        deadline = time.monotonic() + 45
        try:
            last = None
            while time.monotonic() < deadline:
                try:
                    connection = self._connection()
                    connection.close()
                    break
                except Exception as exc:
                    last = exc
                    time.sleep(0.25)
            else:
                raise RuntimeError(f"The isolated profile did not start: {last}")
            app = self.chrome(
                "arguments[arguments.length-1]({version:Services.appinfo.version, "
                "processId:Services.appinfo.processID});"
            )
            self.session_file.write_text(
                json.dumps(
                    {
                        "profile": str(self.profile),
                        "port": self.port,
                        "smtpPort": self.smtp_port,
                        "pid": app["processId"],
                        "launcherPid": self.process.pid,
                        "binary": str(self.binary),
                        "version": app["version"],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            source = CHECKOUT / "addon"
            xpi = build_xpi(self.root / "addon", src=source)
            installed = self.chrome(INSTALL_SCRIPT, [str(xpi), addon_id(source)])
            (self.root / "install-result.json").write_text(
                json.dumps(installed, indent=2), encoding="utf-8"
            )
            result = installed.get("result", {})
            if not result.get("ok") or not result.get("isActive"):
                raise RuntimeError(f"The candidate add-on could not load: {installed}")
            return self.session_file
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        if self.process is None:
            return
        connection = None
        try:
            connection = self._connection()
            # This profile contains fixtures only. Dismiss its recovery composers
            # before quit so failed acceptance tests cannot leave a save prompt.
            connection.execute(
                'for(const w of Services.wm.getEnumerator("")){'
                "if(w.gMsgCompose){w.SetContentAndBodyAsUnmodified();w.close();}}"
                "arguments[arguments.length-1](true);"
            )
            connection.quit_application()
        except Exception:
            pass
        finally:
            if connection is not None:
                connection.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # The Popen handle identifies exactly the launched process, even if
            # other Thunderbird installations/profiles are running concurrently.
            self.process.terminate()
            self.process.wait(timeout=10)
        with contextlib.suppress(OSError):
            (self.profile / "tbmcp-bridge.json").unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--suite", choices=["drafts", "workflows", "followups", "none"], default="drafts"
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Leave only this fixture running for further checks.",
    )
    args = parser.parse_args()
    fixture = LiveProfile(binary=args.binary, root=args.root)
    try:
        session = fixture.start()
        print(f"Isolated session: {session}", flush=True)
        if args.suite == "drafts":
            import smoke_drafts

            smoke_drafts.configure(session)
            asyncio.run(smoke_drafts.main())
        elif args.suite == "workflows":
            import smoke_workflows

            asyncio.run(smoke_workflows.main(session, skip_install=True))
        elif args.suite == "followups":
            import smoke_followups

            asyncio.run(smoke_followups.main(session))
    finally:
        if not args.keep_open:
            fixture.stop()


if __name__ == "__main__":
    main()

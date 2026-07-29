"""Installing the bridge add-on into Thunderbird, ideally without the user clicking.

Release Thunderbird 153 builds `MOZ_REQUIRE_SIGNING=false` and default
`extensions.experiments.enabled=true`, so an unsigned add-on carrying
`experiment_apis` installs and gets full privileges. Two routes exist:

- **automatic** — relaunch Thunderbird with Marionette, call `AddonManager`, relaunch
  it normally. No clicks, and it also works for upgrades. Requires closing
  Thunderbird twice, which we always tell the user about first.
- **manual** — print the XPI path and the three menu clicks. Always available as a
  fallback, and the only option if the user does not want us touching their windows.

Note for anyone tempted to simplify: copying the XPI into `<profile>/extensions/`
does **not** work on modern Gecko. The directory is no longer scanned, and the file
is deleted on the next start.
"""

from __future__ import annotations

import logging
import os
import pathlib
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

from . import marionette
from .addon_build import addon_id, addon_source_dir, addon_version, build_xpi
from .errors import TbmcpError, UnsupportedError
from .ipc import state_dir
from .profile import ThunderbirdProfile

log = logging.getLogger("tbmcp.install")

INSTALL_SCRIPT = r"""
const xpiPath = arguments[0];
const done = arguments[arguments.length - 1];
(async () => {
  const out = { xpiPath };
  const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
  const { FileUtils } = ChromeUtils.importESModule("resource://gre/modules/FileUtils.sys.mjs");
  const { AppConstants } = ChromeUtils.importESModule("resource://gre/modules/AppConstants.sys.mjs");
  const { AddonSettings } = ChromeUtils.importESModule("resource://gre/modules/addons/AddonSettings.sys.mjs");
  out.env = {
    version: Services.appinfo.version,
    requireSigning: AppConstants.MOZ_REQUIRE_SIGNING,
    experimentsEnabled: AddonSettings.EXPERIMENTS_ENABLED,
  };
  if (!AddonSettings.EXPERIMENTS_ENABLED) {
    done({ ...out, error:
      "this build refuses experiment APIs (EXPERIMENTS_ENABLED is false), so the " +
      "privileged half cannot load" });
    return;
  }
  const file = new FileUtils.File(xpiPath);
  if (!file.exists()) { done({ ...out, error: `no such file: ${xpiPath}` }); return; }

  const existing = await AddonManager.getAddonByID(arguments[1]);
  if (existing) { await existing.uninstall(); out.replaced = existing.version; }

  const install = await AddonManager.getInstallForFile(file);
  if (!install) { done({ ...out, error: "getInstallForFile returned null" }); return; }
  if (install.error) {
    done({ ...out, error: `Thunderbird rejected the package (AddonManager error ${install.error}); ` +
      "this is usually a malformed XPI" });
    return;
  }
  const result = await new Promise(resolve => {
    install.addListener({
      onInstallEnded(_i, addon) {
        resolve({ ok: true, id: addon.id, version: addon.version,
                  signedState: addon.signedState, appDisabled: addon.appDisabled,
                  userDisabled: addon.userDisabled, isActive: addon.isActive });
      },
      onInstallFailed(i) { resolve({ ok: false, error: `install failed (${i.error})` }); },
      onInstallCancelled() { resolve({ ok: false, error: "install cancelled" }); },
      onDownloadFailed(i) { resolve({ ok: false, error: `read failed (${i.error})` }); },
    });
    setTimeout(() => resolve({ ok: false, error: "install timed out" }), 60000);
    install.install();
  });
  done({ ...out, result });
})().catch(e => done({ fatalError: String((e && e.stack) || e) }));
"""

STATUS_SCRIPT = r"""
const done = arguments[arguments.length - 1];
(async () => {
  const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
  const addon = await AddonManager.getAddonByID(arguments[0]);
  done(addon ? { installed: true, version: addon.version, isActive: addon.isActive,
                 appDisabled: addon.appDisabled, userDisabled: addon.userDisabled }
             : { installed: false });
})().catch(e => done({ fatalError: String((e && e.stack) || e) }));
"""


@dataclass
class InstallOutcome:
    ok: bool
    message: str
    xpi: pathlib.Path
    details: dict | None = None


# ------------------------------------------------------------------- executable


def find_thunderbird() -> pathlib.Path | None:
    """Locate the Thunderbird binary for this platform."""
    override = os.environ.get("TBMCP_THUNDERBIRD")
    if override and pathlib.Path(override).exists():
        return pathlib.Path(override)

    if sys.platform == "win32":
        candidates = [
            pathlib.Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Mozilla Thunderbird"
            / "thunderbird.exe",
            pathlib.Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Mozilla Thunderbird"
            / "thunderbird.exe",
            pathlib.Path(os.environ.get("LOCALAPPDATA", ""))
            / "Mozilla Thunderbird"
            / "thunderbird.exe",
        ]
    elif sys.platform == "darwin":
        candidates = [
            pathlib.Path("/Applications/Thunderbird.app/Contents/MacOS/thunderbird"),
            pathlib.Path.home() / "Applications/Thunderbird.app/Contents/MacOS/thunderbird",
        ]
    else:
        candidates = [
            pathlib.Path("/usr/bin/thunderbird"),
            pathlib.Path("/usr/local/bin/thunderbird"),
            pathlib.Path("/snap/bin/thunderbird"),
        ]
    for path in candidates:
        if path and path.is_file():
            return path
    found = shutil.which("thunderbird")
    return pathlib.Path(found) if found else None


def running_pids() -> list[int]:
    """PIDs of running Thunderbird processes, best effort and dependency-free."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq thunderbird.exe", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            pids = []
            for line in out.splitlines():
                parts = [p.strip('" ') for p in line.split('","')]
                if len(parts) >= 2 and parts[1].isdigit():
                    pids.append(int(parts[1]))
            return pids
        out = subprocess.run(
            ["pgrep", "-f", "thunderbird"], capture_output=True, text=True, check=False
        ).stdout
        return [int(line) for line in out.split() if line.isdigit()]
    except (OSError, ValueError):
        return []


def is_running() -> bool:
    return bool(running_pids())


def _launch(exe: pathlib.Path, extra_args: list[str], profile: ThunderbirdProfile | None) -> None:
    argv = [str(exe), *extra_args]
    if profile is not None:
        argv += ["-profile", str(profile.path)]
    kwargs: dict = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)


def _stop(timeout: float = 60.0) -> bool:
    """Ask Thunderbird to close, then wait. Never force-kills: that loses mail state."""
    if not is_running():
        return True
    if sys.platform == "win32":
        # A WM_CLOSE to the main window is the graceful path; taskkill without /F
        # sends exactly that.
        subprocess.run(
            ["taskkill", "/IM", "thunderbird.exe"],
            capture_output=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        for pid in running_pids():
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running():
            return True
        time.sleep(1.0)
    return not is_running()


# ------------------------------------------------------------------------ install


def install_automatic(
    profile: ThunderbirdProfile | None = None,
    *,
    restart_after: bool = True,
    xpi: pathlib.Path | None = None,
) -> InstallOutcome:
    """Build and install through Marionette, then relaunch Thunderbird normally."""
    exe = find_thunderbird()
    if exe is None:
        raise UnsupportedError(
            "could not find thunderbird.exe. Set TBMCP_THUNDERBIRD to its full path, "
            "or use `tbmcp install-addon --manual`."
        )
    package = xpi or build_xpi(state_dir() / "addon")
    identifier = addon_id()

    was_running = is_running()
    if was_running and not _stop():
        raise TbmcpError(
            "Thunderbird would not close. Close it yourself and run this again, or use "
            "`tbmcp install-addon --manual`.",
            code="WONT_CLOSE",
        )

    log.info("starting Thunderbird with automation enabled")
    _launch(exe, ["-marionette", "-remote-allow-system-access"], profile)
    if not marionette.wait_for_port(timeout=90.0):
        raise TbmcpError(
            "Thunderbird started but never opened the Marionette port. Try "
            "`tbmcp install-addon --manual`.",
            code="NO_MARIONETTE",
        )

    client = marionette.connect(timeout=60.0)
    try:
        client.start_chrome_session()
        report = client.execute(INSTALL_SCRIPT, [str(package), identifier], timeout_ms=120_000)
    finally:
        # Always take Marionette back down: while it is listening, any local process
        # can run privileged code inside Thunderbird.
        client.quit_application()

    if not isinstance(report, dict):
        raise TbmcpError(f"unexpected response from Thunderbird: {report!r}")
    if report.get("error"):
        message = str(report["error"])
        _restart_plain(exe, profile, restart_after)
        return InstallOutcome(False, message, package, report)

    result = report.get("result") or {}
    if not result.get("ok"):
        _restart_plain(exe, profile, restart_after)
        return InstallOutcome(False, str(result.get("error") or "install failed"), package, report)

    _stop()
    _restart_plain(exe, profile, restart_after)
    return InstallOutcome(
        True,
        f"installed {identifier} {result.get('version')} "
        f"(unsigned, active={result.get('isActive')})",
        package,
        report,
    )


def _restart_plain(exe: pathlib.Path, profile: ThunderbirdProfile | None, restart: bool) -> None:
    if not restart:
        return
    _stop(timeout=30.0)
    log.info("restarting Thunderbird normally")
    _launch(exe, [], profile)


def check_installed(timeout: float = 10.0) -> dict | None:
    """Ask a Marionette-enabled Thunderbird whether the add-on is present.

    Only useful during `install-addon`; normal operation reads the same facts from
    the add-on's own `hello` frame.
    """
    if not marionette.wait_for_port(timeout=timeout):
        return None
    client = marionette.connect(timeout=timeout)
    try:
        client.start_chrome_session()
        result = client.execute(STATUS_SCRIPT, [addon_id()], timeout_ms=20_000)
        return result if isinstance(result, dict) else None
    finally:
        client.close()


def manual_instructions(xpi: pathlib.Path | None = None) -> tuple[pathlib.Path, str]:
    """Build the XPI and return it plus click-by-click instructions."""
    package = xpi or build_xpi(state_dir() / "addon")
    text = (
        f"Add-on package built:\n  {package}\n\n"
        "Install it in Thunderbird:\n"
        "  1. Menu (☰) > Add-ons and Themes\n"
        "  2. the gear icon > Install Add-on From File…\n"
        f"  3. choose {package.name}\n"
        "  4. confirm the permission prompt, then restart Thunderbird\n\n"
        "It is unsigned, which Thunderbird allows: release builds ship "
        "MOZ_REQUIRE_SIGNING=false. The bridge connects on its own once the daemon "
        "is running."
    )
    return package, text


def summary() -> dict:
    """What `tbmcp doctor` prints about the add-on side."""
    src = addon_source_dir()
    exe = find_thunderbird()
    return {
        "addonId": addon_id(src),
        "addonVersion": addon_version(src),
        "addonSource": str(src),
        "thunderbirdExe": str(exe) if exe else None,
        "thunderbirdRunning": is_running(),
    }

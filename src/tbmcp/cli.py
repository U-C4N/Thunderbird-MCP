"""Command line entry point.

`tbmcp` with no arguments serves MCP over stdio, because that is how an MCP client
spawns it. Everything else is a subcommand.

Nothing here may write to stdout before the transport takes over — a stray byte
looks like a protocol violation to the client. Logging goes to stderr, always.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Sequence

from .config import ALL_TOOLSETS, Settings, parse_tools, parse_toolsets


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # websockets logs every frame at DEBUG; that is never what we want.
    logging.getLogger("websockets").setLevel(logging.WARNING)


def _settings_from_args(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    toolsets = parse_toolsets(args.toolsets) if getattr(args, "toolsets", None) else None
    named = getattr(args, "tools", None)
    return settings.merged_with(
        toolsets=toolsets,
        extra_tools=parse_tools(named) if named else None,
        read_only=True if getattr(args, "read_only", False) else None,
        yolo=True if getattr(args, "yolo", False) else None,
        unsafe_prefs=True if getattr(args, "unsafe_prefs", False) else None,
        send_mode="send" if getattr(args, "send", False) else None,
        profile=getattr(args, "profile", None),
        default_timeout=getattr(args, "timeout", None),
    )


# ------------------------------------------------------------------- subcommands


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import build_server

    settings = _settings_from_args(args)
    mcp = build_server(settings)
    if args.http:
        mcp.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path=args.path,
        )
    else:
        mcp.run(transport="stdio")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from .daemon import run_daemon

    return asyncio.run(run_daemon(args.profile, idle_timeout=args.idle_timeout, force=args.force))


def cmd_install_addon(args: argparse.Namespace) -> int:
    from . import addon_install
    from .profile import find_profile

    profile = find_profile(args.profile)
    if args.manual:
        _package, text = addon_install.manual_instructions()
        print(text)
        return 0

    if not args.yes:
        print(
            "This will close Thunderbird, install the bridge add-on through "
            "Thunderbird's own automation channel, and start it again.\n"
            "Nothing is sent anywhere and no mail is touched.\n"
            "Use --manual to get the package and install it by hand instead.",
            file=sys.stderr,
        )
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled. `tbmcp install-addon --manual` builds the XPI only.")
            return 1

    outcome = addon_install.install_automatic(profile, restart_after=not args.no_restart)
    print(outcome.message)
    if not outcome.ok:
        _package, text = addon_install.manual_instructions(outcome.xpi)
        print("\nAutomatic install did not work. Do it by hand:\n", file=sys.stderr)
        print(text, file=sys.stderr)
        return 1
    return 0


def _doctor_ok(report: dict) -> bool:
    """Whether `doctor` actually established a working chain — not just ran.

    Both the direct daemon status *and* the `tb_status` tool call (routed through the
    real MCP tool-calling machinery, the same path a client uses) have to say
    `connected`. Checking only one would let the other silently regress unnoticed.

    Exception: when `tb_status` was never registered — the `admin` toolset is not
    selected and `--tools` does not name it — `report["tbStatusCall"]` carries
    `skipped`, not `connected` or `error`. That is a supported configuration, not a
    broken chain, so it must not sink `ok` the way a genuine dispatch failure would.
    Note that this hinges on `cmd_doctor` skipping only when the tool really is
    absent: a skip recorded for a tool that *was* registered would turn this
    exemption into a way to pass without testing anything.
    """
    bridge_state = report.get("bridge")
    tool_call = report.get("tbStatusCall")
    bridge_ok = isinstance(bridge_state, dict) and bool(bridge_state.get("connected"))
    if isinstance(tool_call, dict) and tool_call.get("skipped"):
        return bridge_ok
    tool_ok = isinstance(tool_call, dict) and bool(tool_call.get("connected"))
    return bridge_ok and tool_ok


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import addon_install
    from .bridge import Bridge, set_shared_bridge
    from .ipc import DaemonInfo
    from .profile import ProfileSnapshot, find_profile, list_profiles
    from .server import build_server

    settings = _settings_from_args(args)
    report: dict[str, object] = {"python": sys.version.split()[0], "executable": sys.executable}

    profiles = list_profiles()
    report["profilesFound"] = [
        {"name": p.name, "path": str(p.path), "default": p.is_default} for p in profiles
    ]
    profile = find_profile(settings.profile)
    report["profileSelected"] = str(profile.path) if profile else None
    if profile:
        snapshot = ProfileSnapshot.load(profile)
        report["accountsOnDisk"] = len(snapshot.accounts())
        report["outgoingServersOnDisk"] = len(snapshot.outgoing_servers())
        report["glodaDatabase"] = profile.gloda_db.is_file()
        report["bridgeFile"] = profile.bridge_file.is_file()
        # Written by the add-on at startup. Its absence, when the add-on is installed
        # and active, means the privileged half did not load.
        if profile.addon_status_file.is_file():
            try:
                report["addonStatus"] = json.loads(
                    profile.addon_status_file.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                report["addonStatus"] = {"error": str(exc)}
        else:
            report["addonStatus"] = None

    report["addon"] = addon_install.summary()
    info = DaemonInfo.load()
    report["daemon"] = (
        {"running": True, "pid": info.pid, "port": info.port} if info else {"running": False}
    )

    async def probe() -> None:
        bridge = Bridge(profile_hint=settings.profile, autostart=not args.no_start)
        try:
            # The add-on dials out, so give it a moment: on a cold start the daemon
            # has only just written the pairing file it is polling for.
            report["bridge"] = await bridge.require_thunderbird(wait=args.wait)
        except Exception as exc:
            try:
                report["bridge"] = await bridge.status()
            except Exception:
                report["bridge"] = {"error": str(exc)}

        # `report["bridge"]` above talks to the daemon directly. This instead goes
        # through `MCPServer.call_tool`, the exact dispatch a real client triggers —
        # tool lookup, annotations, the `Context` object — so `doctor` (and the
        # bootstrap `verify` step that shells out to it) proves the whole chain
        # answers, not merely that `Bridge.status()` can format a reply. Reuses the
        # connection already established above; does not spawn a second daemon.
        set_shared_bridge(bridge)
        try:
            mcp = build_server(settings, bridge=bridge)
            # Ask the server what it registered rather than predicting it from the
            # toolset selection. `--tools tb_status` registers the tool without
            # selecting `admin`, so the two questions stopped being the same one —
            # and answering the wrong one here means reporting a healthy chain
            # without ever having exercised it.
            registered = {tool.name for tool in await mcp.list_tools()}
            if "tb_status" not in registered:
                # Dispatching anyway would just raise `ToolError: Unknown tool:
                # tb_status` — indistinguishable, to a naive check, from the tool
                # genuinely failing. Record the real reason instead of making a call
                # we already know cannot succeed.
                report["tbStatusCall"] = {
                    "skipped": (
                        "tb_status is not registered (the admin toolset is not "
                        "selected and --tools does not name it)"
                    )
                }
            else:
                result = await mcp.call_tool("tb_status", {})
                report["tbStatusCall"] = result.structured_content
        except Exception as exc:
            report["tbStatusCall"] = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            set_shared_bridge(None)
            await bridge.close()

    asyncio.run(probe())

    report["toolsets"] = {
        "selected": list(settings.toolsets),
        "available": list(ALL_TOOLSETS),
        "readOnly": settings.read_only,
        "sendMode": settings.send_mode,
    }

    ok = _doctor_ok(report)
    report["ok"] = ok

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0 if ok else 1

    _print_doctor(report)
    return 0 if ok else 1


def _print_doctor(report: dict) -> None:
    def line(label: str, value: object) -> None:
        print(f"  {label:<26} {value}")

    print("thunderbird-mcp doctor\n")
    print("Python")
    line("version", report["python"])
    line("interpreter", report["executable"])

    print("\nThunderbird")
    addon = report.get("addon") or {}
    line("executable", addon.get("thunderbirdExe") or "NOT FOUND")
    line("running", addon.get("thunderbirdRunning"))
    line("add-on version (source)", addon.get("addonVersion"))
    line("profile", report.get("profileSelected") or "NOT FOUND")
    if report.get("accountsOnDisk") is not None:
        line("accounts (from prefs.js)", report["accountsOnDisk"])
        line("outgoing servers", report["outgoingServersOnDisk"])
        line("global index db", report.get("glodaDatabase"))

    status = report.get("addonStatus")
    if status is None:
        line("add-on startup report", "MISSING")
    elif isinstance(status, dict) and status.get("error"):
        line("add-on startup report", f"unreadable: {status['error']}")
    else:
        capabilities = (status or {}).get("capabilities") or {}
        loaded = (capabilities.get("privilegedModules") or {}).get("loaded") or []
        line("add-on startup report", status.get("writtenAt"))
        line("privileged modules", f"{len(loaded)} loaded" if loaded else "none")
        line("bridge methods", status.get("methodCount"))

    print("\nBridge")
    daemon = report.get("daemon") or {}
    line("daemon", f"pid {daemon.get('pid')}" if daemon.get("running") else "not running")
    bridge = report.get("bridge") or {}
    if bridge.get("error"):
        line("status", f"ERROR: {bridge['error']}")
    else:
        line("connected", bridge.get("connected"))
        tb = bridge.get("thunderbird") or {}
        if tb:
            line("add-on version (live)", tb.get("addonVersion"))
            line("privileged half", tb.get("experiment"))
            app = tb.get("app") or {}
            line("app", f"{app.get('name')} {app.get('version')}")

    tool_call = report.get("tbStatusCall") or {}
    if tool_call.get("skipped"):
        line("tb_status tool call", f"not checked: {tool_call['skipped']}")
    elif tool_call.get("error"):
        line("tb_status tool call", f"ERROR: {tool_call['error']}")
    else:
        line("tb_status tool call", "connected" if tool_call.get("connected") else "not connected")

    print("\nTools")
    tools = report.get("toolsets") or {}
    line("toolsets", ",".join(tools.get("selected", [])))
    line("read-only", tools.get("readOnly"))
    line("send mode", tools.get("sendMode"))

    if not bridge.get("connected"):
        print("\nNot connected. In order, check:")
        if not addon.get("thunderbirdRunning"):
            print("  * Thunderbird is not running — start it.")
        elif report.get("addonStatus") is None:
            print("  * The add-on never wrote its startup report, so either it is not")
            print("    installed or its privileged half failed to load.")
            print("    Install or reinstall it:  tbmcp install-addon")
        elif not (bridge.get("thunderbird") or {}):
            print("  * The add-on started but has not dialled in yet. It polls for the")
            print("    pairing file about once a second; try again in a moment.")
        elif not (bridge.get("thunderbird") or {}).get("experiment"):
            print("  * The privileged half did not load; settings tools will fail.")
            print("    Reinstall:  tbmcp install-addon")


def cmd_setup(args: argparse.Namespace) -> int:
    from .clients import run_setup

    return run_setup(
        clients=args.client,
        scope=args.scope,
        dry_run=args.dry_run,
        print_only=args.print_config,
        settings=_settings_from_args(args),
    )


def cmd_detect_clients(args: argparse.Namespace) -> int:
    """A machine-readable list of installed clients — for `bootstrap` to shell out to.

    `bootstrap.py` may not import from this package at all, so it cannot call
    `clients.installed_clients()` directly; it spawns the venv's interpreter and
    reads this command's stdout instead. Plain `print(json.dumps(...))` keeps that
    parse trivial.
    """
    from .clients import installed_clients

    print(json.dumps(installed_clients()))
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    """List the tools that would be registered — useful when writing allowlists."""
    from .server import build_server

    settings = _settings_from_args(args)
    mcp = build_server(settings)

    async def dump() -> list[dict]:
        listed = await mcp.list_tools()
        return [
            {
                "name": tool.name,
                "title": getattr(tool, "title", None),
                "readOnly": bool(
                    tool.annotations and getattr(tool.annotations, "read_only_hint", False)
                ),
                "destructive": bool(
                    tool.annotations and getattr(tool.annotations, "destructive_hint", False)
                ),
            }
            for tool in listed
        ]

    rows = asyncio.run(dump())
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        marker = "r" if row["readOnly"] else ("!" if row["destructive"] else "w")
        print(f"  [{marker}] {row['name']:<34} {row['title'] or ''}")
    named = f" (+{','.join(settings.extra_tools)} by name)" if settings.extra_tools else ""
    print(f"\n{len(rows)} tools from toolsets: {','.join(settings.toolsets)}{named}")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    from .bootstrap import main as bootstrap_main

    forwarded: list[str] = []
    for flag in ("python", "venv", "clients", "toolsets", "tools", "source"):
        value = getattr(args, flag, None)
        if value:
            forwarded += [f"--{flag}", str(value)]
    for flag in ("skip_addon", "dry_run", "json"):
        if getattr(args, flag, False):
            forwarded.append("--" + flag.replace("_", "-"))
    return bootstrap_main(forwarded)


# ------------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tbmcp",
        description="MCP server for Thunderbird. With no subcommand, serves over stdio.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging on stderr")
    subparsers = parser.add_subparsers(dest="command")

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--profile", help="Thunderbird profile name or directory")
        sub.add_argument(
            "--toolsets",
            help=f"comma separated: {','.join(ALL_TOOLSETS)}, all, or +extra (default: lean set)",
        )
        sub.add_argument("--read-only", action="store_true", help="register no mutating tools")
        sub.add_argument(
            "--tools",
            help="comma separated tool names to register on top of --toolsets; a tool "
            "named here is allowed to write even under --read-only",
        )
        sub.add_argument(
            "--yolo", action="store_true", help="skip every confirmation gate (dangerous)"
        )
        sub.add_argument(
            "--unsafe-prefs",
            action="store_true",
            help="allow preference writes outside the reviewed allowlist",
        )
        sub.add_argument(
            "--send",
            action="store_true",
            help="let mail_send actually send instead of drafting by default",
        )
        sub.add_argument("--timeout", type=float, help="per-call timeout in seconds (default 30)")

    serve = subparsers.add_parser("serve", help="run the MCP server (default)")
    add_common(serve)
    serve.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--path", default="/mcp")
    serve.set_defaults(func=cmd_serve)

    daemon = subparsers.add_parser("daemon", help="run the broker that Thunderbird connects to")
    daemon.add_argument("--profile")
    daemon.add_argument(
        "--idle-timeout",
        type=float,
        default=900.0,
        help="exit after this many seconds with no MCP clients (0 disables)",
    )
    daemon.add_argument(
        "--force",
        action="store_true",
        help="start even when another daemon is running (it will then stand down)",
    )
    daemon.set_defaults(func=cmd_daemon)

    install = subparsers.add_parser("install-addon", help="build and install the bridge add-on")
    install.add_argument("--profile")
    install.add_argument("--manual", action="store_true", help="only build it and print the steps")
    install.add_argument("--yes", "-y", action="store_true", help="do not ask before restarting")
    install.add_argument(
        "--no-restart", action="store_true", help="leave Thunderbird closed afterwards"
    )
    install.set_defaults(func=cmd_install_addon)

    doctor = subparsers.add_parser("doctor", help="diagnose the whole chain")
    add_common(doctor)
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--no-start", action="store_true", help="do not start the daemon")
    doctor.add_argument(
        "--wait",
        type=float,
        default=25.0,
        help="seconds to wait for the add-on to connect (default 25)",
    )
    doctor.set_defaults(func=cmd_doctor)

    setup = subparsers.add_parser("setup", help="register this server with an MCP client")
    add_common(setup)
    setup.add_argument(
        "client",
        nargs="*",
        default=["claude-code", "codex"],
        help="claude-code, codex, claude-desktop, cursor, vscode, gemini, zed, all",
    )
    setup.add_argument("--scope", choices=["user", "project"], default="user")
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument(
        "--print-config", action="store_true", help="print the blocks, write nothing"
    )
    setup.set_defaults(func=cmd_setup)

    detect = subparsers.add_parser(
        "detect-clients", help="list installed MCP clients on this machine, as JSON"
    )
    detect.set_defaults(func=cmd_detect_clients)

    tools = subparsers.add_parser("tools", help="list the tools that would be registered")
    add_common(tools)
    tools.add_argument("--json", action="store_true")
    tools.set_defaults(func=cmd_tools)

    boot = subparsers.add_parser("bootstrap", help="install, repair, register, verify")
    boot.add_argument("--python")
    boot.add_argument("--venv")
    boot.add_argument("--clients", help="comma separated; default: auto-detect installed clients")
    boot.add_argument("--toolsets")
    # Needed even though bootstrap only forwards it. argparse accepts any unambiguous
    # prefix, so without this `--tools X` is silently read as `--toolsets X` and travels
    # on as a toolset name — failing later, in a different command, with a message that
    # names something the caller never typed.
    boot.add_argument("--tools")
    boot.add_argument("--source")
    boot.add_argument("--skip-addon", action="store_true")
    boot.add_argument("--dry-run", action="store_true")
    boot.add_argument("--json", action="store_true")
    boot.set_defaults(func=cmd_bootstrap)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    # Bare `tbmcp` is how an MCP client launches us.
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "-v", "--verbose")):
        argv = ["serve", *argv]
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["serve", *argv])

    _configure_logging(getattr(args, "verbose", False) or os.environ.get("TBMCP_DEBUG") == "1")
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logging.getLogger("tbmcp").error("%s", exc)
        if getattr(args, "verbose", False):
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

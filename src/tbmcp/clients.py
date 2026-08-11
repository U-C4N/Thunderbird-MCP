"""Registering this server with the MCP clients people actually use.

Every client keeps stdio servers in a different file, under a different key, with a
different idea of what a command is. The differences that matter here are all failure
modes hit on a real Windows box:

- the command must be an **absolute path to a real executable**. Node-based clients
  spawn it with no shell, so a bare name or a `.cmd` shim dies with `spawn ENOENT`.
  Wrapping it in `cmd /c` is worse: the pipes are not inherited cleanly and a clear
  error turns into a startup timeout.
- `PYTHONUTF8` and `PYTHONUNBUFFERED` go into every per-server `env`. Codex does not
  hand its own environment to stdio servers (it assembles a fixed core set), and
  Claude Code ignores `settings.json` `env` for MCP children — so the only reliable
  channel is the server entry itself.
- Codex ignores tool annotations and `_meta`, so its config file is the only place
  the safety model can be expressed there at all; hence the approval modes below.

Writes are idempotent — read, build the wanted entry, compare, write only on a real
difference — backed up once per file and swapped in with `os.replace`. Where a config
is really JSONC (VS Code and Zed both ship theirs full of comments) we refuse to
reserialise it and print the block for the user to paste instead. Losing someone's
comments to save them one paste is not a trade worth making.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

from .config import ALL_TOOLSETS, DEFAULT_TOOLSETS, Settings

SERVER_NAME = "thunderbird"

#: Order clients are processed and reported in.
CLIENTS: tuple[str, ...] = (
    "claude-code",
    "codex",
    "claude-desktop",
    "cursor",
    "vscode",
    "gemini",
    "zed",
)

#: Written into every client's per-server `env`. UTF-8 because Windows consoles still
#: default to a legacy code page and a subject line will eventually contain an em dash;
#: unbuffered because a half-flushed frame on a stdio pipe reads as a hung server.
CHILD_ENV: dict[str, str] = {"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}

#: Codex asks per tool, from config only. These four are the ones that leave the
#: machine or change configuration, so they always get an explicit prompt.
APPROVE_TOOLS: tuple[str, ...] = ("pref_set", "account_set_server", "mail_send", "mail_delete")

#: Codex's default 10 s startup budget is the single most common cause of a
#: "request timed out" on first use: the daemon may still be waiting for Thunderbird.
CODEX_STARTUP_TIMEOUT = 60
CODEX_TOOL_TIMEOUT = 120


class SetupError(Exception):
    """A config we could read but must not touch. Reported, then skipped."""


@dataclass
class Outcome:
    client: str
    status: str  # added | updated | unchanged | skipped | failed
    target: str = ""
    detail: str = ""
    block: str = ""
    planned: bool = False

    @property
    def label(self) -> str:
        if not self.planned:
            return self.status
        return {"added": "would add", "updated": "would update"}.get(self.status, self.status)


@dataclass
class Ctx:
    scope: str = "user"
    dry_run: bool = False
    print_only: bool = False

    @property
    def preview(self) -> bool:
        return self.dry_run or self.print_only


# ------------------------------------------------------------------- the command


def _probe_exit(argv: list[str]) -> int:
    done = subprocess.run(argv, capture_output=True, timeout=2)
    return done.returncode


def _runnable(path: Path) -> bool:
    """Does this launcher actually start?

    `is_file()` is not enough. Windows Application Control blocks pip's generated
    `.exe` shims, and the failure only shows up in the client as a timeout with no
    stated cause. Two seconds of `--help` here buys a config that works.
    """
    try:
        return _probe_exit([str(path), "--help"]) == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _console_script() -> Path | None:
    """Absolute path to the installed `thunderbird-mcp` launcher, if there is one.

    A `.cmd`/`.bat` shim is deliberately rejected: `shutil.which` will happily find
    one, but the clients that spawn without a shell cannot execute it. Falling back
    to `python -m tbmcp` is slower to type and always works.
    """
    candidates: list[Path] = []
    found = shutil.which("thunderbird-mcp")
    if found:
        candidates.append(Path(found))
    # A venv or `uv tool` install is frequently not on the PATH of the shell that
    # happens to be running setup, so look beside the interpreter too.
    here = Path(sys.executable).resolve().parent
    candidates += [
        here / "thunderbird-mcp.exe",
        here / "thunderbird-mcp",
        here / "Scripts" / "thunderbird-mcp.exe",
        here / "bin" / "thunderbird-mcp",
    ]
    for path in candidates:
        if path.is_file() and path.suffix.lower() not in (".cmd", ".bat", ".ps1"):
            resolved = path.resolve()
            if _runnable(resolved):
                return resolved
    return None


def _serve_args(settings: Settings) -> list[str]:
    """The flags that reproduce this invocation's settings in a spawned server.

    Baked into `args` rather than `env` on purpose: `args` is the one channel every
    client passes through untouched.
    """
    args = ["serve"]
    toolsets = tuple(settings.toolsets)
    if toolsets != DEFAULT_TOOLSETS:
        args += ["--toolsets", "all" if toolsets == ALL_TOOLSETS else ",".join(toolsets)]
    if settings.read_only:
        args.append("--read-only")
    if settings.send_mode == "send":
        args.append("--send")
    if settings.unsafe_prefs:
        args.append("--unsafe-prefs")
    if settings.yolo:
        # Carried through because it was asked for explicitly, and called out in the
        # summary: a registration that quietly skips every gate is worth noticing.
        args.append("--yolo")
    if settings.profile:
        args += ["--profile", settings.profile]
    if abs(settings.default_timeout - 30.0) > 0.01:
        args += ["--timeout", f"{settings.default_timeout:g}"]
    return args


def server_command(settings: Settings) -> tuple[str, list[str]]:
    """`(command, args)` as it must appear in a client config."""
    script = _console_script()
    serve = _serve_args(settings)
    if script is not None:
        return str(script), serve
    return str(Path(sys.executable).resolve()), ["-m", "tbmcp", *serve]


def _entry_for(client: str, command: str, args: Sequence[str]) -> dict[str, Any]:
    """The per-client server entry. Key order is the order it lands in the file."""
    entry: dict[str, Any] = {}
    if client in ("claude-code", "vscode"):
        entry["type"] = "stdio"
    if client == "zed":
        # A hand-written Zed entry without this is silently ignored — it is how Zed
        # tells its own extension-provided servers from yours.
        entry["source"] = "custom"
    entry["command"] = command
    entry["args"] = list(args)
    entry["env"] = dict(CHILD_ENV)
    return entry


# --------------------------------------------------------------------- file layer


def _read(path: Path) -> str | None:
    """Existing text, or `None` when the file is not there.

    `utf-8-sig` on the way in and plain `utf-8` on the way out: an editor may have
    left a BOM, and both `json.loads` and `tomlkit.parse` choke on one, so it must be
    dropped on read and never written back.
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SetupError(f"cannot read {path}: {exc}") from exc


def _has_comments(text: str) -> bool:
    """Whether this is really JSONC. String-aware, so `https://` does not count."""
    in_string = False
    escaped = False
    previous = ""
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif previous == "/" and char in "/*":
            return True
        previous = char
    return False


def _load_json(path: Path) -> tuple[dict[str, Any], bool]:
    """`(object, has_comments)`. Refuses to guess at a file it cannot parse."""
    text = _read(path)
    if text is None or not text.strip():
        return {}, False
    commented = _has_comments(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        if commented:
            return {}, True
        raise SetupError(
            f"{path} is not valid JSON ({exc}). Fix it, then run setup again."
        ) from exc
    if not isinstance(data, dict):
        raise SetupError(f"{path} does not hold a JSON object.")
    return data, commented


def _peek_json(path: Path) -> dict[str, Any]:
    """Read-only, forgiving load — for files we inspect but must never write."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _backup(path: Path) -> Path | None:
    """One timestamped copy before the first write of a run."""
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.tbmcp-{time.strftime('%Y%m%d%H%M%S')}.bak")
    try:
        shutil.copy2(path, target)
    except OSError as exc:
        raise SetupError(f"could not back up {path}: {exc}") from exc
    return target


def _write(path: Path, text: str) -> Path | None:
    """Back up, write a sibling temp file, then `os.replace` it into place.

    Same directory for the temp file so the replace is atomic on the same volume —
    a crash mid-write can then never leave a truncated config behind.
    """
    backup = _backup(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tbmcp-tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        raise SetupError(f"could not write {path}: {exc}") from exc
    return backup


def _dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _run(argv: list[str]) -> tuple[int, str]:
    """Run a client CLI. Never raises: a missing CLI is a normal outcome here."""
    # argv is ours and argv[0] came from which(), so there is no shell in the path.
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        return 127, str(exc)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


# ------------------------------------------------------------------ generic JSON


def _apply_json(
    client: str,
    path: Path,
    root_key: str,
    entry: dict[str, Any],
    ctx: Ctx,
    *,
    note: str = "",
) -> Outcome:
    """Merge one server entry into a plain JSON config, preserving everything else."""
    data, commented = _load_json(path)
    section = data.get(root_key)
    if not isinstance(section, dict):
        section = {}
    current = section.get(SERVER_NAME)
    block = _dump_json({root_key: {SERVER_NAME: entry}})

    if current == entry:
        return Outcome(client, "unchanged", str(path), note)
    status = "updated" if isinstance(current, dict) else "added"
    if ctx.preview:
        return Outcome(client, status, str(path), note, block, planned=True)
    if commented:
        return Outcome(
            client,
            "skipped",
            str(path),
            "has // comments; reserialising would delete them. Paste this instead:",
            block,
        )

    data[root_key] = {**section, SERVER_NAME: entry}
    backup = _write(path, _dump_json(data))
    return Outcome(client, status, str(path), _with_backup(note, backup))


def _with_backup(note: str, backup: Path | None) -> str:
    if backup is None:
        return note
    text = f"backup: {backup.name}"
    return f"{note} ({text})" if note else text


# ---------------------------------------------------------------- Claude Code


def _claude_code_path() -> Path:
    return Path.home() / ".claude.json"


def _claude_code(entry: dict[str, Any], ctx: Ctx) -> Outcome:
    """`claude mcp add-json` for user scope; `.mcp.json` for project scope.

    `~/.claude.json` is never hand-edited: it carries OAuth state and the per-project
    trust decisions, and a well-meaning reserialisation there is how people lose their
    logins. We read it only to stay idempotent and to verify afterwards.
    """
    if ctx.scope == "project":
        return _apply_json(
            "claude-code",
            Path.cwd() / ".mcp.json",
            "mcpServers",
            entry,
            ctx,
            note="checked in with the project",
        )

    path = _claude_code_path()
    current = (_peek_json(path).get("mcpServers") or {}).get(SERVER_NAME)
    payload = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    command = f"claude mcp add-json {SERVER_NAME} '{payload}' --scope user"

    if current == entry:
        return Outcome("claude-code", "unchanged", str(path))
    status = "updated" if isinstance(current, dict) else "added"
    if ctx.preview:
        return Outcome("claude-code", status, str(path), "via the CLI:", command, planned=True)

    claude = shutil.which("claude")
    if claude is None:
        return Outcome(
            "claude-code",
            "skipped",
            str(path),
            "the `claude` CLI is not on PATH, and this file is not safe to edit. Run:",
            command,
        )

    backup = _backup(path)
    code, output = _run([claude, "mcp", "add-json", SERVER_NAME, payload, "--scope", "user"])
    if code != 0 and isinstance(current, dict):
        # add-json refuses a name that already exists, so replace it in two steps.
        _run([claude, "mcp", "remove", SERVER_NAME, "--scope", "user"])
        code, output = _run([claude, "mcp", "add-json", SERVER_NAME, payload, "--scope", "user"])
    # Trust the file, not the exit code: on Windows `claude` is often a batch shim,
    # and cmd.exe can mangle the quoting of a JSON argument on the way through.
    written = (_peek_json(path).get("mcpServers") or {}).get(SERVER_NAME)
    if written != entry:
        return Outcome(
            "claude-code",
            "failed",
            str(path),
            f"`claude mcp add-json` did not take ({output or f'exit {code}'}). Run this yourself:",
            command,
        )
    return Outcome("claude-code", status, str(path), _with_backup("via claude CLI", backup))


# ---------------------------------------------------------------------- Codex


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _plain(value: Any) -> Any:
    """tomlkit item -> ordinary Python, for comparison."""
    unwrap = getattr(value, "unwrap", None)
    return unwrap() if callable(unwrap) else value


def _toml_value(value: Any) -> Any:
    """Convert to a TOML item, spelling Windows paths as literal strings.

    `"C:\\Users\\..."` is not a valid TOML basic string — `\\U` starts a unicode
    escape — so anything with a backslash goes in single quotes.
    """
    if isinstance(value, str):
        literal = "\\" in value and "'" not in value and "\n" not in value
        return tomlkit.string(value, literal=literal)
    if isinstance(value, (list, tuple)):
        array = tomlkit.array()
        for item in value:
            array.append(_toml_value(item))
        return array
    if isinstance(value, dict):
        inline = tomlkit.inline_table()
        for key, item in value.items():
            inline[key] = _toml_value(item)
        return inline
    return value


def _codex(command: str, args: Sequence[str], ctx: Ctx) -> Outcome:
    """`[mcp_servers.thunderbird]` in `config.toml`, edited through tomlkit.

    tomlkit keeps the rest of the document byte-for-byte — comments, key order,
    blank lines — which matters because `config.toml` is a file people write by hand.
    Only our own table is rebuilt.
    """
    if ctx.scope == "project":
        return Outcome(
            "codex",
            "skipped",
            str(_codex_home() / "config.toml"),
            "Codex has no per-project config; re-run with --scope user.",
        )

    path = _codex_home() / "config.toml"
    text = _read(path) or ""
    try:
        doc = tomlkit.parse(text)
    except Exception as exc:  # tomlkit raises a family of ParseError subclasses
        raise SetupError(
            f"{path} is not valid TOML ({exc}). Fix it, then run setup again."
        ) from exc

    servers = doc.get("mcp_servers")
    existing = _plain(servers.get(SERVER_NAME)) if isinstance(servers, dict) else None
    existing = existing if isinstance(existing, dict) else None

    tools = dict(existing.get("tools") or {}) if existing else {}
    tools.update({name: {"approval_mode": "approve"} for name in APPROVE_TOOLS})
    wanted: dict[str, Any] = {
        "command": command,
        "args": list(args),
        "env": dict(CHILD_ENV),
        "startup_timeout_sec": CODEX_STARTUP_TIMEOUT,
        "tool_timeout_sec": CODEX_TOOL_TIMEOUT,
    }
    if existing is None or "enabled" not in existing:
        wanted["enabled"] = True
    # Only these keys pass Codex's deny_unknown_fields check; anything else it sees
    # here makes it reject the whole file, not just this server.
    wanted["default_tools_approval_mode"] = "writes"
    wanted["tools"] = tools

    block = _codex_block(wanted)
    if existing is not None and all(existing.get(k) == v for k, v in wanted.items()):
        return Outcome("codex", "unchanged", str(path))
    status = "updated" if existing is not None else "added"
    if ctx.preview:
        return Outcome("codex", status, str(path), "", block, planned=True)

    # Rebuilt rather than patched key by key: a table's sub-tables are rendered after
    # its scalars, so adding a scalar to a table that already has
    # `[mcp_servers.thunderbird.tools.x]` would place the new key under that header —
    # silently reassigning it to the wrong table.
    table = tomlkit.table()
    for key, value in wanted.items():
        if key == "tools":
            continue
        table[key] = _toml_value(value)
    for key, value in (existing or {}).items():
        # Keep anything the user set that we do not manage, e.g. enabled = false.
        if key not in wanted:
            table[key] = _toml_value(value)
    table["tools"] = _codex_tools(tools)

    if not isinstance(doc.get("mcp_servers"), dict):
        doc["mcp_servers"] = tomlkit.table(True)
    doc["mcp_servers"][SERVER_NAME] = table  # type: ignore[index]
    backup = _write(path, tomlkit.dumps(doc))
    return Outcome("codex", status, str(path), _with_backup("", backup), "")


def _codex_tools(tools: dict[str, Any]) -> Any:
    """`[mcp_servers.thunderbird.tools.<name>]` sub-tables, in a stable order."""
    parent = tomlkit.table(True)
    for name in sorted(tools):
        child = tomlkit.table()
        for key, value in (tools[name] or {}).items():
            child[key] = _toml_value(value)
        parent[name] = child
    return parent


def _codex_block(wanted: dict[str, Any]) -> str:
    """The same table, rendered on its own, for `--print-config`."""
    doc = tomlkit.document()
    doc["mcp_servers"] = tomlkit.table(True)
    table = tomlkit.table()
    for key, value in wanted.items():
        if key != "tools":
            table[key] = _toml_value(value)
    table["tools"] = _codex_tools(wanted.get("tools") or {})
    doc["mcp_servers"][SERVER_NAME] = table  # type: ignore[index]
    return tomlkit.dumps(doc).strip()


# ----------------------------------------------------------------- other clients


def _app_data(*parts: str) -> Path:
    """Per-user config directory for a GUI app, per platform."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base.joinpath(*parts)


def _claude_desktop_path() -> Path:
    return _app_data("Claude", "claude_desktop_config.json")


def _claude_desktop(entry: dict[str, Any], ctx: Ctx) -> Outcome:
    path = _claude_desktop_path()
    note = "" if ctx.scope == "user" else "no project scope here; wrote the user config"
    return _apply_json("claude-desktop", path, "mcpServers", entry, ctx, note=note)


def _cursor_path() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


def _cursor(entry: dict[str, Any], ctx: Ctx) -> Outcome:
    path = Path.cwd() / ".cursor" / "mcp.json" if ctx.scope == "project" else _cursor_path()
    return _apply_json("cursor", path, "mcpServers", entry, ctx)


def _gemini_path() -> Path:
    return Path.home() / ".gemini" / "settings.json"


def _gemini(entry: dict[str, Any], ctx: Ctx) -> Outcome:
    path = Path.cwd() / ".gemini" / "settings.json" if ctx.scope == "project" else _gemini_path()
    return _apply_json("gemini", path, "mcpServers", entry, ctx)


def _zed_path() -> Path:
    return (
        _app_data("Zed", "settings.json")
        if sys.platform == "win32"
        else Path.home() / ".config" / "zed" / "settings.json"
    )


def _zed(entry: dict[str, Any], ctx: Ctx) -> Outcome:
    """Zed's `context_servers`, which usually ends in a paste.

    Zed ships `settings.json` as a commented template and has no CLI for this, so the
    comment guard nearly always fires. That is the right outcome, not a shortcoming.
    """
    path = _zed_path()
    note = "" if ctx.scope == "user" else "context_servers is user-wide; wrote the user config"
    return _apply_json("zed", path, "context_servers", entry, ctx, note=note)


def _vscode_user_path() -> Path:
    return _app_data("Code", "User", "mcp.json")


def _vscode(entry: dict[str, Any], ctx: Ctx) -> Outcome:
    """`.vscode/mcp.json` for a project; `code --add-mcp` for the user profile."""
    if ctx.scope == "project":
        return _apply_json("vscode", Path.cwd() / ".vscode" / "mcp.json", "servers", entry, ctx)

    path = _vscode_user_path()
    payload = json.dumps({"name": SERVER_NAME, **entry}, ensure_ascii=False, separators=(",", ":"))
    current = (_peek_json(path).get("servers") or {}).get(SERVER_NAME)
    if current == entry:
        return Outcome("vscode", "unchanged", str(path))
    status = "updated" if isinstance(current, dict) else "added"
    block = _dump_json({"servers": {SERVER_NAME: entry}})
    if ctx.preview:
        return Outcome("vscode", status, str(path), "", block, planned=True)

    code = shutil.which("code")
    if code is not None:
        backup = _backup(path)
        exit_code, output = _run([code, "--add-mcp", payload])
        if (_peek_json(path).get("servers") or {}).get(SERVER_NAME) == entry:
            return Outcome("vscode", status, str(path), _with_backup("via code CLI", backup))
        detail = f"`code --add-mcp` did not take ({output or f'exit {exit_code}'})"
    else:
        detail = "the `code` CLI is not on PATH"

    # Fall back to the file, but only while it is plain JSON — VS Code writes JSONC.
    outcome = _apply_json("vscode", path, "servers", entry, ctx)
    if outcome.status == "skipped":
        outcome.detail = f"{detail}, and the file {outcome.detail}"
    return outcome


# --------------------------------------------------------------------- reporting


@dataclass
class _Plan:
    command: str
    args: list[str]
    outcomes: list[Outcome] = field(default_factory=list)


def _print_report(plan: _Plan, ctx: Ctx) -> None:
    print("thunderbird-mcp setup\n")
    print(f"  command  {plan.command}")
    print(f"  args     {' '.join(plan.args)}")
    print(f"  env      {' '.join(f'{k}={v}' for k, v in CHILD_ENV.items())}")
    print(f"  scope    {ctx.scope}")
    if ctx.preview:
        print("\n  --- nothing was written ---")
    print()

    width = max((len(o.client) for o in plan.outcomes), default=12)
    for outcome in plan.outcomes:
        print(f"  {outcome.client:<{width}}  {outcome.label:<12}  {outcome.target}")
        if outcome.detail:
            print(f"  {'':<{width}}  {'':<12}  {outcome.detail}")
        if outcome.block and (outcome.planned or outcome.status in ("skipped", "failed")):
            for line in outcome.block.splitlines():
                print(f"        {line}" if line.strip() else "")
            print()

    touched = {o.client for o in plan.outcomes if o.status in ("added", "updated")}
    if not ctx.preview and touched:
        print("\nVerify:")
        if "claude-code" in touched:
            print(f"  claude mcp get {SERVER_NAME}")
        if "codex" in touched:
            print(f"  codex mcp get {SERVER_NAME} --json")
        print("  tbmcp doctor")
    if any(o.status == "skipped" for o in plan.outcomes):
        print("\nSkipped configs were left untouched. Paste the block above into each.")
    if "--yolo" in plan.args:
        print(
            "\nThis registration includes --yolo, so every confirmation gate is off "
            "for these clients. Re-run setup without it to put them back."
        )
    if plan.args[:2] == ["-m", "tbmcp"]:
        print(
            "\nNo `thunderbird-mcp` executable was found, so this registers "
            f"`{Path(plan.command).name} -m tbmcp` instead. `uv tool install "
            "thunderbird-mcp` (or pipx) gives you a launcher that does not depend on "
            "this interpreter staying put."
        )


# ------------------------------------------------------------------ entry point


def installed_clients() -> list[str]:
    """Which of `CLIENTS` actually look present on this machine, in `CLIENTS` order.

    "Present" is judged from the same locations the writers themselves use, on the
    theory that whatever the writer would touch is also good evidence the client
    exists — no new paths are invented here. `claude-code`, `codex`, and `vscode`
    each have a CLI that their own writer treats as the *primary* way to register
    (`_claude_code`, `_codex`, and `_vscode` all try `claude`/`codex`/`code` before
    ever touching a config file), so detection trusts that same CLI being on PATH:
    all three write their config lazily, on first use, and a fresh install with the
    CLI present but never yet used would otherwise read as absent. Every other
    client here is a GUI app whose writer only ever touches a config path, with no
    CLI fallback to mirror — for those, that file or its parent directory existing
    is the best signal available without something OS-specific like querying an app
    registry.
    """
    checks: dict[str, bool] = {
        "claude-code": shutil.which("claude") is not None or _claude_code_path().exists(),
        "codex": shutil.which("codex") is not None or _codex_home().exists(),
        "claude-desktop": _claude_desktop_path().exists() or _claude_desktop_path().parent.exists(),
        "cursor": _cursor_path().exists() or _cursor_path().parent.exists(),
        "vscode": (
            shutil.which("code") is not None
            or _vscode_user_path().exists()
            or _vscode_user_path().parent.exists()
        ),
        "gemini": _gemini_path().exists() or _gemini_path().parent.exists(),
        "zed": _zed_path().exists() or _zed_path().parent.exists(),
    }
    return [client for client in CLIENTS if checks[client]]


def resolve_clients(requested: Sequence[str] | None) -> list[str]:
    """Expand `all`, drop duplicates, and reject anything we do not know."""
    names = [str(name).strip().lower() for name in (requested or []) if str(name).strip()]
    if not names:
        names = ["claude-code", "codex"]
    unknown = [name for name in names if name not in CLIENTS and name != "all"]
    if unknown:
        raise SystemExit(
            f"unknown client {', '.join(repr(u) for u in unknown)}; "
            f"choose from: {', '.join(CLIENTS)}, all"
        )
    if "all" in names:
        return list(CLIENTS)
    return [name for name in CLIENTS if name in names]


def run_setup(
    clients: Sequence[str] | None = None,
    scope: str = "user",
    dry_run: bool = False,
    print_only: bool = False,
    settings: Settings | None = None,
) -> int:
    """Register this server with each named client. Returns a process exit code."""
    settings = settings or Settings()
    ctx = Ctx(scope=scope, dry_run=dry_run, print_only=print_only)
    command, args = server_command(settings)
    plan = _Plan(command=command, args=args)

    for client in resolve_clients(clients):
        entry = _entry_for(client, command, args)
        try:
            if client == "claude-code":
                outcome = _claude_code(entry, ctx)
            elif client == "codex":
                outcome = _codex(command, args, ctx)
            elif client == "claude-desktop":
                outcome = _claude_desktop(entry, ctx)
            elif client == "cursor":
                outcome = _cursor(entry, ctx)
            elif client == "vscode":
                outcome = _vscode(entry, ctx)
            elif client == "gemini":
                outcome = _gemini(entry, ctx)
            else:
                outcome = _zed(entry, ctx)
        except SetupError as exc:
            outcome = Outcome(client, "failed", detail=str(exc))
        plan.outcomes.append(outcome)

    _print_report(plan, ctx)
    if ctx.preview:
        return 0
    return 1 if any(o.status == "failed" for o in plan.outcomes) else 0

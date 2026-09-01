"""Assembling the MCP server: instructions, toolset registration, transports."""

from __future__ import annotations

import importlib
import inspect
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import safety
from .bridge import Bridge, set_shared_bridge
from .config import ALL_TOOLSETS, Settings

log = logging.getLogger("tbmcp.server")

F = TypeVar("F", bound=Callable[..., Any])

# Claude Code truncates server instructions at 2 KB, and with tool search on this
# text is the primary discovery signal. Lead with what matters.
INSTRUCTIONS = """\
Drives the user's local Thunderbird: mail, folders, contacts, calendar, message
filters, account/server configuration, and preferences.

Reach for these tools whenever the user talks about *their* mail, calendar, or
Thunderbird setup — this is live data on this machine, not a copy.

Notes that will save you a round trip:
- Search with `mail_search`. It accepts a `full_text` query (Thunderbird's global
  index) plus structured filters, and paginates; prefer it over listing folders.
- IDs are opaque and only valid while Thunderbird stays open. Re-query rather than
  reusing an ID from an earlier session.
- `mail_send` creates a reviewable draft by default. Pass `mode="send"` to actually
  send, and expect a confirmation prompt.
- Anything that deletes, sends, or changes settings needs `confirm=true` (or an
  approval prompt). If a call comes back asking for confirmation, ask the user
  first, then repeat the call with `confirm=true`.
- Settings live in two places: `pref_*` for global preferences and `account_*` for
  per-account server/identity configuration. Read before writing; both report the
  old value so you can undo.
- If a tool reports that Thunderbird is not connected, tell the user to start
  Thunderbird; if it says the add-on is missing, tell them to run
  `tbmcp install-addon`.
"""


class Registrar:
    """Registers tools with consistent annotations, and drops writes in read-only mode."""

    def __init__(self, mcp: MCPServer, settings: Settings) -> None:
        self.mcp = mcp
        self.settings = settings
        self.registered: list[str] = []
        self.skipped: list[str] = []
        self.named = frozenset(settings.extra_tools)
        """Tools the operator asked for by name, whatever the toolset selection says."""
        self.exempted: list[str] = []
        """Named tools that read-only would otherwise have dropped."""
        self.found: set[str] = set()
        """Which named tools actually exist, so a typo can be reported rather than ignored."""
        self.toolset_selected = True
        """False while a module is imported only to reach one named tool inside it.
        Its other tools must not be registered as a side effect of that import."""

    def _add(
        self,
        fn: F,
        *,
        title: str,
        annotations: ToolAnnotations,
        meta: dict[str, Any] | None,
        mutates: bool,
    ) -> F:
        name = fn.__name__
        named = name in self.named
        if named:
            self.found.add(name)
        elif not self.toolset_selected:
            return fn
        registered: F = fn
        if mutates and self.settings.read_only:
            if not named:
                self.skipped.append(name)
                return fn
            # Named by hand, so read-only yields for this one tool. The decision has
            # to be carried into the call: `guard_write` runs inside the body and
            # cannot see that this tool was singled out.
            registered = safety.exempt_write(fn)  # type: ignore[assignment]
            self.exempted.append(name)
        self.mcp.add_tool(
            registered,
            title=title,
            # Normalise the docstring ourselves rather than letting the interpreter
            # decide: CPython 3.13 strips common leading whitespace from `__doc__` at
            # compile time and 3.11 does not, so on 3.11 every tool description
            # reached the client indented — wasted tokens, and it reads as sloppy.
            description=inspect.cleandoc(fn.__doc__ or "") or None,
            annotations=annotations,
            meta=meta or None,
        )
        self.registered.append(name)
        return fn

    def read_tool(
        self,
        *,
        title: str,
        annotations: ToolAnnotations | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Callable[[F], F]:
        """A tool that cannot change anything."""

        def decorate(fn: F) -> F:
            return self._add(
                fn,
                title=title,
                annotations=annotations or safety.READ_ONLY,
                meta=meta,
                mutates=False,
            )

        return decorate

    def write_tool(
        self,
        *,
        title: str,
        annotations: ToolAnnotations | None = None,
        meta: dict[str, Any] | None = None,
        interactive: bool = True,
    ) -> Callable[[F], F]:
        """A tool that changes Thunderbird. Host-level prompting is on by default."""

        def decorate(fn: F) -> F:
            merged = dict(meta or {})
            if interactive:
                merged.update(safety.NEEDS_INTERACTION)
            return self._add(
                fn,
                title=title,
                annotations=annotations or safety.MUTATING,
                meta=merged,
                mutates=True,
            )

        return decorate


def build_server(settings: Settings, *, bridge: Bridge | None = None) -> MCPServer:
    """Create the server and register the selected toolsets, in a stable order."""
    safety.set_settings(settings)
    if bridge is not None:
        set_shared_bridge(bridge)
    else:
        set_shared_bridge(
            Bridge(
                profile_hint=settings.profile,
                autostart=settings.autostart_daemon,
                default_timeout=settings.default_timeout,
            )
        )

    mcp = MCPServer(
        name="thunderbird",
        title="Thunderbird",
        version=_version(),
        instructions=INSTRUCTIONS,
        warn_on_duplicate_tools=True,
    )
    registrar = Registrar(mcp, settings)

    # A named tool may live in a toolset that was not selected, and nothing outside a
    # toolset module knows which tools it defines. So once `--tools` is in play every
    # toolset is imported and the Registrar drops whatever was not asked for.
    for name in ALL_TOOLSETS:
        selected = name in settings.toolsets
        if not selected and not registrar.named:
            continue
        module = importlib.import_module(f".tools.{name}", package=__package__)
        register = getattr(module, "register", None)
        if register is None:
            log.warning("toolset %s has no register()", name)
            continue
        registrar.toolset_selected = selected
        register(registrar)
    registrar.toolset_selected = True

    unknown = sorted(registrar.named - registrar.found)
    if unknown:
        # Loud, because the failure is otherwise invisible: the server starts, the
        # tool the operator asked for is simply absent, and the model reports that
        # Thunderbird cannot do the thing.
        raise SystemExit(
            f"unknown tool(s) in --tools: {', '.join(unknown)}. "
            "Run `tbmcp tools --toolsets all` for the available names."
        )

    log.info(
        "registered %d tools from %s%s%s",
        len(registrar.registered),
        ",".join(settings.toolsets),
        f" (+{','.join(sorted(registrar.named))} by name)" if registrar.named else "",
        f" ({len(registrar.skipped)} write tools omitted: read-only)" if registrar.skipped else "",
    )
    if registrar.exempted:
        # Worth a WARNING and not an INFO: --read-only no longer means what it says,
        # and the operator should be able to find out why from the log alone.
        log.warning(
            "read-only lifted for %s (named explicitly with --tools)",
            ", ".join(sorted(registrar.exempted)),
        )
    return mcp


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("thunderbird-mcp")
    except Exception:
        return "1.2.0"

"""Assembling the MCP server: instructions, toolset registration, transports."""

from __future__ import annotations

import functools
import importlib
import inspect
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import safety
from .bridge import Bridge, set_shared_bridge
from .config import ALL_TOOLSETS, Settings
from .errors import TbmcpError

log = logging.getLogger("tbmcp.server")

F = TypeVar("F", bound=Callable[..., Any])


def _speaking_errors(fn: F) -> F:
    """Re-raise our own failures as the SDK's `ToolError`, which keeps the message.

    An unrecognised exception is wrapped by mcp 2.1 as
    `UnexpectedToolError("Error executing tool <name>")`, with the text dropped —
    reasonably, since a stray exception's message is not written for a model to
    read. Ours are: every `TbmcpError` says what the caller should do differently,
    and losing that turns "pass at least one filter, `full_text` is usually what
    you want" back into "something went wrong".

    Raising `ToolError` is how a server says the message is deliberate. It is
    understood by 2.0 and 2.1 alike, so this does not pin the dependency floor.
    """

    @functools.wraps(fn)
    async def speaking(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except TbmcpError as exc:
            raise ToolError(str(exc)) from exc

    return speaking  # type: ignore[return-value]


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

    def _add(
        self,
        fn: F,
        *,
        title: str,
        annotations: ToolAnnotations,
        meta: dict[str, Any] | None,
        mutates: bool,
    ) -> F:
        if mutates and self.settings.read_only:
            self.skipped.append(fn.__name__)
            return fn
        self.mcp.add_tool(
            _speaking_errors(fn),
            title=title,
            # Normalise the docstring ourselves rather than letting the interpreter
            # decide: CPython 3.13 strips common leading whitespace from `__doc__` at
            # compile time and 3.11 does not, so on 3.11 every tool description
            # reached the client indented — wasted tokens, and it reads as sloppy.
            description=inspect.cleandoc(fn.__doc__ or "") or None,
            annotations=annotations,
            meta=meta or None,
        )
        self.registered.append(fn.__name__)
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

    for name in ALL_TOOLSETS:
        if name not in settings.toolsets:
            continue
        module = importlib.import_module(f".tools.{name}", package=__package__)
        register = getattr(module, "register", None)
        if register is None:
            log.warning("toolset %s has no register()", name)
            continue
        register(registrar)

    log.info(
        "registered %d tools from %s%s",
        len(registrar.registered),
        ",".join(settings.toolsets),
        f" ({len(registrar.skipped)} write tools omitted: read-only)" if registrar.skipped else "",
    )
    return mcp


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("thunderbird-mcp")
    except Exception:
        return "1.2.0"

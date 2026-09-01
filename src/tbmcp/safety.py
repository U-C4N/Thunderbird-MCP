"""Consent, annotations, and the shape every mutating tool follows.

Four independent layers, because no single one is present on every client:

1. Honest `ToolAnnotations` — advisory, but hosts use them to decide when to ask.
2. `_meta["anthropic/requiresUserInteraction"]` — Claude Code prompts on every call,
   even under `bypassPermissions`. Codex ignores it.
3. An explicit `confirm: bool = False` tool parameter — works on every client,
   including ones with no back-channel at all.
4. Elicitation through `Annotated[Consent, Resolve(...)]` — a real prompt where the
   client supports it, and it never appears in the tool's input schema, so the model
   cannot fabricate the approval.

The confirmation resolver degrades in that order: `--yolo` short-circuits, an
explicit `confirm=True` satisfies it, an elicitation-capable client is asked, and
anything else gets a `BlockedError` that tells the model exactly what to pass.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from contextvars import ContextVar
from typing import Annotated, Any

from mcp.server.mcpserver import Context, Elicit, Resolve
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .config import Settings
from .errors import BlockedError

# Set once by server.build_server(); resolvers are module-level functions, so they
# need a way to see the active policy.
_settings = Settings()


def set_settings(settings: Settings) -> None:
    global _settings
    _settings = settings


def current_settings() -> Settings:
    return _settings


class Consent(BaseModel):
    """The answer to a confirmation prompt."""

    approve: bool = Field(
        default=False,
        description="Approve this action. Answer no to abort without any change.",
    )
    note: str | None = Field(default=None, description="Optional note recorded in the server log.")


#: Marks "there was no way to ask" as distinct from "the user said no". The
#: difference matters: the first is worth telling the model how to fix, the second
#: must never be presented as something to work around.
NO_CHANNEL = "__tbmcp_no_consent_channel__"


def _elicitation_available(ctx: Context | None) -> bool:
    if ctx is None:
        return False
    try:
        capabilities = ctx.client_capabilities
    except Exception:
        return False
    return bool(capabilities and getattr(capabilities, "elicitation", None))


def consent_for(action: str) -> Callable[..., Any]:
    """Build a resolver that gates a tool behind approval.

    `action` is a short imperative phrase used in the prompt, e.g.
    `"send this message"`. The generated resolver reads the tool's own `confirm`
    argument by name, so every gated tool must declare `confirm: bool = False`.

    The resolver never raises. Resolvers run *before* the tool body, so raising here
    would also block a `dry_run_only` preview — which is exactly when a caller most
    wants to look before committing. Instead it reports "no channel", and `require()`
    turns that into an error at the point the tool is about to actually do something.
    """

    async def resolver(confirm: bool = False, ctx: Context | None = None):  # type: ignore[no-untyped-def]
        if _settings.yolo or confirm:
            return Consent(approve=True)
        if _elicitation_available(ctx):
            return Elicit(f"Allow thunderbird-mcp to {action}?", Consent)
        return Consent(approve=False, note=NO_CHANNEL)

    return resolver


def require(consent: Consent | None, action: str) -> None:
    """Stop unless the resolved consent actually approved the action.

    Two failure modes, deliberately worded differently: a missing consent channel is
    something the caller can fix by passing `confirm=true`, whereas a user who said
    no must not be handed a workaround.
    """
    if consent is not None and consent.approve:
        return
    if consent is None or consent.note == NO_CHANNEL:
        raise BlockedError(
            f"Refusing to {action} without confirmation.",
            needs="confirm=true",
            code="NEEDS_CONFIRMATION",
            hint="Ask the user first, then re-issue the same call with confirm=true.",
        )
    raise BlockedError(
        f"The user declined to {action}.", code="DECLINED", needs="a new instruction from the user"
    )


def Gate(action: str) -> Any:
    """`consent: Gate("delete these messages")` in a tool signature."""
    return Annotated[Consent, Resolve(consent_for(action))]


#: Set for the duration of a tool the operator named in `--tools`. Read-only is a
#: blanket policy and naming one tool by hand is the deliberate exception to it, but
#: the exception has to reach `guard_write`, which runs deep inside a tool body and
#: has no idea which tool it is guarding.
#:
#: A ContextVar rather than a field on `_settings`: tool calls share one process and
#: interleave at every await, so toggling global state around one call would leak its
#: exemption into whatever else happened to be in flight.
_exempt_from_read_only: ContextVar[bool] = ContextVar("tbmcp_write_exempt", default=False)


def exempt_write(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool named in `--tools` so `guard_write` lets its writes through."""

    @functools.wraps(fn)
    async def permitted(*args: Any, **kwargs: Any) -> Any:
        token = _exempt_from_read_only.set(True)
        try:
            return await fn(*args, **kwargs)
        finally:
            _exempt_from_read_only.reset(token)

    return permitted


def guard_write(what: str) -> None:
    """Refuse a mutating operation when the server was started read-only.

    The exception is a tool the operator named in `--tools`, which is how you allow
    one write — saving a draft, say — without lifting read-only for everything else.
    """
    if _settings.read_only and not _exempt_from_read_only.get():
        raise BlockedError(
            f"This server is running read-only, so it will not {what}.",
            code="READ_ONLY",
            needs="restart without --read-only, or --tools <name> to allow just this one",
        )


# --------------------------------------------------------------------- annotations

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
"""Reads nothing but Thunderbird's own state; safe to call speculatively."""

MUTATING = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
"""Changes state, but nothing is lost if it happens twice."""

IDEMPOTENT_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
"""Setting a value: repeating the call lands in the same place."""

DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)
"""Deletes or permanently rewrites something the user may not be able to recover."""

OUTBOUND = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)
"""Leaves the machine — sending mail cannot be undone."""

#: Force a host prompt where the host honours it.
NEEDS_INTERACTION: dict[str, Any] = {"anthropic/requiresUserInteraction": True}


def large_output(chars: int = 400_000) -> dict[str, Any]:
    """Raise a host's per-result cap for tools that legitimately return a lot."""
    return {"anthropic/maxResultSizeChars": min(chars, 500_000)}

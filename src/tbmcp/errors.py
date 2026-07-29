"""Error taxonomy shared by the bridge, the daemon and the tool layer.

The `kind` values mirror `docs/PROTOCOL.md`. They exist so the tool layer can
decide *how* a failure should reach the model:

- `usage`, `blocked`, `unsupported`, `thunderbird` -> raise a plain exception, which
  the SDK turns into `CallToolResult(is_error=True)`. The model sees the message and
  can correct itself.
- `internal` and transport failures -> also surfaced to the model, because a hidden
  JSON-RPC error just looks like a hang from the user's side.
"""

from __future__ import annotations

from typing import Any, Literal

ErrorKind = Literal["usage", "blocked", "unsupported", "thunderbird", "internal", "transport"]


class TbmcpError(Exception):
    """Base class for everything this package raises deliberately."""

    kind: ErrorKind = "internal"

    def __init__(self, message: str, *, code: str | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra

    def __str__(self) -> str:  # keep tool output terse and actionable
        parts = [self.message]
        if self.code:
            parts.append(f"[{self.code}]")
        hint = self.extra.get("hint")
        if hint:
            parts.append(f"— {hint}")
        return " ".join(parts)


class UsageError(TbmcpError):
    """The call was malformed. A more careful caller could have avoided it."""

    kind: ErrorKind = "usage"


class BlockedError(TbmcpError):
    """A safety rule refused the operation.

    `needs` names what would unblock it, so the model can ask for it explicitly
    instead of retrying blind.
    """

    kind: ErrorKind = "blocked"

    def __init__(self, message: str, *, needs: str | list[str] | None = None, **extra: Any) -> None:
        super().__init__(message, **extra)
        self.needs = [needs] if isinstance(needs, str) else (needs or [])

    def __str__(self) -> str:
        base = super().__str__()
        if self.needs:
            base += f" (requires: {', '.join(self.needs)})"
        return base


class UnsupportedError(TbmcpError):
    """This Thunderbird build cannot do it — not a bug in the caller."""

    kind: ErrorKind = "unsupported"


class ThunderbirdError(TbmcpError):
    """Thunderbird itself failed the request (XPCOM or WebExtension API error)."""

    kind: ErrorKind = "thunderbird"


class TransportError(TbmcpError):
    """The bridge could not carry the request."""

    kind: ErrorKind = "transport"


class NotConnectedError(TransportError):
    """No add-on session is attached to the daemon."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or "Thunderbird is not connected. Start Thunderbird, and make sure the "
            "thunderbird-mcp add-on is installed (run `tbmcp install-addon`) — "
            "`tbmcp doctor` explains what is missing.",
            code="NOT_CONNECTED",
        )


class TimeoutError_(TransportError):
    """The add-on did not answer within the deadline."""

    def __init__(self, method: str, seconds: float) -> None:
        super().__init__(
            f"Thunderbird did not answer {method!r} within {seconds:g}s. "
            "It may be busy syncing a large IMAP folder; retry with a narrower query.",
            code="TIMEOUT",
        )


def from_wire(method: str, payload: dict[str, Any]) -> TbmcpError:
    """Rebuild a typed exception from a protocol `error` object.

    The `code` matters as much as the `kind`: callers branch on specific transport
    codes to decide whether waiting and retrying is worthwhile, and a "not connected"
    that arrives as a bare `TransportError` silently loses that behaviour.
    """
    kind = payload.get("kind") or "internal"
    message = payload.get("message") or f"{method} failed"
    code = payload.get("code")

    if code == "NOT_CONNECTED":
        return NotConnectedError(message)
    if code == "TIMEOUT":
        return TransportError(message, code="TIMEOUT")

    cls: type[TbmcpError] = {
        "usage": UsageError,
        "blocked": BlockedError,
        "unsupported": UnsupportedError,
        "thunderbird": ThunderbirdError,
        "transport": TransportError,
    }.get(kind, TbmcpError)
    if cls is BlockedError:
        return BlockedError(message, needs=payload.get("needs"), code=code)
    return cls(message, code=code)

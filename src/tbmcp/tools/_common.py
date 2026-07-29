"""Helpers every toolset uses. Import from here rather than reaching for the bridge.

House style for tool functions:

- `async def`, typed parameters, a docstring that becomes the tool description.
  Keep the first sentence short — hosts truncate descriptions.
- Return a `dict` (or a pydantic model). Never a bare scalar: the SDK wraps scalars
  as `{"result": ...}`, which reads badly in transcripts.
- Never `print()`. Log through `logging` — stdout belongs to the protocol.
- Validate inputs and raise `UsageError` with a message that says what to send
  instead; the model sees it and gets one cheap chance to fix itself.
- Mutating tools take `confirm: bool = False` plus `consent: Gate("…")`, call
  `guard_write(...)` first, and report what changed so the caller can undo it.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from collections.abc import Iterable, Sequence
from typing import Any

from ..bridge import shared_bridge
from ..errors import UsageError

log = logging.getLogger("tbmcp.tools")

MAX_TEXT = 200_000
"""Ceiling for any single text field we hand back, so one huge mail cannot blow a
client's output budget. Truncation is always announced in the payload."""


async def call(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
) -> Any:
    """Invoke a bridge method. The single door to Thunderbird."""
    return await shared_bridge().call(method, params or {}, timeout=timeout)


async def status() -> dict[str, Any]:
    return await shared_bridge().status()


# ------------------------------------------------------------------------ inputs


def require_ids(value: Sequence[int] | int | None, *, field: str = "message_ids") -> list[int]:
    """Accept one id or a list, and reject the empty case loudly."""
    if value is None:
        raise UsageError(f"{field} is required.")
    ids = [value] if isinstance(value, int) else list(value)
    if not ids:
        raise UsageError(f"{field} was empty — pass at least one id.")
    bad = [i for i in ids if not isinstance(i, int) or i < 0]
    if bad:
        raise UsageError(
            f"{field} must be the integer ids returned by mail_search or mail_list; got {bad!r}."
        )
    return ids


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def coerce_date(value: str | None, *, field: str) -> str | None:
    """Normalise a date/datetime string to ISO-8601 for the add-on.

    Accepts `YYYY-MM-DD` (interpreted as local midnight) or anything
    `datetime.fromisoformat` understands.
    """
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    try:
        if _DATE_ONLY.match(text):
            parsed = _dt.datetime.fromisoformat(text)
        else:
            parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UsageError(
            f"{field} must be an ISO-8601 date or datetime (e.g. 2026-07-01 or "
            f"2026-07-01T09:30:00); got {value!r}."
        ) from exc
    return parsed.isoformat()


def clamp(value: int | None, *, default: int, minimum: int, maximum: int, field: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int):
        raise UsageError(f"{field} must be an integer.")
    if value < minimum or value > maximum:
        raise UsageError(f"{field} must be between {minimum} and {maximum}; got {value}.")
    return value


def one_of(value: str | None, allowed: Iterable[str], *, field: str, default: str) -> str:
    if value is None:
        return default
    options = list(allowed)
    if value not in options:
        raise UsageError(f"{field} must be one of {', '.join(options)}; got {value!r}.")
    return value


# ----------------------------------------------------------------------- outputs


def trim(text: str | None, limit: int = MAX_TEXT) -> tuple[str | None, bool]:
    """Cut over-long text and say so, instead of silently losing the tail."""
    if text is None:
        return None, False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def message_summary(message: dict[str, Any]) -> dict[str, Any]:
    """The compact message shape used by every list/search result.

    Deliberately small: a search returning 50 of these should not dominate the
    context. `mail_get` is one call away when the model needs the body.
    """
    return {
        "id": message.get("id"),
        "subject": message.get("subject"),
        "author": message.get("author"),
        "recipients": message.get("recipients") or [],
        "date": message.get("date"),
        "folderId": (message.get("folder") or {}).get("id")
        if isinstance(message.get("folder"), dict)
        else message.get("folderId"),
        "read": message.get("read"),
        "flagged": message.get("flagged"),
        "junk": message.get("junk"),
        "tags": message.get("tags") or [],
        "hasAttachment": message.get("hasAttachment"),
        "size": message.get("size"),
    }


def page(
    items: list[dict[str, Any]],
    *,
    total: int | None = None,
    cursor: str | None = None,
    truncated: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """Uniform envelope for anything list-shaped."""
    payload: dict[str, Any] = {"items": items, "count": len(items)}
    if total is not None:
        payload["totalAvailable"] = total
    if cursor:
        payload["nextCursor"] = cursor
        payload["hint"] = "Pass cursor=nextCursor to continue."
    if truncated:
        payload["truncated"] = True
    payload.update(extra)
    return payload


def changed(what: str, before: Any, after: Any, **extra: Any) -> dict[str, Any]:
    """Uniform envelope for a write, including what it replaced.

    Reporting `before` is what makes an undo possible without a transaction log.
    """
    return {"changed": True, "target": what, "previous": before, "current": after, **extra}


def dry_run(what: str, plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "changed": False,
        "dryRun": True,
        "target": what,
        "plan": plan,
        "hint": "Re-issue with dry_run=false to apply.",
    }

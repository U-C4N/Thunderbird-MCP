"""What happened to each add-on connection before it was welcomed — and what to say.

The daemon's WebSocket handler only runs once the HTTP upgrade has succeeded, so a
connection that opens, sits there and closes is invisible to it: nothing is logged,
nothing is counted, and every reporter downstream can only say "Thunderbird is not
connected". That is exactly the shape of the outage this module was written for —
thunderbird.exe dialling the add-on port twice every ~25 s for half an hour, each
connection dying ~10 s later without ever upgrading.

So the daemon records one `HandshakeAttempt` per TCP connection, from the moment the
socket opens to whatever ended it, and `describe_handshake` turns that record into
the paragraph `tb_status`, `tb_diagnostics` and `doctor` all put in front of the
user. Pure and self-contained: it holds no sockets, only what became of them.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary

log = logging.getLogger("tbmcp.handshake")

FAILED_BEFORE_HELLO = frozenset(
    {
        "no-upgrade",
        "no-hello-timeout",
        "closed-before-hello",
        "bad-hello",
        "bad-token",
        "protocol-mismatch",
        "non-loopback",
    }
)
"""Outcomes that mean the add-on reached us and still never got welcomed. These are
the ones worth counting: they say the pairing file was read and Thunderbird is
running, so the fault is between those two facts and a working session."""

_TERMINAL_AFTER_WELCOME = frozenset({"disconnected", "superseded"})

#: Extra sentence per outcome, where the outcome narrows the cause down to one thing.
_CAUSES = {
    "no-upgrade": "The TCP connection opened but the WebSocket upgrade never completed.",
    "bad-token": (
        "It keeps presenting a stale token: a stray older `tbmcp daemon` may still be "
        "running, or Thunderbird is using a different profile than the one the daemon "
        "writes its pairing file into (pass `--profile`)."
    ),
    "protocol-mismatch": (
        "The installed add-on speaks a different protocol version — `tbmcp install-addon`."
    ),
}


@dataclass
class HandshakeAttempt:
    """One TCP connection to the add-on port, and what became of it."""

    at: float
    peer_port: int | None
    outcome: str = "accepted"
    close_code: int | None = None
    detail: str | None = None
    user_agent: str | None = None
    origin: str | None = None
    addon_version: str | None = None
    resolved_at: float | None = None


def _peer_port(key: Any) -> int | None:
    """The port the peer dialled from, if the connection will say.

    Two connections a second from the same client are otherwise indistinguishable
    in a log; the ephemeral port is what lets a reader line an entry up against
    `netstat` output.
    """
    address = getattr(key, "remote_address", None)
    if isinstance(address, tuple) and len(address) >= 2:
        try:
            return int(address[1])
        except (TypeError, ValueError):
            return None
    return None


class HandshakeLog:
    """A short history of add-on connection attempts, keyed by the connection.

    Keys are held weakly on purpose: a daemon that has been up for days must not
    keep a dead `ServerConnection` — and its buffers — alive just because it once
    recorded what happened to it. The records themselves live in the ring buffer.
    """

    def __init__(self, maxlen: int = 50, *, clock=time.time, window: float = 60.0) -> None:
        self._attempts: deque[HandshakeAttempt] = deque(maxlen=maxlen)
        self._by_key: WeakKeyDictionary[Any, HandshakeAttempt] = WeakKeyDictionary()
        self._clock = clock
        self._window = window
        # Counted separately from the ring buffer, which caps at `maxlen`: an add-on
        # reconnecting every 25 s fills any buffer long before anyone runs `doctor`.
        self._total = 0

    def opened(self, key: Any) -> HandshakeAttempt:
        """Start (or return) the record for one connection. Idempotent per key."""
        attempt = self._by_key.get(key)
        if attempt is not None:
            return attempt
        attempt = HandshakeAttempt(at=self._clock(), peer_port=_peer_port(key))
        self._by_key[key] = attempt
        self._attempts.append(attempt)
        self._total += 1
        return attempt

    def note_request(self, key: Any, *, path, origin, user_agent) -> None:
        """Record the HTTP upgrade request, which is the last thing before the hello."""
        attempt = self.opened(key)
        attempt.origin = origin
        attempt.user_agent = user_agent
        if path and attempt.detail is None:
            # Keeping the path separates "nothing was ever sent" from "it asked for
            # our endpoint and then died"; a real outcome's detail replaces it.
            attempt.detail = str(path)

    def resolve(
        self,
        key: Any,
        outcome: str,
        *,
        close_code: int | None = None,
        detail: str | None = None,
        addon_version: str | None = None,
    ) -> None:
        """Record how a connection ended, unless it has already been decided.

        The first diagnosis wins. `connection_lost` fires for every connection,
        including one the handler has already rejected by name, so without this the
        specific outcome would be overwritten by the generic hook behind it. A
        welcomed session is the one case that legitimately moves on — to
        `disconnected`, or to `superseded` when we replace it ourselves.
        """
        attempt = self.opened(key)
        current = attempt.outcome
        if current != "accepted" and not (
            current == "welcomed" and outcome in _TERMINAL_AFTER_WELCOME
        ):
            return
        attempt.outcome = outcome
        attempt.resolved_at = self._clock()
        if close_code is not None:
            attempt.close_code = close_code
        if detail is not None:
            attempt.detail = detail
        if addon_version is not None:
            attempt.addon_version = addon_version
        self._report(attempt)

    def _report(self, attempt: HandshakeAttempt) -> None:
        parts: list[str] = []
        if attempt.peer_port is not None:
            parts.append(f"peer port {attempt.peer_port}")
        if attempt.user_agent:
            parts.append(attempt.user_agent)
        if attempt.addon_version:
            parts.append(f"add-on {attempt.addon_version}")
        if attempt.resolved_at is not None:
            parts.append(f"after {attempt.resolved_at - attempt.at:.1f}s")
        if attempt.detail:
            parts.append(attempt.detail)
        where = ", ".join(parts)
        if attempt.outcome in FAILED_BEFORE_HELLO:
            log.warning("add-on handshake failed: %s (%s)", attempt.outcome, where)
        else:
            log.info("add-on handshake %s (%s)", attempt.outcome, where)

    def summary(self) -> dict[str, Any]:
        """Everything a reporter needs, JSON-safe, oldest attempt first."""
        cutoff = self._clock() - self._window
        recent = [attempt for attempt in self._attempts if attempt.at >= cutoff]
        outcomes: dict[str, int] = {}
        for attempt in recent:
            outcomes[attempt.outcome] = outcomes.get(attempt.outcome, 0) + 1
        last = self._attempts[-1] if self._attempts else None
        return {
            "attempts": self._total,
            "lastAttemptAt": last.at if last else None,
            "lastOutcome": last.outcome if last else None,
            "lastCloseCode": last.close_code if last else None,
            "lastUserAgent": last.user_agent if last else None,
            "recentFailures": sum(1 for a in recent if a.outcome in FAILED_BEFORE_HELLO),
            "recentOutcomes": outcomes,
            "windowSeconds": self._window,
            "recent": [_compact(attempt) for attempt in list(self._attempts)[-10:]],
        }


def _compact(attempt: HandshakeAttempt) -> dict[str, Any]:
    """One attempt with its unknowns dropped — these end up in `doctor --json`."""
    row: dict[str, Any] = {"at": round(attempt.at, 3), "outcome": attempt.outcome}
    optional = {
        "peerPort": attempt.peer_port,
        "closeCode": attempt.close_code,
        "detail": attempt.detail,
        "userAgent": attempt.user_agent,
        "addonVersion": attempt.addon_version,
    }
    row.update({name: value for name, value in optional.items() if value is not None})
    if attempt.resolved_at is not None:
        row["seconds"] = round(attempt.resolved_at - attempt.at, 3)
    return row


def _last_failure(summary: dict[str, Any]) -> tuple[str | None, float | None]:
    """The most recent attempt that failed before being welcomed.

    Not simply `lastOutcome`: an add-on that flaps gets welcomed between failures,
    and naming the connection that worked would send the reader looking in the
    wrong place.
    """
    for row in reversed(summary.get("recent") or []):
        if isinstance(row, dict) and row.get("outcome") in FAILED_BEFORE_HELLO:
            return str(row.get("outcome")), row.get("at")
    return summary.get("lastOutcome"), summary.get("lastAttemptAt")


def describe_handshake(summary: dict[str, Any] | None) -> str | None:
    """One paragraph explaining repeated handshake failures, or `None` if there are
    none — so a caller can write `describe_handshake(...) or <the usual hint>`."""
    if not summary:
        return None
    failures = int(summary.get("recentFailures") or 0)
    if failures <= 0:
        return None

    window = float(summary.get("windowSeconds") or 60.0)
    outcome, at = _last_failure(summary)
    when = ""
    if isinstance(at, (int, float)):
        age = time.time() - at
        if age >= 0:
            when = f", {age:.0f} s ago"

    text = (
        f"Thunderbird's add-on reached the daemon {failures} "
        f"{'time' if failures == 1 else 'times'} in the last {window:g} s but never "
        f"completed the handshake (last: {outcome}{when}). Thunderbird is running and "
        "can read the pairing file, so the add-on is failing before it is welcomed. "
        "Restart Thunderbird; if it recurs, open its Error Console (Ctrl+Shift+J), "
        "filter on `[tbmcp]`, and reinstall the add-on to match this package: "
        "`tbmcp install-addon`."
    )
    cause = _CAUSES.get(str(outcome))
    return f"{text} {cause}" if cause else text

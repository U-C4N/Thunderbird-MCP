"""The error taxonomy has to survive the wire.

A "not connected" that came back as a bare `TransportError` silently disabled the
wait-and-retry path in `Bridge.call`, so the first tool call after a cold start failed
instead of waiting a second for the add-on to attach. The symptom was indistinguishable
from a broken install.
"""

from __future__ import annotations

from tbmcp.daemon import error_payload
from tbmcp.errors import (
    BlockedError,
    NotConnectedError,
    ThunderbirdError,
    TransportError,
    UnsupportedError,
    UsageError,
    from_wire,
)


def test_not_connected_survives_the_round_trip() -> None:
    original = NotConnectedError()
    wire = {"kind": "transport", "code": original.code, "message": str(original)}
    rebuilt = from_wire("messages.query", wire)
    assert isinstance(rebuilt, NotConnectedError), "the retry path keys off this type"
    assert rebuilt.code == "NOT_CONNECTED"


def test_kinds_map_to_types() -> None:
    cases = {
        "usage": UsageError,
        "blocked": BlockedError,
        "unsupported": UnsupportedError,
        "thunderbird": ThunderbirdError,
        "transport": TransportError,
    }
    for kind, expected in cases.items():
        rebuilt = from_wire("x.prefs.set", {"kind": kind, "message": "nope"})
        assert isinstance(rebuilt, expected), kind


def test_blocked_keeps_what_would_unblock_it() -> None:
    rebuilt = from_wire(
        "mail_delete",
        {"kind": "blocked", "message": "refusing", "needs": ["confirm=true"]},
    )
    assert isinstance(rebuilt, BlockedError)
    assert rebuilt.needs == ["confirm=true"]
    assert "confirm=true" in str(rebuilt)


def test_an_unknown_kind_still_produces_an_error() -> None:
    rebuilt = from_wire("whatever", {"kind": "martian", "message": "?"})
    assert isinstance(rebuilt, Exception)


def test_a_message_is_always_present() -> None:
    assert "messages.query" in str(from_wire("messages.query", {}))


def test_usage_errors_read_as_instructions() -> None:
    error = UsageError("subject is required", hint="pass subject")
    assert "subject is required" in str(error)
    assert "pass subject" in str(error)


# --------------------------------------------------------- relaying, without echoes

#: What the add-on puts on the wire for a refused delete.
WIRE = {
    "kind": "blocked",
    "code": "NEEDS_CONSENT",
    "message": "refusing to delete 40 messages",
    "needs": ["confirm=true"],
}


def test_relaying_an_error_leaves_it_unchanged() -> None:
    """The daemon used to serialise `str(exc)`, which already renders the code and the
    `needs` list into the text for humans. Every hop therefore appended them again, and
    a caller saw "... [NOT_CONNECTED] [NOT_CONNECTED]". Relaying has to be idempotent.
    """
    once = error_payload(from_wire("mail_delete", WIRE))
    assert once == WIRE
    twice = error_payload(from_wire("mail_delete", once))
    assert twice == WIRE


def test_a_twice_relayed_error_still_reads_once() -> None:
    rebuilt = from_wire("mail_delete", error_payload(from_wire("mail_delete", WIRE)))
    text = str(rebuilt)
    assert text.count("requires:") == 1
    assert text.count("NEEDS_CONSENT") == 1


def test_an_untyped_failure_keeps_its_text() -> None:
    """Anything the daemon did not raise deliberately is still worth relaying: a hidden
    JSON-RPC error looks like a hang from the user's side."""
    assert error_payload(RuntimeError("x")) == {"kind": "internal", "code": None, "message": "x"}

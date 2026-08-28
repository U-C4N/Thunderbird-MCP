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


def test_relaying_an_error_does_not_render_it_into_its_own_message() -> None:
    """The daemon re-serialises errors the add-on already sent structured, and the
    bridge rebuilds them again on the far side. `__str__` folds `code` and `needs`
    into the text for a human reader, so relaying that instead of `.message` made
    each hop bake in another copy — which is how a blocked call arrived reading
    "… (requires: x) [CODE] (requires: x)"."""
    wire = {
        "kind": "blocked",
        "code": "NEEDS_CONSENT",
        "message": "refusing to delete 40 messages",
        "needs": ["confirm=true"],
    }
    once = error_payload(from_wire("mail_delete", wire))
    assert once == wire, "one hop must be lossless and add nothing"

    # Two hops is the real path: add-on -> daemon -> serve.
    twice = error_payload(from_wire("mail_delete", once))
    assert twice == wire, "relaying must be idempotent, not cumulative"

    rebuilt = from_wire("mail_delete", twice)
    assert rebuilt.message == "refusing to delete 40 messages"
    assert rebuilt.needs == ["confirm=true"]
    # The rendering still happens once, at the point a human reads it.
    assert str(rebuilt).count("requires:") == 1
    assert str(rebuilt).count("NEEDS_CONSENT") == 1


def test_relaying_a_plain_exception_still_has_a_message() -> None:
    payload = error_payload(RuntimeError("something went sideways"))
    assert payload["message"] == "something went sideways"
    assert payload["kind"] == "internal"
    assert "needs" not in payload

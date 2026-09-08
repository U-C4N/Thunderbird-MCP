"""Why the add-on never got welcomed, recorded where every reporter can read it.

The morning this exists for: thunderbird.exe opened two connections to the add-on
port every ~25 s for half an hour, each dying ~10 s later without ever finishing the
HTTP upgrade. `tb_status` said "ask the user to start Thunderbird" and `doctor` said
"try again in a moment", because nothing anywhere recorded that the add-on had been
*trying*. This module is that record, so both reporters can say what is happening.
"""

from __future__ import annotations

import logging
import time

import pytest

from tbmcp.handshake import FAILED_BEFORE_HELLO, HandshakeLog, describe_handshake


class Clock:
    """An injected clock: the window arithmetic must not depend on wall time."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Conn:
    """Stands in for a `ServerConnection`: the log keys on identity and reads the
    peer port off the object, in the shape websockets exposes it."""

    def __init__(self, port: int | None = None) -> None:
        self.remote_address = ("127.0.0.1", port) if port is not None else None


# ------------------------------------------------------------------ HandshakeLog


def test_one_attempt_per_connection_however_often_it_is_opened() -> None:
    log = HandshakeLog(clock=Clock(1000.0))
    conn = Conn(51234)

    first = log.opened(conn)

    assert log.opened(conn) is first, "a second event on one connection is one attempt"
    assert first.peer_port == 51234
    assert log.summary()["attempts"] == 1


def test_the_outcome_an_attempt_lands_on_first_is_the_one_that_sticks() -> None:
    """`connection_lost` fires for a rejected connection too, so a failure that has
    already been diagnosed must not be relabelled by the generic hook behind it."""
    log = HandshakeLog(clock=Clock(1000.0))
    conn = Conn(1)
    log.opened(conn)

    log.resolve(conn, "bad-token", close_code=4001)
    log.resolve(conn, "no-upgrade")
    log.resolve(conn, "disconnected", close_code=1000)

    summary = log.summary()
    assert summary["lastOutcome"] == "bad-token"
    assert summary["lastCloseCode"] == 4001
    assert summary["recentFailures"] == 1


def test_a_welcomed_session_only_moves_on_to_disconnected_or_superseded() -> None:
    log = HandshakeLog(clock=Clock(1000.0))
    conn = Conn(1)
    log.opened(conn)
    log.resolve(conn, "welcomed", addon_version="1.3.0")

    log.resolve(conn, "no-upgrade")
    assert log.summary()["lastOutcome"] == "welcomed", "a working session is not a failure"

    log.resolve(conn, "disconnected", close_code=1000)
    summary = log.summary()
    assert summary["lastOutcome"] == "disconnected"
    assert summary["lastCloseCode"] == 1000
    assert summary["recentFailures"] == 0


def test_superseded_is_final_so_the_handler_behind_it_cannot_relabel_it() -> None:
    """The replaced session's own `finally` runs after it is closed with 1012; it
    reporting "disconnected" would hide the fact that we replaced it."""
    log = HandshakeLog(clock=Clock(1000.0))
    conn = Conn(1)
    log.opened(conn)
    log.resolve(conn, "welcomed")

    log.resolve(conn, "superseded", close_code=1012)
    log.resolve(conn, "disconnected", close_code=1012)

    assert log.summary()["lastOutcome"] == "superseded"


def test_every_outcome_that_fails_before_the_hello_counts_as_a_failure() -> None:
    log = HandshakeLog(clock=Clock(1000.0))
    for index, outcome in enumerate(sorted(FAILED_BEFORE_HELLO)):
        conn = Conn(index)
        log.opened(conn)
        log.resolve(conn, outcome)

    assert log.summary()["recentFailures"] == len(FAILED_BEFORE_HELLO)


def test_a_failure_falls_out_of_the_window_once_it_is_old_enough() -> None:
    clock = Clock(1000.0)
    log = HandshakeLog(clock=clock, window=60.0)
    old = Conn(1)
    log.opened(old)
    log.resolve(old, "no-hello-timeout", close_code=4002)

    clock.advance(61.0)
    fresh = Conn(2)
    log.opened(fresh)
    log.resolve(fresh, "no-upgrade")

    summary = log.summary()
    assert summary["attempts"] == 2
    assert summary["recentFailures"] == 1, "the 61s-old failure is not recent"
    assert summary["recentOutcomes"] == {"no-upgrade": 1}
    assert summary["windowSeconds"] == 60.0


def test_the_ring_buffer_caps_what_is_kept_but_not_what_is_counted() -> None:
    """A reconnect every 25 s fills any buffer in minutes; "attempts" has to keep
    counting or the doctor line understates a half-hour outage."""
    log = HandshakeLog(maxlen=5, clock=Clock(1000.0))
    for index in range(12):
        conn = Conn(index)
        log.opened(conn)
        log.resolve(conn, "no-upgrade")

    summary = log.summary()
    assert summary["attempts"] == 12
    assert len(summary["recent"]) == 5
    assert summary["recent"][-1]["peerPort"] == 11


def test_the_upgrade_request_names_who_dialled_in() -> None:
    log = HandshakeLog(clock=Clock(1000.0))
    conn = Conn(51234)
    log.opened(conn)

    log.note_request(conn, path="/tbmcp", origin=None, user_agent="Thunderbird/155.0")
    log.resolve(conn, "no-hello-timeout", close_code=4002)

    summary = log.summary()
    assert summary["lastUserAgent"] == "Thunderbird/155.0"
    # The path is the one thing that separates "the TCP connection opened and
    # nothing was ever sent" from "it asked for our endpoint and then died".
    assert summary["recent"][-1]["detail"] == "/tbmcp"


def test_a_failure_is_logged_with_the_facts_a_reader_needs(caplog) -> None:
    clock = Clock(1000.0)
    log = HandshakeLog(clock=clock)
    conn = Conn(51234)
    log.opened(conn)
    log.note_request(conn, path="/tbmcp", origin=None, user_agent="Thunderbird/155.0")
    clock.advance(10.0)

    with caplog.at_level(logging.DEBUG, logger="tbmcp.handshake"):
        log.resolve(conn, "no-hello-timeout", close_code=4002)

    assert [record.levelname for record in caplog.records] == ["WARNING"]
    assert "no-hello-timeout" in caplog.text
    assert "51234" in caplog.text
    assert "Thunderbird/155.0" in caplog.text
    assert "10.0s" in caplog.text


def test_a_welcome_is_news_but_not_a_warning(caplog) -> None:
    log = HandshakeLog(clock=Clock(1000.0))
    conn = Conn(1)
    log.opened(conn)

    with caplog.at_level(logging.DEBUG, logger="tbmcp.handshake"):
        log.resolve(conn, "welcomed", addon_version="1.3.0")

    assert [record.levelname for record in caplog.records] == ["INFO"]
    assert "1.3.0" in caplog.text


# ------------------------------------------------------------- describe_handshake


def _summary(**overrides: object) -> dict:
    """A summary as the daemon would have serialised it, 12 s after the last try."""
    base = {
        "attempts": 3,
        "lastAttemptAt": time.time() - 12,
        "lastOutcome": "no-hello-timeout",
        "lastCloseCode": 4002,
        "lastUserAgent": "Thunderbird/155.0",
        "recentFailures": 3,
        "recentOutcomes": {"no-hello-timeout": 3},
        "windowSeconds": 60.0,
        "recent": [],
    }
    base.update(overrides)
    return base


def test_nothing_to_diagnose_reads_as_no_diagnosis() -> None:
    assert describe_handshake(None) is None
    assert describe_handshake(_summary(recentFailures=0, recentOutcomes={})) is None


def test_the_diagnosis_names_the_symptom_and_every_remedy() -> None:
    text = describe_handshake(_summary())

    assert text is not None
    assert "3 times in the last 60 s" in text
    assert "no-hello-timeout" in text
    assert "12 s ago" in text
    assert "Restart Thunderbird" in text
    assert "Error Console" in text
    assert "[tbmcp]" in text
    assert "tbmcp install-addon" in text


def test_one_failure_is_reported_as_one_time() -> None:
    text = describe_handshake(_summary(recentFailures=1, recentOutcomes={"bad-hello": 1}))

    assert text is not None and "1 time in the last 60 s" in text


@pytest.mark.parametrize(
    ("outcome", "sentence"),
    [
        ("no-upgrade", "the WebSocket upgrade never completed"),
        ("bad-token", "stale token"),
        ("protocol-mismatch", "different protocol version"),
    ],
)
def test_each_outcome_that_has_its_own_cause_says_so(outcome: str, sentence: str) -> None:
    text = describe_handshake(_summary(lastOutcome=outcome, recentOutcomes={outcome: 3}))

    assert text is not None and sentence in text


def test_the_diagnosis_quotes_the_last_failure_not_a_later_success() -> None:
    """A flapping add-on gets welcomed between failures. Naming the connection that
    worked would send the reader looking in the wrong place."""
    now = time.time()
    text = describe_handshake(
        _summary(
            lastOutcome="disconnected",
            lastAttemptAt=now - 1,
            recent=[
                {"at": now - 30, "outcome": "bad-token"},
                {"at": now - 1, "outcome": "disconnected"},
            ],
        )
    )

    assert text is not None
    assert "bad-token" in text
    assert "stale token" in text

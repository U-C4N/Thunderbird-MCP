from collections import deque
from types import SimpleNamespace

import pytest

from tbmcp.daemon import Daemon
from tbmcp.errors import UsageError


def state(count, maxlen=500):
    return SimpleNamespace(
        event_epoch="epoch",
        event_seq=count,
        events=deque([{"seq": n, "data": {}} for n in range(1, count + 1)], maxlen=maxlen),
    )


def test_events_pages_and_independent_consumers():
    daemon = state(120)
    first = Daemon._events_page(daemon, {"limit": 50})
    second = Daemon._events_page(daemon, {"limit": 50, "cursor": first["nextCursor"]})
    third = Daemon._events_page(daemon, {"limit": 50, "cursor": second["nextCursor"]})
    assert [len(page["events"]) for page in [first, second, third]] == [50, 50, 20]
    assert [e["seq"] for page in [first, second, third] for e in page["events"]] == list(
        range(1, 121)
    )
    assert Daemon._events_page(daemon, {"limit": 50}) == first
    assert first["latestSeq"] == 50 and first["newestSeq"] == 120
    assert not third["hasMore"]


def test_events_loss_restart_and_empty():
    daemon = state(600)
    page = Daemon._events_page(daemon, {})
    assert page["gap"] == {"from": 1, "to": 100, "count": 100}
    assert page["resyncRequired"]
    empty = Daemon._events_page(daemon, {"since": 600})
    assert empty["latestSeq"] == 600 and not empty["events"]
    with pytest.raises(UsageError, match="earlier daemon"):
        Daemon._events_page(daemon, {"cursor": "old:50"})
    with pytest.raises(UsageError):
        Daemon._events_page(daemon, {"cursor": "epoch:50", "since": 1})

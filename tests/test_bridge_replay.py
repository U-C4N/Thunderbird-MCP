"""A response lost after delivery must not duplicate a write or consume a page."""

import pytest

from tbmcp import ipc
from tbmcp.bridge import Bridge
from tbmcp.errors import TransportError


class Writer:
    def is_closing(self):
        return False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "method,params,retries",
    [
        ("compose.send", {"mode": "send"}, 0),
        ("messages.move", {"messageIds": [1]}, 0),
        ("messages.query", {"cursor": "consume-once"}, 0),
        ("messages.read", {"messageId": 1}, 1),
        ("daemon.operations.submit", {"requestKey": "one"}, 1),
        ("daemon.operations.submit", {}, 0),
    ],
)
async def test_connection_loss_retries_only_replay_safe(monkeypatch, method, params, retries):
    bridge = Bridge(autostart=False)
    bridge._writer = Writer()
    bridge._attached = True
    calls = []

    async def noop():
        pass

    async def write(writer, frame):
        calls.append(frame)
        future = bridge._pending[frame["id"]]
        if len(calls) == 1:
            future.set_exception(TransportError("reply lost", code="DISCONNECTED"))
        else:
            future.set_result({"done": True})

    monkeypatch.setattr(bridge, "_ensure", noop)
    monkeypatch.setattr(bridge, "_teardown", noop)
    monkeypatch.setattr(ipc, "write_message", write)
    if retries:
        assert await bridge.call(method, params) == {"done": True}
    else:
        with pytest.raises(TransportError):
            await bridge.call(method, params)
    assert len(calls) == retries + 1


@pytest.mark.anyio
async def test_partial_write_failure_does_not_resend(monkeypatch):
    bridge = Bridge(autostart=False)
    bridge._writer = Writer()
    bridge._attached = True
    calls = []

    async def noop():
        pass

    async def broken(writer, frame):
        calls.append(frame)
        raise ConnectionError("drain failed after bytes were written")

    monkeypatch.setattr(bridge, "_ensure", noop)
    monkeypatch.setattr(ipc, "write_message", broken)
    with pytest.raises(TransportError) as error:
        await bridge.call("compose.send", {"mode": "send"})
    assert error.value.code == "OUTCOME_UNKNOWN"
    assert len(calls) == 1

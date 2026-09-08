"""Daemon advertisement, line framing, and the size a frame is allowed to be."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time

import pytest

from tbmcp import ipc
from tbmcp.bridge import Bridge
from tbmcp.daemon import Daemon
from tbmcp.errors import TransportError
from tbmcp.profile import ThunderbirdProfile

pytestmark = pytest.mark.anyio


def test_daemon_info_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    info = ipc.DaemonInfo(
        version=ipc.PROTOCOL_VERSION,
        port=51234,
        token=ipc.new_token(),
        pid=os.getpid(),
        profile="/somewhere",
    )
    info.write()
    loaded = ipc.DaemonInfo.load()
    assert loaded is not None
    assert (loaded.port, loaded.token, loaded.pid) == (info.port, info.token, info.pid)

    ipc.DaemonInfo.clear()
    assert ipc.DaemonInfo.load() is None


def test_a_dead_pid_is_treated_as_no_daemon(tmp_path, monkeypatch) -> None:
    """Otherwise every `serve` would try to talk to a port nobody is listening on."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    ipc.DaemonInfo(
        version=ipc.PROTOCOL_VERSION, port=1, token="x", pid=0x7FFFFFFF, profile=""
    ).write()
    assert ipc.DaemonInfo.load() is None


def test_a_wrong_protocol_version_is_ignored(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    ipc.DaemonInfo(
        version=ipc.PROTOCOL_VERSION + 99, port=1, token="x", pid=os.getpid(), profile=""
    ).write()
    assert ipc.DaemonInfo.load() is None


def test_corrupt_advertisement_is_ignored(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    ipc.DaemonInfo.path().write_text("{not json", encoding="utf-8")
    assert ipc.DaemonInfo.load() is None


def test_tokens_are_long_and_unique() -> None:
    tokens = {ipc.new_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(token) >= 32 for token in tokens)


class _Reader:
    """A StreamReader-alike that serves pre-canned lines."""

    def __init__(self, payload: bytes) -> None:
        self._buf = payload

    async def readline(self) -> bytes:
        index = self._buf.find(b"\n")
        if index == -1:
            line, self._buf = self._buf, b""
            return line
        line, self._buf = self._buf[: index + 1], self._buf[index + 1 :]
        return line


async def test_read_message_parses_one_object() -> None:
    reader = _Reader(b'{"t":"req","id":1}\n{"t":"res","id":1}\n')
    assert await ipc.read_message(reader) == {"t": "req", "id": 1}
    assert await ipc.read_message(reader) == {"t": "res", "id": 1}
    assert await ipc.read_message(reader) is None


async def test_read_message_rejects_malformed_json() -> None:
    with pytest.raises(TransportError) as caught:
        await ipc.read_message(_Reader(b"{oops\n"))
    assert caught.value.code == "BAD_FRAME"


async def test_read_message_rejects_a_non_object() -> None:
    with pytest.raises(TransportError):
        await ipc.read_message(_Reader(b"[1,2,3]\n"))


def test_encode_message_is_one_line_and_keeps_unicode() -> None:
    # Subjects and sender names are routinely non-ASCII; escaping them would bloat
    # every frame and make logs unreadable.
    encoded = ipc.encode_message({"subject": "Fatura – Ödeme"})
    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1
    assert "Ödeme" in json.loads(encoded)["subject"]


async def test_write_then_read_round_trip() -> None:
    class _Writer:
        def __init__(self) -> None:
            self.chunks: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.chunks.append(data)

        async def drain(self) -> None:
            return None

    writer = _Writer()
    frame = {"t": "req", "id": 7, "method": "prefs.get", "params": {"name": "a.b"}}
    await ipc.write_message(writer, frame)
    assert await ipc.read_message(_Reader(b"".join(writer.chunks))) == frame


# ------------------------------------------------------------------ the size ceiling
#
# `MAX_LINE` named the ceiling, but neither end passed it to asyncio, so what a frame
# really had to fit in was asyncio's 64 KiB default. Past it `readline()` raised a bare
# `ValueError`, the bridge's read loop died, and a wide search result — 200 hits, about
# 64 KB of JSON — wedged the connection for every later call.


async def test_read_message_reports_a_frame_the_buffer_cannot_hold() -> None:
    """Unhandled, that `ValueError` failed every pending call as a plain disconnect,
    which points the reader at the network instead of at the size."""

    async def flood(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError):
            writer.write(b"x" * 300_000)  # no newline, so it can never be framed
            await writer.drain()
        writer.close()

    async with await asyncio.start_server(flood, host="127.0.0.1", port=0) as server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=1024)
        try:
            with pytest.raises(TransportError) as caught:
                await ipc.read_message(reader)
        finally:
            writer.close()
    assert caught.value.code == "TOO_LARGE"


async def test_a_large_frame_survives_when_both_ends_agree_on_the_limit() -> None:
    frame = {"t": "res", "id": 1, "ok": True, "result": {"body": "m" * 200_000}}

    async def send_one(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError):
            await ipc.write_message(writer, frame)
        writer.close()

    async with await asyncio.start_server(
        send_one, host="127.0.0.1", port=0, limit=ipc.MAX_LINE
    ) as server:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=ipc.MAX_LINE)
        try:
            assert await ipc.read_message(reader) == frame
        finally:
            writer.close()


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    ipc.DaemonInfo.clear()
    yield tmp_path
    ipc.DaemonInfo.clear()


async def test_a_large_result_reaches_the_caller(isolated_state) -> None:
    """The live failure, end to end: 200 search hits came back, the bridge's reader
    gave up mid-frame, and the call — plus every other one in flight — died as
    DISCONNECTED."""
    result = {"messages": [{"subject": "invoice", "preview": "x" * 1000} for _ in range(200)]}

    async def daemon(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            while True:
                message = await ipc.read_message(reader)
                if message is None:
                    return
                if message.get("t") == "auth":
                    await ipc.write_message(writer, {"t": "ready", "id": message["id"]})
                    continue
                await ipc.write_message(
                    writer, {"t": "res", "id": message["id"], "ok": True, "result": result}
                )

    async with await asyncio.start_server(
        daemon, host="127.0.0.1", port=0, limit=ipc.MAX_LINE
    ) as server:
        ipc.DaemonInfo(
            version=ipc.PROTOCOL_VERSION,
            port=server.sockets[0].getsockname()[1],
            token="t",
            pid=os.getpid(),
            profile="",
        ).write()
        bridge = Bridge(autostart=False)
        try:
            # `daemon.status` is answered by the daemon itself, so nothing waits for
            # Thunderbird to attach.
            assert await bridge.call("daemon.status", timeout=10.0) == result
        finally:
            await bridge.close()


async def test_a_large_request_reaches_the_daemon(isolated_state) -> None:
    """The other direction: a mail_send carrying an attachment is a big frame too."""
    profile = ThunderbirdProfile(
        path=isolated_state, name="test", is_default=True, root=isolated_state
    )
    daemon = Daemon(profile, idle_timeout=0)
    running = asyncio.create_task(daemon.run())
    try:
        info = await _advertised_daemon()
        reader, writer = await asyncio.open_connection("127.0.0.1", info.port, limit=ipc.MAX_LINE)
        try:
            await ipc.write_message(writer, {"t": "auth", "id": 1, "token": info.token})
            ready = await asyncio.wait_for(ipc.read_message(reader), timeout=5.0)
            assert ready is not None and ready["t"] == "ready"
            await ipc.write_message(
                writer,
                {
                    "t": "req",
                    "id": 2,
                    "method": "daemon.status",
                    "params": {"attachment": "p" * 200_000},
                    "timeout": 5.0,
                },
            )
            reply = await asyncio.wait_for(ipc.read_message(reader), timeout=5.0)
            assert reply is not None and reply["ok"], reply
        finally:
            writer.close()
    finally:
        daemon._stop.set()
        await asyncio.wait_for(running, timeout=10.0)


async def _advertised_daemon() -> ipc.DaemonInfo:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        info = ipc.DaemonInfo.load()
        if info is not None:
            return info
        await asyncio.sleep(0.05)
    raise AssertionError("the daemon never advertised itself")


class _StubWriter:
    """Just enough writer for what the read loop does on its way out."""

    def __init__(self) -> None:
        self._closing = False

    def close(self) -> None:
        self._closing = True

    def is_closing(self) -> bool:
        return self._closing


def _stub_bridge() -> tuple[Bridge, _StubWriter]:
    """A bridge holding a reader that cannot frame much, and a writer we can watch."""
    bridge = Bridge(autostart=False)
    bridge._reader = asyncio.StreamReader(limit=1024)
    writer = _StubWriter()
    bridge._writer = writer  # type: ignore[assignment]
    return bridge, writer


def _in_flight(bridge: Bridge) -> asyncio.Future:
    future = asyncio.get_running_loop().create_future()
    bridge._pending[1] = future
    return future


async def test_an_oversized_frame_fails_the_call_by_name() -> None:
    """DISCONNECTED sends whoever reads it looking at the daemon; the size is the one
    thing they need to know."""
    bridge, _ = _stub_bridge()
    pending = _in_flight(bridge)
    bridge._reader.feed_data(b"x" * 4096)  # no newline, well past the buffer
    await bridge._read_loop()
    assert pending.exception().code == "TOO_LARGE"


async def test_a_peer_that_hangs_up_still_reads_as_a_disconnect() -> None:
    bridge, _ = _stub_bridge()
    pending = _in_flight(bridge)
    bridge._reader.feed_eof()
    await bridge._read_loop()
    assert pending.exception().code == "DISCONNECTED"


async def test_the_read_loop_closes_the_writer_it_can_no_longer_read_for() -> None:
    """`_ensure` judges the connection by the writer alone. Left open after the reader
    died, the next call wrote into a socket nobody was reading and then waited out its
    whole timeout — one unreadable frame wedged the bridge for good."""
    bridge, writer = _stub_bridge()
    bridge._reader.feed_eof()
    await bridge._read_loop()
    assert writer.is_closing()

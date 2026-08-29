"""Daemon advertisement and line framing."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from tbmcp import ipc
from tbmcp.errors import TransportError

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


async def test_a_frame_larger_than_64k_survives_the_wire() -> None:
    """asyncio's StreamReader defaults to a 64 KiB buffer — a thousandth of the
    ceiling MAX_LINE names — and `readline()` raises past it, killing the
    connection. Any answer over ~64 KB (a wide search, a bulk read) hit that
    instead of arriving, so both ends have to be opened with an explicit limit."""
    payload = {"t": "res", "id": 1, "ok": True, "result": {"blob": "x" * 200_000}}
    received: list[dict] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await ipc.write_message(writer, payload)
        await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=ipc.MAX_LINE)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=ipc.MAX_LINE)
        received.append(await ipc.read_message(reader))
        writer.close()
    finally:
        server.close()
        await server.wait_closed()

    assert received[0] == payload


async def test_an_oversized_frame_is_a_typed_error_not_a_dead_socket() -> None:
    """If the ceiling is ever genuinely exceeded, the caller should learn that it
    was a size problem rather than watch the connection drop."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"y" * 300_000)  # no newline: the buffer fills and never frames
        await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, _writer = await asyncio.open_connection("127.0.0.1", port, limit=1024)
        with pytest.raises(TransportError) as caught:
            await ipc.read_message(reader)
        assert caught.value.code == "TOO_LARGE"
    finally:
        server.close()
        await server.wait_closed()

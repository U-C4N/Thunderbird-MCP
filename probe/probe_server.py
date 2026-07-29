"""Dependency-free loopback listener used by the capability probe.

Speaks just enough HTTP and WebSocket (RFC 6455) on 127.0.0.1:8271 to prove that
a Thunderbird background page can reach a Python process, and prints whatever
the add-on sends. Run it before restarting Thunderbird.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
import sys

HOST = "127.0.0.1"
PORT = 8271
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def log(*parts: object) -> None:
    print(*parts, flush=True)


async def read_headers(reader: asyncio.StreamReader) -> tuple[str, dict[str, str]]:
    request_line = (await reader.readline()).decode("latin-1").strip()
    headers: dict[str, str] = {}
    while True:
        line = (await reader.readline()).decode("latin-1")
        if line in ("\r\n", "\n", ""):
            break
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return request_line, headers


async def ws_read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    header = await reader.readexactly(2)
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(length)
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def ws_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    header = bytes([0x80 | opcode])
    size = len(payload)
    if size < 126:
        header += bytes([size])
    elif size < 1 << 16:
        header += bytes([126]) + struct.pack(">H", size)
    else:
        header += bytes([127]) + struct.pack(">Q", size)
    return header + payload


async def handle_websocket(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, key: str
) -> None:
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    writer.write(
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
    )
    await writer.drain()
    log("  [ws] handshake completed -- WebSocket from the add-on WORKS")
    while True:
        try:
            frame = await ws_read_frame(reader)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            break
        if frame is None:
            break
        opcode, payload = frame
        if opcode == 0x8:
            break
        if opcode == 0x9:  # ping
            writer.write(ws_frame(payload, 0xA))
            await writer.drain()
            continue
        text = payload.decode("utf-8", "replace")
        log(f"  [ws] recv: {text[:400]}")
        writer.write(ws_frame(json.dumps({"echo": "tbmcp-probe-server"}).encode()))
        await writer.drain()
    log("  [ws] closed")


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    try:
        request_line, headers = await read_headers(reader)
    except Exception as exc:
        log(f"[{peer}] header error: {exc}")
        writer.close()
        return
    log(f"[{peer}] {request_line}  origin={headers.get('origin', '-')}")

    if headers.get("upgrade", "").lower() == "websocket":
        await handle_websocket(reader, writer, headers.get("sec-websocket-key", ""))
    else:
        length = int(headers.get("content-length", "0") or 0)
        body = (await reader.readexactly(length)).decode("utf-8", "replace") if length else ""
        if body:
            log(f"  [http] body: {body[:400]}")
        log("  [http] responded 200 -- fetch() from the add-on WORKS")
        payload = json.dumps({"ok": True, "server": "tbmcp-probe-server"}).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Access-Control-Allow-Origin: *\r\n"
            b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload
        )
        await writer.drain()
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


async def main() -> None:
    server = await asyncio.start_server(handle, HOST, PORT)
    log(f"probe server listening on http://{HOST}:{PORT} (HTTP + WebSocket)")
    log("Now restart Thunderbird. Ctrl+C to stop.")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)

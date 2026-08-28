"""The broker.

Exactly one process owns the loopback listener the Thunderbird add-on dials into,
and multiplexes requests from every `tbmcp serve` process onto that single
connection. That is what lets Claude Code and Codex drive one Thunderbird at the
same time.

Layout:

    add-on  ──WebSocket──▶  AddonSession   ◀── ControlServer ◀──JSON lines── serve
                             (one)                                            (many)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import ipc
from .errors import NotConnectedError, TimeoutError_, TransportError, from_wire
from .profile import BRIDGE_FILE, ThunderbirdProfile, find_profile

log = logging.getLogger("tbmcp.daemon")

CHUNK_LIMIT = 4 * 1024 * 1024
PING_INTERVAL = 20.0
DEFAULT_IDLE_TIMEOUT = 900.0

ProgressCb = Callable[[dict[str, Any]], Awaitable[None]]


def error_payload(exc: BaseException) -> dict[str, Any]:
    """A failure in the protocol's `error` shape, ready to relay.

    Uses `.message` rather than `str(exc)`. This hop re-serialises an error the
    add-on already sent us structured, and `TbmcpError.__str__` renders `code` and
    `needs` *into* the text for a human reader — so relaying that baked them into
    the message, and the far side then rendered its own copy from the structured
    fields it was also given. `[Error] [Error]` and a doubled `(requires: …)` were
    both this. Round-tripping has to be idempotent, because it happens twice on
    every add-on failure: once here, once in `Bridge.call`.
    """
    payload: dict[str, Any] = {
        "kind": getattr(exc, "kind", "internal"),
        "code": getattr(exc, "code", None),
        "message": getattr(exc, "message", None) or str(exc),
    }
    needs = getattr(exc, "needs", None)
    if needs:
        payload["needs"] = needs
    return payload


@dataclass
class _Pending:
    future: asyncio.Future[Any]
    method: str
    on_progress: ProgressCb | None = None
    chunks: list[bytes] = field(default_factory=list)


class AddonSession:
    """One live add-on connection."""

    def __init__(self, ws: ServerConnection, hello: dict[str, Any]) -> None:
        self.ws = ws
        self.hello = hello
        self.app: dict[str, Any] = hello.get("app") or {}
        self.capabilities: dict[str, Any] = hello.get("capabilities") or {}
        self.addon_version: str = str(hello.get("addonVersion", "?"))
        self.connected_at = time.time()
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._closed = asyncio.Event()

    @property
    def has_experiment(self) -> bool:
        return bool(self.capabilities.get("experiment"))

    def describe(self) -> dict[str, Any]:
        return {
            "addonVersion": self.addon_version,
            "app": self.app,
            "experiment": self.has_experiment,
            "namespaces": self.capabilities.get("namespaces", []),
            "connectedForSeconds": round(time.time() - self.connected_at, 1),
            "inFlight": len(self._pending),
        }

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
        on_progress: ProgressCb | None = None,
    ) -> Any:
        if self._closed.is_set():
            raise NotConnectedError()
        if method.startswith("x.") and not self.has_experiment:
            raise TransportError(
                f"{method} needs the privileged half of the add-on, which did not load. "
                "Reinstall with `tbmcp install-addon` and check Thunderbird's error console.",
                code="NO_EXPERIMENT",
            )
        self._next_id += 1
        request_id = self._next_id
        loop = asyncio.get_running_loop()
        pending = _Pending(future=loop.create_future(), method=method, on_progress=on_progress)
        self._pending[request_id] = pending
        frame = {
            "t": "req",
            "id": request_id,
            "method": method,
            "params": params or {},
            "deadlineMs": int(timeout * 1000),
        }
        try:
            await self.ws.send(ipc.encode_message(frame).decode("utf-8"))
        except ConnectionClosed as exc:
            self._pending.pop(request_id, None)
            raise NotConnectedError() from exc
        try:
            return await asyncio.wait_for(pending.future, timeout=timeout)
        except TimeoutError:
            with contextlib.suppress(ConnectionClosed):
                await self.ws.send(json.dumps({"t": "cancel", "id": request_id}))
            raise TimeoutError_(method, timeout) from None
        finally:
            self._pending.pop(request_id, None)

    async def pump(self, on_event: Callable[[dict[str, Any]], None]) -> None:
        """Read frames until the add-on goes away."""
        try:
            async for raw in self.ws:
                try:
                    frame = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    log.warning("dropping malformed frame from add-on")
                    continue
                if not isinstance(frame, dict):
                    continue
                await self._handle(frame, on_event)
        except ConnectionClosed:
            pass
        finally:
            self._closed.set()
            for pending in self._pending.values():
                if not pending.future.done():
                    pending.future.set_exception(NotConnectedError())
            self._pending.clear()

    async def _handle(
        self, frame: dict[str, Any], on_event: Callable[[dict[str, Any]], None]
    ) -> None:
        kind = frame.get("t")
        if kind == "ping":
            with contextlib.suppress(ConnectionClosed):
                await self.ws.send(json.dumps({"t": "pong", "ts": frame.get("ts")}))
            return
        if kind == "event":
            on_event(frame)
            return
        if kind == "progress":
            pending = self._pending.get(int(frame.get("id", -1)))
            if pending and pending.on_progress:
                with contextlib.suppress(Exception):
                    await pending.on_progress(frame)
            return
        if kind != "res":
            return

        pending = self._pending.get(int(frame.get("id", -1)))
        if pending is None or pending.future.done():
            return

        chunk = frame.get("chunked")
        if isinstance(chunk, dict):
            payload = frame.get("result")
            if isinstance(payload, str):
                pending.chunks.append(base64.b64decode(payload))
            if not chunk.get("last"):
                return
            joined = b"".join(pending.chunks)
            try:
                pending.future.set_result(json.loads(joined.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                pending.future.set_exception(
                    TransportError(f"chunked result was not valid JSON: {exc}", code="BAD_CHUNK")
                )
            return

        if frame.get("ok"):
            pending.future.set_result(frame.get("result"))
        else:
            error = frame.get("error")
            pending.future.set_exception(
                from_wire(pending.method, error if isinstance(error, dict) else {})
            )

    async def keepalive(self) -> None:
        while not self._closed.is_set():
            await asyncio.sleep(PING_INTERVAL)
            try:
                await asyncio.wait_for(self.ws.ping(), timeout=PING_INTERVAL)
            except (TimeoutError, ConnectionClosed):
                with contextlib.suppress(ConnectionClosed):
                    await self.ws.close(code=1011, reason="keepalive timeout")
                return


class Daemon:
    def __init__(
        self,
        profile: ThunderbirdProfile,
        *,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        event_buffer: int = 500,
        startup_lock: ipc.DaemonLock | None = None,
    ) -> None:
        self.profile = profile
        self.idle_timeout = idle_timeout
        self.startup_lock = startup_lock
        self.addon_token = ipc.new_token()
        self.control_token = ipc.new_token()
        self.session: AddonSession | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=event_buffer)
        self.event_seq = 0
        self.started_at = time.time()
        self._clients = 0
        self._last_client_at = time.time()
        self._inflight: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()
        self._session_ready = asyncio.Event()

    # ------------------------------------------------------------------ add-on side

    async def _serve_addon(self, ws: ServerConnection) -> None:
        peer = ws.remote_address[0] if ws.remote_address else "?"
        if peer not in ("127.0.0.1", "::1", "localhost"):
            await ws.close(code=4003, reason="non-loopback peer")
            return
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            hello = json.loads(raw)
        except (TimeoutError, ConnectionClosed, json.JSONDecodeError, TypeError):
            await ws.close(code=4002, reason="expected a hello frame")
            return
        if not isinstance(hello, dict) or hello.get("t") != "hello":
            await ws.close(code=4002, reason="expected a hello frame")
            return
        if hello.get("token") != self.addon_token:
            log.warning("rejected an add-on connection with a bad token")
            await ws.close(code=4001, reason="bad token")
            return
        if int(hello.get("protocol", 0)) != ipc.PROTOCOL_VERSION:
            await ws.close(code=4002, reason="protocol mismatch")
            return

        if self.session is not None:
            # A restarted Thunderbird, or a second window; the newest wins.
            with contextlib.suppress(ConnectionClosed):
                await self.session.ws.close(code=1012, reason="superseded")

        session = AddonSession(ws, hello)
        self.session = session
        self._session_ready.set()
        await ws.send(
            json.dumps({"t": "welcome", "protocol": ipc.PROTOCOL_VERSION, "server": "tbmcp"})
        )
        log.info(
            "add-on connected: %s %s (experiment=%s)",
            session.app.get("name", "Thunderbird"),
            session.app.get("version", "?"),
            session.has_experiment,
        )
        keepalive = asyncio.create_task(session.keepalive())
        try:
            await session.pump(self._record_event)
        finally:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive
            if self.session is session:
                self.session = None
                self._session_ready.clear()
            log.info("add-on disconnected")

    def _record_event(self, frame: dict[str, Any]) -> None:
        self.event_seq += 1
        self.events.append(
            {
                "seq": self.event_seq,
                "at": time.time(),
                "name": frame.get("name"),
                "data": frame.get("data"),
            }
        )

    # ------------------------------------------------------------------ control side

    async def _serve_control(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        if not peer or peer[0] not in ("127.0.0.1", "::1"):
            writer.close()
            return
        authenticated = False
        self._clients += 1
        try:
            while True:
                message = await ipc.read_message(reader)
                if message is None:
                    return
                if not authenticated:
                    if message.get("t") != "auth" or message.get("token") != self.control_token:
                        await ipc.write_message(
                            writer,
                            {
                                "t": "res",
                                "id": message.get("id"),
                                "ok": False,
                                "error": {
                                    "kind": "transport",
                                    "code": "UNAUTHORIZED",
                                    "message": "bad control token",
                                },
                            },
                        )
                        return
                    authenticated = True
                    await ipc.write_message(writer, {"t": "ready", "id": message.get("id")})
                    continue
                # Dispatch concurrently so one slow call cannot block the rest of
                # this client's requests. Keeping a reference matters: a task held
                # only by the event loop can be garbage-collected mid-flight, which
                # would look like Thunderbird silently dropping a request.
                task = asyncio.create_task(self._dispatch(message, writer))
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
        except TransportError as exc:
            log.warning("control connection error: %s", exc)
        finally:
            self._clients -= 1
            self._last_client_at = time.time()
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _dispatch(self, message: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        request_id = message.get("id")
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        timeout = float(message.get("timeout") or 30.0)
        wants_progress = bool(message.get("wantsProgress"))

        async def forward_progress(frame: dict[str, Any]) -> None:
            await ipc.write_message(
                writer,
                {
                    "t": "progress",
                    "id": request_id,
                    "done": frame.get("done"),
                    "total": frame.get("total"),
                    "message": frame.get("message"),
                },
            )

        try:
            result = await self._invoke(
                method,
                params,
                timeout=timeout,
                on_progress=forward_progress if wants_progress else None,
            )
        except Exception as exc:
            payload = error_payload(exc)
            with contextlib.suppress(Exception):
                await ipc.write_message(
                    writer, {"t": "res", "id": request_id, "ok": False, "error": payload}
                )
            return
        with contextlib.suppress(Exception):
            await ipc.write_message(
                writer, {"t": "res", "id": request_id, "ok": True, "result": result}
            )

    async def _invoke(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        on_progress: ProgressCb | None,
    ) -> Any:
        """Daemon-local methods first, then anything the add-on handles."""
        if method == "daemon.status":
            return self.status()
        if method == "daemon.events":
            since = int(params.get("since") or 0)
            limit = int(params.get("limit") or 100)
            selected = [e for e in self.events if e["seq"] > since][:limit]
            return {"events": selected, "latestSeq": self.event_seq}
        if method == "daemon.waitForThunderbird":
            wait = float(params.get("timeout") or 30.0)
            try:
                await asyncio.wait_for(self._session_ready.wait(), timeout=wait)
            except TimeoutError:
                raise NotConnectedError() from None
            return self.status()
        if method == "daemon.shutdown":
            self._stop.set()
            return {"stopping": True}

        session = self.session
        if session is None:
            raise NotConnectedError()
        return await session.call(method, params, timeout=timeout, on_progress=on_progress)

    def status(self) -> dict[str, Any]:
        return {
            "daemon": {
                "pid": os.getpid(),
                "uptimeSeconds": round(time.time() - self.started_at, 1),
                "clients": self._clients,
                "eventsBuffered": len(self.events),
                "latestEventSeq": self.event_seq,
            },
            "profile": {"path": str(self.profile.path), "name": self.profile.name},
            "thunderbird": self.session.describe() if self.session else None,
            "connected": self.session is not None,
        }

    # ------------------------------------------------------------------ lifecycle

    def _write_bridge_file(self, port: int) -> None:
        payload = {
            "version": ipc.PROTOCOL_VERSION,
            "port": port,
            "token": self.addon_token,
            "pid": os.getpid(),
        }
        path = self.profile.path / BRIDGE_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        ipc._restrict_permissions(path)
        log.info("pairing file written to %s (port %d)", path, port)

    def _cleanup(self) -> None:
        with contextlib.suppress(OSError):
            (self.profile.path / BRIDGE_FILE).unlink()
        ipc.DaemonInfo.clear()

    async def _watch_idle(self) -> None:
        if self.idle_timeout <= 0:
            return
        while not self._stop.is_set():
            await asyncio.sleep(30.0)
            if self._clients == 0 and (time.time() - self._last_client_at) > self.idle_timeout:
                log.info("no clients for %.0fs — shutting down", self.idle_timeout)
                self._stop.set()

    async def _watch_superseded(self) -> None:
        """Stand down if another daemon has taken over the advertisement.

        Only one daemon can hold the add-on's connection, but nothing stops two from
        binding their own sockets. When that happened the add-on stayed glued to the
        older one while `serve` talked to the newer, and every tool reported
        "Thunderbird is not connected" — with both processes looking healthy. Losing
        the advertisement is the signal to exit and let the winner have the add-on.
        """
        while not self._stop.is_set():
            await asyncio.sleep(20.0)
            winner = self.superseded_by()
            if winner is not None:
                log.warning("daemon %d has taken over the advertisement — standing down", winner)
                self._stop.set()

    @staticmethod
    def superseded_by() -> int | None:
        """The pid that owns the advertisement, if it is not us."""
        advertised = ipc.DaemonInfo.load()
        if advertised is None or advertised.pid == os.getpid():
            return None
        return advertised.pid

    async def run(self) -> None:
        async with (
            await asyncio.start_server(self._serve_control, host="127.0.0.1", port=0) as control,
            serve(
                self._serve_addon, host="127.0.0.1", port=0, max_size=CHUNK_LIMIT * 2
            ) as addon_server,
        ):
            control_port = control.sockets[0].getsockname()[1]
            addon_port = addon_server.sockets[0].getsockname()[1]
            self._write_bridge_file(addon_port)
            ipc.DaemonInfo(
                version=ipc.PROTOCOL_VERSION,
                port=control_port,
                token=self.control_token,
                pid=os.getpid(),
                profile=str(self.profile.path),
            ).write()
            # Published: the startup race is over, so stop blocking other starts.
            if self.startup_lock is not None:
                self.startup_lock.release()
                self.startup_lock = None
            log.info("daemon ready (control %d, add-on %d)", control_port, addon_port)
            watchdogs = [
                asyncio.create_task(self._watch_idle()),
                asyncio.create_task(self._watch_superseded()),
            ]
            try:
                await self._stop.wait()
            finally:
                for task in watchdogs:
                    task.cancel()
                for task in watchdogs:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                self._cleanup()


async def run_daemon(
    profile_hint: str | None = None,
    *,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    force: bool = False,
) -> int:
    # One daemon per user, because only one can hold the add-on's connection.
    #
    # The lock covers *startup only*, and is released as soon as the advertisement is
    # published. Holding it for the daemon's whole life looked tidier but could wedge
    # the system: if the holder ever stopped advertising, every later daemon stood
    # down without publishing anything, and the bridge was stuck until that process
    # died. The advertisement is the source of truth; the lock only stops two daemons
    # binding in the same instant, and `_watch_superseded` handles the rest.
    lock: ipc.DaemonLock | None = None
    if not force:
        lock = ipc.DaemonLock.acquire()
        if lock is None:
            # Someone is starting up. Give them a moment to advertise before deciding.
            existing = await _await_advertisement(timeout=6.0)
            if existing is not None:
                log.info(
                    "a daemon is already running (pid %d, control port %d); nothing to do",
                    existing.pid,
                    existing.port,
                )
                return 0
            log.warning("a stale start-up lock is in the way; taking it over")
            lock = ipc.DaemonLock.acquire_forcibly()

    profile = find_profile(profile_hint)
    if profile is None:
        log.error("no Thunderbird profile found; pass --profile with an explicit path")
        if lock is not None:
            lock.release()
        return 2

    daemon = Daemon(profile, idle_timeout=idle_timeout, startup_lock=lock)
    try:
        await daemon.run()
    except asyncio.CancelledError:
        daemon._cleanup()
        raise
    finally:
        if lock is not None:
            lock.release()
    return 0


async def _await_advertisement(*, timeout: float) -> ipc.DaemonInfo | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = ipc.DaemonInfo.load()
        if info is not None:
            return info
        await asyncio.sleep(0.25)
    return ipc.DaemonInfo.load()

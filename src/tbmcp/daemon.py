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
import hashlib
import json
import logging
import os
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import ipc
from .errors import (
    BlockedError,
    NotConnectedError,
    TimeoutError_,
    TransportError,
    UsageError,
    from_wire,
)
from .handshake import HandshakeLog, describe_handshake
from .methods import is_read
from .operations import Operations
from .profile import BRIDGE_FILE, ThunderbirdProfile, find_profile

log = logging.getLogger("tbmcp.daemon")

CHUNK_LIMIT = 4 * 1024 * 1024
PING_INTERVAL = 20.0
DEFAULT_IDLE_TIMEOUT = 900.0
HELLO_TIMEOUT = 10.0
"""How long a connected add-on has to send its `hello`. A module constant so the
handshake tests can shorten it without waiting out a real ten seconds."""

ProgressCb = Callable[[dict[str, Any]], Awaitable[None]]


def _profile_journal_id(path) -> str:
    value = str(path.resolve())
    if sys.platform == "win32":
        value = value.casefold()
    return hashlib.sha256(value.encode()).hexdigest()


def error_payload(exc: BaseException) -> dict[str, Any]:
    """Serialise a failure into the protocol's `error` object.

    Deliberately reads `.message` rather than `str(exc)`. `TbmcpError.__str__` renders
    the code and the `needs` list into the text for humans, so relaying `str(exc)`
    appended them again on every hop: a "not connected" that crossed the add-on, the
    daemon and the bridge reached the caller as "... [NOT_CONNECTED] [NOT_CONNECTED]".
    Reading the field back keeps a relayed error identical to the one that was sent.
    """
    payload: dict[str, Any] = {
        "kind": getattr(exc, "kind", "internal"),
        "code": getattr(exc, "code", None),
        "message": getattr(exc, "message", None) or str(exc),
    }
    needs = getattr(exc, "needs", None)
    if needs:
        payload["needs"] = needs
    extra = getattr(exc, "extra", {})
    if isinstance(extra.get("recoveryWindowOpen"), bool):
        payload["recoveryWindowOpen"] = extra["recoveryWindowOpen"]
    if type(extra.get("tabId")) is int:
        payload["tabId"] = extra["tabId"]
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
        self.uncertain: set[asyncio.Future] = set()
        self.late_outcomes: dict[int, dict[str, Any]] = {}

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
            "runtimeEpoch": self.hello.get("runtimeEpoch"),
            "capabilities": self.capabilities,
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
        if method in {"search.global", "search.conversation", "messages.followups"} and (
            method not in self.capabilities.get("methods", [])
        ):
            raise BlockedError(
                f"{method} is unavailable in the connected add-on; install the matching add-on.",
                code="UNSUPPORTED_CAPABILITY",
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
            return await asyncio.wait_for(asyncio.shield(pending.future), timeout=timeout)
        except (TimeoutError, asyncio.CancelledError) as exc:
            # Cancellation cannot roll a native mutation back. Keep the late
            # response so the daemon can tell when a timed-out write has ended.
            self.uncertain.add(pending.future)

            def completed(future):
                self.uncertain.discard(future)
                self._pending.pop(request_id, None)
                if not future.cancelled():
                    error = future.exception()
                    self.late_outcomes[request_id] = (
                        {"error": error} if error else {"result": future.result()}
                    )
                    if len(self.late_outcomes) > 64:
                        self.late_outcomes.pop(next(iter(self.late_outcomes)))

            pending.future.add_done_callback(completed)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise TimeoutError_(method, timeout) from None
        finally:
            if pending.future not in self.uncertain:
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
        self.handshakes = HandshakeLog()
        self.session: AddonSession | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=event_buffer)
        self.event_seq = 0
        self.started_at = time.time()
        self._clients = 0
        self._last_client_at = time.time()
        self._inflight: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()
        self._session_ready = asyncio.Event()
        self.event_epoch = ipc.new_token()
        self._write_lock = asyncio.Lock()
        self._operations: Operations | None = None
        self._barrier_session: AddonSession | None = None
        self._ever_connected = False

    # ------------------------------------------------------------------ add-on side

    async def _serve_addon(self, ws: ServerConnection) -> None:
        """One add-on connection, from the upgrade to whatever ends it.

        Every exit records its own outcome in `self.handshakes`. Splitting what used
        to be one `except` is the point: "the socket closed before the hello" and
        "the hello was not JSON" are different faults with different remedies, and
        collapsing them left the user with nothing but a 4002 close code.
        """
        peer = ws.remote_address[0] if ws.remote_address else "?"
        if peer not in ("127.0.0.1", "::1", "localhost"):
            self.handshakes.resolve(ws, "non-loopback", close_code=4003, detail=str(peer))
            await ws.close(code=4003, reason="non-loopback peer")
            return
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=HELLO_TIMEOUT)
        except TimeoutError:
            self.handshakes.resolve(
                ws, "no-hello-timeout", close_code=4002, detail=f"{HELLO_TIMEOUT:g}s"
            )
            await ws.close(code=4002, reason="expected a hello frame")
            return
        except ConnectionClosed:
            # Already gone: there is nothing left to close, only to record.
            self.handshakes.resolve(ws, "closed-before-hello", close_code=ws.close_code)
            return
        try:
            hello = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            await self._reject_hello(ws, detail=str(exc))
            return
        if not isinstance(hello, dict) or hello.get("t") != "hello":
            await self._reject_hello(ws, detail=f"first frame was {type(hello).__name__}")
            return
        if hello.get("token") != self.addon_token:
            self.handshakes.resolve(ws, "bad-token", close_code=4001)
            await ws.close(code=4001, reason="bad token")
            return
        try:
            protocol = int(hello.get("protocol", 0))
        except (TypeError, ValueError):
            # A non-numeric version used to raise straight out of the handler, so
            # websockets logged a traceback and the add-on was told nothing at all.
            protocol = -1
        if protocol != ipc.PROTOCOL_VERSION:
            self.handshakes.resolve(
                ws, "protocol-mismatch", close_code=4002, detail=f"protocol {protocol}"
            )
            await ws.close(code=4002, reason="protocol mismatch")
            return

        if self.session is not None:
            # A restarted Thunderbird, or a second window; the newest wins.
            self.handshakes.resolve(self.session.ws, "superseded", close_code=1012)
            with contextlib.suppress(ConnectionClosed):
                await self.session.ws.close(code=1012, reason="superseded")

        session = AddonSession(ws, hello)
        self.session = session
        self._session_ready.set()
        await ws.send(
            json.dumps({"t": "welcome", "protocol": ipc.PROTOCOL_VERSION, "server": "tbmcp"})
        )
        self.handshakes.resolve(ws, "welcomed", addon_version=session.addon_version)
        if self._ever_connected:
            self._record_event(
                {"name": "bridge.reconnected", "data": {"resyncRequired": True, "lostCount": None}}
            )
        self._ever_connected = True
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
                self._record_event(
                    {
                        "name": "bridge.disconnected",
                        "data": {"resyncRequired": True, "lostCount": None},
                    }
                )
            # A no-op for a session we superseded: that outcome is already final.
            self.handshakes.resolve(ws, "disconnected", close_code=ws.close_code)

    async def _reject_hello(self, ws: ServerConnection, *, detail: str) -> None:
        self.handshakes.resolve(ws, "bad-hello", close_code=4002, detail=detail)
        await ws.close(code=4002, reason="expected a hello frame")

    def _connection_class(self) -> type[ServerConnection]:
        """A `ServerConnection` that reports the whole life of the TCP connection.

        `_serve_addon` only runs once the HTTP upgrade has succeeded, so a client
        that connects and never upgrades — which is exactly what Thunderbird did for
        half an hour — is invisible to it. These two hooks are the only place that
        sees it happen.
        """
        handshakes = self.handshakes

        class TelemetryConnection(ServerConnection):
            def connection_made(self, transport: asyncio.BaseTransport) -> None:
                # After super(): it installs the transport that `remote_address`,
                # and so the peer port, is read from.
                super().connection_made(transport)
                handshakes.opened(self)

            def connection_lost(self, exc: Exception | None) -> None:
                super().connection_lost(exc)
                handshakes.resolve(self, "no-upgrade")

        return TelemetryConnection

    def _on_upgrade_request(self, conn: ServerConnection, request: Any) -> None:
        """`process_request`: note who is dialling in, then accept by returning None."""
        # Nothing here may raise: websockets turns an exception from process_request
        # into a 500 and rejects the connection, which would make the telemetry the
        # outage it is meant to explain.
        with contextlib.suppress(Exception):
            self.handshakes.note_request(
                conn,
                path=getattr(request, "path", None),
                origin=request.headers.get("Origin"),
                user_agent=request.headers.get("User-Agent"),
            )
        return None

    def _addon_server(self, **overrides: Any) -> serve:
        """The add-on listener, factored out so tests exercise the server `run` runs."""
        kwargs: dict[str, Any] = {
            "host": "127.0.0.1",
            "port": 0,
            "max_size": CHUNK_LIMIT * 2,
            "create_connection": self._connection_class(),
            "process_request": self._on_upgrade_request,
        }
        kwargs.update(overrides)
        return serve(self._serve_addon, **kwargs)

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
            with contextlib.suppress(Exception):
                await ipc.write_message(
                    writer,
                    {"t": "res", "id": request_id, "ok": False, "error": error_payload(exc)},
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
        if self._stop.is_set() and not is_read(method):
            raise BlockedError(
                "Daemon is shutting down; reconnect before writing.", code="DAEMON_STOPPING"
            )
        if method == "daemon.status":
            return self.status()
        if method == "daemon.events":
            return self._events_page(params)
        if method.startswith("daemon.operations."):
            return await self.operations().call(method.rsplit(".", 1)[-1], params)
        if method == "daemon.drafts.get":
            return await self.operations().draft_get(params)
        if method == "daemon.waitForThunderbird":
            wait = float(params.get("timeout") or 30.0)
            try:
                await asyncio.wait_for(self._session_ready.wait(), timeout=wait)
            except TimeoutError:
                raise self._not_connected() from None
            return self.status()
        if method == "daemon.shutdown":
            if self._write_lock.locked():
                raise BlockedError(
                    "A write is active or unresolved; finish it before replacing the daemon.",
                    code="OPERATIONS_ACTIVE",
                )
            async with self._write_lock:
                # Reconciliation must hold the same lock as dispatch. Otherwise
                # an old receipt can erase the barrier of a concurrent new write.
                if (
                    self.session
                    and self._operations
                    and not self._operations.busy
                    and self._operations.journal.barrier()
                ):
                    await self._clear_settled_barrier()
                if self.writes_active(include_lock=False):
                    raise BlockedError(
                        "A write is active or unresolved; finish it before replacing the daemon.",
                        code="OPERATIONS_ACTIVE",
                    )
                self._stop.set()
                return {"stopping": True}

        if is_read(method):
            return await self._addon_call(method, params, timeout=timeout, on_progress=on_progress)
        if method == "x.admin.restart":
            # Explicit, gated recovery remains possible when an uncertain old
            # add-on cannot produce a completion receipt.
            return await self._addon_call(method, params, timeout=timeout, on_progress=on_progress)
        async with self._write_lock:
            return await self._guarded_call(
                method, params, timeout=timeout, on_progress=on_progress
            )

    def operations(self) -> Operations:
        if self._operations is None:
            self._operations = Operations(self, self._operation_path())
        return self._operations

    def _operation_path(self):
        return (
            ipc.state_dir()
            / "profiles"
            / _profile_journal_id(self.profile.path)
            / "operations.sqlite"
        )

    def journal_health(self) -> dict[str, Any]:
        health = {"available": True, "retentionDays": 7, "busy": False, "writeBlocked": False}
        try:
            # Recover persisted state before reporting it, without creating a
            # journal merely because a read-only client requested diagnostics.
            if self._operations is None and self._operation_path().exists():
                self.operations()
            if self._operations:
                journal = self._operations.journal
                worker_error = getattr(self._operations, "health_error", None)
                if worker_error:
                    health.update(available=False, writeBlocked=True, error=worker_error)
                journal.purge()
                health.update(
                    busy=self._operations.busy,
                    writeBlocked=bool(worker_error or journal.barrier()),
                    counts={
                        row[0]: row[1]
                        for row in journal.db.execute(
                            "SELECT status,COUNT(*) FROM operations GROUP BY status"
                        )
                    },
                )
        except Exception as exc:
            health.update(available=False, writeBlocked=True, error=error_payload(exc))
        return health

    def writes_active(self, *, include_lock: bool = True) -> bool:
        health = self.journal_health()
        if (include_lock and self._write_lock.locked()) or health["busy"]:
            return True
        if self._operations:
            try:
                # A worker health latch blocks new writes but does not prevent
                # explicit daemon recovery once native work is proven finished.
                return bool(self._operations.journal.barrier())
            except Exception:
                return True
        return bool(health["writeBlocked"])

    async def _addon_call(
        self, method: str, params: dict, *, timeout=30.0, on_progress=None
    ) -> Any:
        session = self.session
        if session is None:
            raise self._not_connected()
        return await session.call(method, params, timeout=timeout, on_progress=on_progress)

    async def _clear_settled_barrier(self) -> None:
        journal = self.operations().journal
        barrier = journal.barrier()
        if not barrier:
            return
        session = self.session
        if session is None:
            raise self._not_connected()
        epoch = session.hello.get("runtimeEpoch")
        # Native XPCOM work may outlive an add-on reload. A changed runtime epoch
        # alone never proves that a filter/calendar/send has stopped.
        settled = False
        if barrier.get("createdAt") and (
            barrier.get("needsRestart") or epoch != barrier.get("epoch") or not epoch
        ):
            try:
                app = await self._addon_call("x.admin.appInfo", {}, timeout=15)
                started = app.get("processStartedAt")
                settled = (
                    isinstance(started, (int, float)) and started > barrier["createdAt"] * 1000
                )
            except Exception:
                pass
        if (
            not barrier.get("needsRestart")
            and self._barrier_session is session
            and not session._closed.is_set()
        ):
            outcome = session.late_outcomes.get(barrier.get("requestId"))
            if outcome is not None:
                result = outcome.get("result")
                error = outcome.get("error")
                native_unknown = (
                    isinstance(result, dict)
                    and (
                        result.get("status") == "unknown"
                        or (
                            barrier.get("method") == "x.filters.run"
                            and result.get("completed") is False
                        )
                    )
                ) or getattr(error, "code", None) in {
                    "NATIVE_TIMEOUT",
                    "TIMEOUT",
                    "OUTCOME_UNKNOWN",
                }
                if native_unknown:
                    barrier["needsRestart"] = True
                    journal.set_barrier(barrier)
                else:
                    settled = True
        if (
            not settled
            and not barrier.get("needsRestart")
            and epoch
            and epoch == barrier.get("epoch")
            and barrier.get("operationId")
        ):
            answer = await self._addon_call(
                "operations.receipt",
                {
                    "operationId": barrier["operationId"],
                    "itemId": barrier.get("itemId", 0),
                    "method": barrier["method"],
                },
                timeout=15,
            )
            settled = bool(
                answer.get("found")
                and not answer.get("pending")
                and (answer.get("result") or {}).get("status") != "unknown"
                and (answer.get("error") or {}).get("code")
                not in {"OUTCOME_UNKNOWN", "NATIVE_TIMEOUT", "TIMEOUT"}
            )
        if settled:
            journal.set_barrier(None)
            self._barrier_session = None
            return
        raise BlockedError(
            "A previous write may still be running. Read its operation status; if no receipt is available, install the 1.4.0 add-on and restart Thunderbird explicitly. Reconnecting alone does not finish native work.",
            code="WRITE_IN_FLIGHT",
        )

    async def _guarded_call(
        self, method: str, params: dict, *, timeout=180.0, on_progress=None
    ) -> Any:
        if self._stop.is_set():
            raise BlockedError(
                "Daemon is shutting down; reconnect before writing.", code="DAEMON_STOPPING"
            )
        if getattr(self.operations(), "health_error", None):
            raise BlockedError(
                "Operation history failed; inspect status and repair it before another write.",
                code="JOURNAL_UNHEALTHY",
            )
        await self._clear_settled_barrier()
        session = self.session
        if session is None:
            raise self._not_connected()
        journal = self.operations().journal
        journal.set_barrier(
            {
                "epoch": session.hello.get("runtimeEpoch"),
                "method": method,
                "createdAt": time.time(),
                "requestId": getattr(session, "_next_id", 0) + 1,
                "operationId": params.get("operationId"),
                "itemId": params.get("itemId", 0),
            }
        )
        self._barrier_session = session
        try:
            result = await self._addon_call(
                method, params, timeout=timeout, on_progress=on_progress
            )
        except TransportError as exc:
            # TIMEOUT received as an add-on error may represent a native deadline
            # rather than our pending transport future. In that case a completed
            # handler receipt does not prove that the native mutation stopped.
            if not session.uncertain and not session._closed.is_set():
                barrier = journal.barrier() or {}
                barrier["needsRestart"] = True
                journal.set_barrier(barrier)
            raise TransportError(
                "The write may have completed; it was not repeated. Inspect operation status before doing it again.",
                code="OUTCOME_UNKNOWN",
                **{
                    k: v
                    for k, v in error_payload(exc).items()
                    if k in {"recoveryWindowOpen", "tabId"}
                },
            ) from exc
        except Exception as exc:
            if getattr(exc, "code", None) in {"NATIVE_TIMEOUT", "OUTCOME_UNKNOWN"}:
                barrier = journal.barrier() or {}
                barrier["needsRestart"] = True
                journal.set_barrier(barrier)
                raise TransportError(
                    "Native work may still be running; restart Thunderbird before another write.",
                    code="OUTCOME_UNKNOWN",
                    **{
                        k: v
                        for k, v in error_payload(exc).items()
                        if k in {"recoveryWindowOpen", "tabId"}
                    },
                ) from exc
            # An error response is still a receipt: the handler has finished.
            journal.set_barrier(None)
            self._barrier_session = None
            raise
        native_continues = (
            method == "x.filters.run"
            and isinstance(result, dict)
            and result.get("completed") is False
        )
        if native_continues:
            result = {**result, "status": "unknown", "code": "NATIVE_IN_FLIGHT"}
        if isinstance(result, dict) and result.get("status") == "unknown":
            barrier = journal.barrier() or {}
            barrier["needsRestart"] = True
            journal.set_barrier(barrier)
        else:
            journal.set_barrier(None)
            self._barrier_session = None
        return result

    def _events_page(self, params: dict) -> dict:
        since = params.get("since", 0)
        limit = params.get("limit", 50)
        if (
            not isinstance(since, int)
            or since < 0
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise UsageError("since must be nonnegative and limit must be 1-500.")
        cursor = params.get("cursor")
        if cursor:
            if since:
                raise UsageError("Use cursor or since, not both.")
            try:
                epoch, number = cursor.rsplit(":", 1)
                since = int(number)
                if epoch != self.event_epoch:
                    raise UsageError(
                        "Event cursor belongs to an earlier daemon session; resynchronise the mailbox.",
                        code="EVENT_SESSION_CHANGED",
                    )
                if since < 0:
                    raise ValueError
            except (ValueError, AttributeError):
                raise UsageError("Invalid event cursor.", code="INVALID_CURSOR") from None
        if since > self.event_seq:
            raise UsageError(
                "Event sequence is ahead of this daemon; resynchronise.",
                code="EVENT_SESSION_CHANGED",
            )
        earliest = self.events[0]["seq"] if self.events else self.event_seq + 1
        gap = (
            {"from": since + 1, "to": earliest - 1, "count": earliest - since - 1}
            if since < earliest - 1
            else None
        )
        selected = [e for e in self.events if e["seq"] > since][:limit]
        delivered = selected[-1]["seq"] if selected else since
        resync = bool(gap) or any((e.get("data") or {}).get("resyncRequired") for e in selected)
        return {
            "events": selected,
            "latestSeq": delivered,
            "newestSeq": self.event_seq,
            "earliestAvailableSeq": earliest,
            "nextCursor": f"{self.event_epoch}:{delivered}",
            "hasMore": delivered < self.event_seq,
            "gap": gap,
            "resyncRequired": resync,
        }

    def _not_connected(self) -> NotConnectedError:
        """ "Not connected", carrying the diagnosis when we have one.

        Every tool call that fails this way is someone asking why, so the answer
        travels with the failure rather than waiting for them to run `doctor`.
        """
        return NotConnectedError(describe_handshake(self.handshakes.summary()))

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
            "handshake": self.handshakes.summary(),
            "operationJournal": self.journal_health(),
            # Nothing the daemon logs reaches a terminal — it is spawned detached
            # with its streams on DEVNULL — so every reporter needs the path.
            "logFile": str(ipc.daemon_log_path()),
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
        """Remove what we published — and only what is still ours.

        A superseded daemon runs this on its way out too. Unlinking unconditionally
        deleted the *winner's* pairing file and advertisement, which left the add-on
        holding a token nothing was listening for and `serve` with nothing to find.
        """
        path = self.profile.path / BRIDGE_FILE
        if _bridge_file_pid(path) == os.getpid():
            with contextlib.suppress(OSError):
                path.unlink()
        ipc.DaemonInfo.clear_if_owned(os.getpid())

    async def _watch_idle(self) -> None:
        if self.idle_timeout <= 0:
            return
        while not self._stop.is_set():
            await asyncio.sleep(30.0)
            if (
                self._clients == 0
                and not self._inflight
                and not self.writes_active()
                and (time.time() - self._last_client_at) > self.idle_timeout
            ):
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
                if self.writes_active():
                    log.error(
                        "daemon %d attempted takeover while a write is unresolved; staying alive",
                        winner,
                    )
                    continue
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
            await asyncio.start_server(
                self._serve_control, host="127.0.0.1", port=0, limit=ipc.MAX_LINE
            ) as control,
            self._addon_server() as addon_server,
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


def _bridge_file_pid(path) -> int | None:
    """The pid named by a pairing file, or None if it names nothing we can read."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return int(payload["pid"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


async def run_daemon(
    profile_hint: str | None = None,
    *,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    force: bool = False,
) -> int:
    # --force negotiates retirement; it does not bypass startup serialization.
    # Keep this lock through retirement/publication so two upgrade processes
    # cannot both publish independent write queues for the same profile.
    lock = ipc.DaemonLock.acquire()
    if lock is None:
        existing = await _await_advertisement(timeout=6.0)
        if existing is not None:
            log.info("a daemon is already starting/running (pid %d); nothing to do", existing.pid)
            return 0
        log.error(
            "a live startup lock has no advertisement; finish or stop that startup before retrying"
        )
        return 2

    daemon = None
    try:
        existing = ipc.DaemonInfo.load()
        if existing is not None:
            if not force:
                log.info("a daemon is already running (pid %d); nothing to do", existing.pid)
                return 0
            if not await _retire_existing(existing):
                return 2
        profile = find_profile(profile_hint)
        if profile is None:
            log.error("no Thunderbird profile found; pass --profile with an explicit path")
            return 2
        daemon = Daemon(profile, idle_timeout=idle_timeout, startup_lock=lock)
        await daemon.run()
    except asyncio.CancelledError:
        if daemon:
            daemon._cleanup()
        raise
    finally:
        lock.release()
    return 0


async def _retire_existing(info: ipc.DaemonInfo) -> bool:
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", info.port, limit=ipc.MAX_LINE), timeout=5
        )
        await ipc.write_message(writer, {"t": "auth", "id": 1, "token": info.token})
        reply = await asyncio.wait_for(ipc.read_message(reader), timeout=5)
        if not reply or reply.get("t") != "ready":
            raise RuntimeError("existing daemon did not authenticate")
        await ipc.write_message(writer, {"t": "req", "id": 2, "method": "daemon.status"})
        reply = await asyncio.wait_for(ipc.read_message(reader), timeout=5)
        if not reply or not reply.get("ok"):
            raise RuntimeError("existing daemon status unavailable")
        status = reply.get("result") or {}
        if "operationJournal" not in status:
            # An old owner cannot atomically refuse shutdown after a new write
            # races its status reply, nor track native work after a timeout.
            raise RuntimeError(
                "older daemon cannot confirm safe retirement; finish its work and explicitly restart Thunderbird and the daemon before upgrading"
            )
        await ipc.write_message(writer, {"t": "req", "id": 3, "method": "daemon.shutdown"})
        reply = await asyncio.wait_for(ipc.read_message(reader), timeout=5)
        if not reply or not reply.get("ok"):
            detail = (reply or {}).get("error", {}).get("message", "shutdown not acknowledged")
            raise RuntimeError(detail)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = ipc.DaemonInfo.load()
            if current is None:
                return True
            if current.pid != info.pid:
                raise RuntimeError("another daemon appeared during shutdown")
            await asyncio.sleep(0.05)
        raise RuntimeError("existing daemon has not finished shutting down")
    except Exception as exc:
        log.error("cannot replace daemon %d: %s", info.pid, exc)
        return False
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def _await_advertisement(*, timeout: float) -> ipc.DaemonInfo | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = ipc.DaemonInfo.load()
        if info is not None:
            return info
        await asyncio.sleep(0.25)
    return ipc.DaemonInfo.load()

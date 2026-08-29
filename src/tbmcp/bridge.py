"""Client half of the daemon connection — what the tool layer actually calls.

`Bridge.call()` is the only way tools reach Thunderbird. It hides three things:
starting the daemon if nobody has yet, reconnecting when the daemon was restarted
underneath us, and turning protocol errors back into typed exceptions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

from . import ipc
from .errors import NotConnectedError, TimeoutError_, TransportError, from_wire

log = logging.getLogger("tbmcp.bridge")

SPAWN_WAIT_SECONDS = 12.0
ATTACH_WAIT_SECONDS = 20.0
"""How long a tool call waits for the add-on to dial in before giving up.

The add-on polls for the pairing file every 0.5-2s, so this is generous — but it has
to cover a Thunderbird that is still starting up, where the background page has not
run yet."""
ProgressCb = Callable[[dict[str, Any]], Awaitable[None]]

#: Methods the daemon answers itself. They must never wait for Thunderbird, because
#: they are how a caller finds out whether Thunderbird is there at all.
_DAEMON_LOCAL = frozenset(
    {"daemon.status", "daemon.events", "daemon.waitForThunderbird", "daemon.shutdown"}
)


def _daemon_local(method: str) -> bool:
    return method in _DAEMON_LOCAL


class Bridge:
    def __init__(
        self,
        *,
        profile_hint: str | None = None,
        autostart: bool = True,
        default_timeout: float = 30.0,
    ) -> None:
        self.profile_hint = profile_hint
        self.autostart = autostart
        self.default_timeout = default_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._progress: dict[int, ProgressCb] = {}
        self._next_id = 0
        self._pump: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._attached = False
        self._attach_lock = asyncio.Lock()

    # ------------------------------------------------------------------ connection

    async def _spawn_daemon(self) -> None:
        """Start a detached daemon and wait for it to advertise itself."""
        argv = [sys.executable, "-m", "tbmcp", "daemon"]
        if self.profile_hint:
            argv += ["--profile", self.profile_hint]
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if sys.platform == "win32":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            CREATE_NO_WINDOW = 0x08000000
            kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        log.info("starting the tbmcp daemon")
        try:
            subprocess.Popen(argv, **kwargs)
        except OSError as exc:
            raise TransportError(
                f"could not start the tbmcp daemon: {exc}", code="SPAWN_FAILED"
            ) from exc

        deadline = time.monotonic() + SPAWN_WAIT_SECONDS
        while time.monotonic() < deadline:
            if ipc.DaemonInfo.load() is not None:
                return
            await asyncio.sleep(0.15)
        raise TransportError(
            "the tbmcp daemon did not come up in time; run `tbmcp daemon` in a terminal to see why",
            code="SPAWN_TIMEOUT",
        )

    async def _connect(self) -> None:
        info = ipc.DaemonInfo.load()
        if info is None:
            if not self.autostart:
                raise TransportError(
                    "no tbmcp daemon is running (autostart disabled)", code="NO_DAEMON"
                )
            await self._spawn_daemon()
            info = ipc.DaemonInfo.load()
            if info is None:
                raise TransportError("the daemon vanished right after starting", code="NO_DAEMON")

        # `limit` is the stream buffer, and its default is 64 KiB — a thousandth
        # of the ceiling ipc.MAX_LINE names. Past it `readline()` raises and the
        # connection dies, so any answer over ~64 KB (a wide search, a bulk read)
        # dropped the bridge instead of arriving.
        reader, writer = await asyncio.open_connection("127.0.0.1", info.port, limit=ipc.MAX_LINE)
        self._next_id += 1
        await ipc.write_message(writer, {"t": "auth", "id": self._next_id, "token": info.token})
        reply = await asyncio.wait_for(ipc.read_message(reader), timeout=10.0)
        if not reply or reply.get("t") != "ready":
            writer.close()
            raise TransportError("the daemon rejected our control token", code="UNAUTHORIZED")
        self._reader, self._writer = reader, writer
        self._pump = asyncio.create_task(self._read_loop())

    async def _ensure(self) -> None:
        async with self._lock:
            if self._writer is not None and not self._writer.is_closing():
                return
            await self._teardown()
            await self._connect()

    async def _teardown(self) -> None:
        if self._pump:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump
            self._pump = None
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
                await self._writer.wait_closed()
        self._reader = self._writer = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(
                    TransportError("the daemon connection dropped", code="DISCONNECTED")
                )
        self._pending.clear()
        self._progress.clear()

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                message = await ipc.read_message(self._reader)
                if message is None:
                    break
                request_id = message.get("id")
                kind = message.get("t")
                if kind == "progress":
                    callback = self._progress.get(request_id)
                    if callback:
                        with contextlib.suppress(Exception):
                            await callback(message)
                    continue
                if kind != "res":
                    continue
                future = self._pending.pop(request_id, None)
                self._progress.pop(request_id, None)
                if future is None or future.done():
                    continue
                if message.get("ok"):
                    future.set_result(message.get("result"))
                else:
                    error = message.get("error")
                    future.set_exception(
                        from_wire("call", error if isinstance(error, dict) else {})
                    )
        except (TransportError, OSError) as exc:
            log.debug("control read loop ended: %s", exc)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        TransportError("the daemon connection dropped", code="DISCONNECTED")
                    )
            self._pending.clear()
            self._progress.clear()

    # ------------------------------------------------------------------ public API

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        on_progress: ProgressCb | None = None,
        _retry: bool = True,
    ) -> Any:
        timeout = timeout if timeout is not None else self.default_timeout
        await self._ensure()
        # The add-on dials out, so on a cold start the daemon exists a second or two
        # before Thunderbird is attached. Waiting once, deliberately, is far better
        # than letting the first few tool calls race and fail — which is what a user
        # actually saw: three tools erroring, then everything working.
        if not _daemon_local(method):
            await self._wait_for_attachment()
        assert self._writer is not None
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        if on_progress:
            self._progress[request_id] = on_progress
        frame = {
            "t": "req",
            "id": request_id,
            "method": method,
            "params": params or {},
            # Give the daemon a little less than our own budget so its timeout wins
            # and we get a typed error instead of a bare cancellation.
            "timeout": max(1.0, timeout - 1.0),
            "wantsProgress": on_progress is not None,
        }
        try:
            await ipc.write_message(self._writer, frame)
        except (OSError, ConnectionError) as exc:
            self._pending.pop(request_id, None)
            self._progress.pop(request_id, None)
            if _retry:
                await self._teardown()
                return await self.call(
                    method, params, timeout=timeout, on_progress=on_progress, _retry=False
                )
            raise TransportError(f"could not reach the daemon: {exc}", code="WRITE_FAILED") from exc

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            raise TimeoutError_(method, timeout) from None
        except NotConnectedError:
            # Thunderbird went away mid-session (a restart, or the add-on reloading).
            # Wait for it to come back once, then retry — the alternative is failing a
            # call for something that resolves itself in a second.
            self._attached = False
            if not _retry:
                raise
            self._pending.pop(request_id, None)
            await self._wait_for_attachment()
            return await self.call(
                method, params, timeout=timeout, on_progress=on_progress, _retry=False
            )
        except TransportError as exc:
            # A daemon that exited on idle is normal; reconnect once and retry.
            if _retry and exc.code in ("DISCONNECTED", "NO_DAEMON"):
                await self._teardown()
                return await self.call(
                    method, params, timeout=timeout, on_progress=on_progress, _retry=False
                )
            raise
        finally:
            self._pending.pop(request_id, None)
            self._progress.pop(request_id, None)

    async def _wait_for_attachment(self) -> None:
        """Block until Thunderbird is attached, at most once per connection.

        Cleared again whenever a call comes back "not connected", so a Thunderbird
        that restarts mid-session is waited for afresh rather than failing every call.
        """
        if self._attached:
            return
        async with self._attach_lock:
            if self._attached:
                return
            try:
                await self.call(
                    "daemon.waitForThunderbird",
                    {"timeout": ATTACH_WAIT_SECONDS},
                    timeout=ATTACH_WAIT_SECONDS + 5.0,
                    _retry=False,
                )
            except NotConnectedError:
                # Do not raise from here. The caller's own request will fail with the
                # same message, and doing it there keeps one wait from turning into two
                # for a single tool call.
                log.info("Thunderbird has not attached after %.0fs", ATTACH_WAIT_SECONDS)
                return
            self._attached = True

    async def status(self) -> dict[str, Any]:
        return await self.call("daemon.status", timeout=10.0)

    async def require_thunderbird(self, *, wait: float = 0.0) -> dict[str, Any]:
        """Status, but fail loudly when Thunderbird is not attached."""
        if wait > 0:
            return await self.call(
                "daemon.waitForThunderbird", {"timeout": wait}, timeout=wait + 5.0
            )
        status = await self.status()
        if not status.get("connected"):
            raise NotConnectedError()
        return status

    async def close(self) -> None:
        async with self._lock:
            await self._teardown()


_shared: Bridge | None = None


def shared_bridge() -> Bridge:
    """Process-wide bridge, so every tool reuses one daemon connection."""
    global _shared
    if _shared is None:
        _shared = Bridge(
            profile_hint=os.environ.get("TBMCP_PROFILE"),
            autostart=os.environ.get("TBMCP_NO_AUTOSTART") not in ("1", "true", "yes"),
        )
    return _shared


def set_shared_bridge(bridge: Bridge | None) -> None:
    """Test seam."""
    global _shared
    _shared = bridge

"""Where the daemon advertises itself, and how the two sides frame messages.

Shared by `bridge` (client half) and `daemon` (server half) so the framing can
never drift between them.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import TransportError

PROTOCOL_VERSION = 1
MAX_LINE = 64 * 1024 * 1024  # a raw message body can legitimately be large


def state_dir() -> Path:
    """Per-user directory for the daemon advertisement and lock."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    path = Path(base) / "tbmcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class DaemonInfo:
    """Contents of `<state_dir>/daemon.json` — how `serve` finds the daemon."""

    version: int
    port: int
    token: str
    pid: int
    profile: str

    @classmethod
    def path(cls) -> Path:
        return state_dir() / "daemon.json"

    @classmethod
    def load(cls) -> DaemonInfo | None:
        try:
            raw = json.loads(cls.path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            info = cls(
                version=int(raw["version"]),
                port=int(raw["port"]),
                token=str(raw["token"]),
                pid=int(raw["pid"]),
                profile=str(raw.get("profile", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return info if info.version == PROTOCOL_VERSION and info.is_alive() else None

    def is_alive(self) -> bool:
        """Whether the advertised process still exists."""
        if self.pid <= 0:
            return False
        if sys.platform == "win32":
            # Signal 0 is unavailable; ask the OS for the handle instead.
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, self.pid
            )
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(handle)
            STILL_ACTIVE = 259
            return bool(ok) and exit_code.value == STILL_ACTIVE
        try:
            os.kill(self.pid, 0)
        except (ProcessLookupError, PermissionError) as exc:
            return isinstance(exc, PermissionError)
        return True

    def write(self) -> None:
        path = self.path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)
        _restrict_permissions(path)

    @classmethod
    def clear(cls) -> None:
        try:
            cls.path().unlink()
        except FileNotFoundError:
            pass


def _restrict_permissions(path: Path) -> None:
    """Best-effort: make the token file readable only by this user."""
    if sys.platform == "win32":
        # icacls is the only dependency-free way to drop inherited ACEs.
        import subprocess

        user = os.environ.get("USERNAME")
        if not user:
            return
        for args in (
            ["icacls", str(path), "/inheritance:r"],
            ["icacls", str(path), "/grant:r", f"{user}:(R,W)"],
        ):
            try:
                subprocess.run(
                    args,
                    check=False,
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError:
                return
    else:
        try:
            path.chmod(0o600)
        except OSError:
            pass


class DaemonLock:
    """Atomic "only one daemon" guarantee.

    Checking `DaemonInfo` is not enough: it is written *after* the sockets are bound,
    so two daemons started within the same second both see an empty advertisement and
    both bind. The add-on then attaches to one while `serve` talks to the other, and
    every tool reports "Thunderbird is not connected" with both processes healthy.

    `O_CREAT | O_EXCL` is atomic on every platform we target, so exactly one process
    wins the create. A lock left behind by a crash is detected by checking whether the
    recorded pid is still alive.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def acquire(cls) -> DaemonLock | None:
        path = state_dir() / "daemon.lock"
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if cls._holder_alive(path):
                    return None
                # Stale: the previous holder died without releasing it.
                try:
                    path.unlink()
                except OSError:
                    return None
                continue
            except OSError:
                return None
            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
            finally:
                os.close(fd)
            _restrict_permissions(path)
            return cls(path)
        return None

    @classmethod
    def acquire_forcibly(cls) -> DaemonLock:
        """Take the lock regardless of who holds it.

        Only for the recovery path: a live holder that never advertised is wedged, and
        refusing to start behind it would leave the bridge permanently down.
        """
        path = state_dir() / "daemon.lock"
        path.write_text(str(os.getpid()), encoding="ascii")
        _restrict_permissions(path)
        return cls(path)

    @staticmethod
    def _holder_alive(path: Path) -> bool:
        try:
            pid = int(path.read_text(encoding="ascii").strip() or 0)
        except (OSError, ValueError):
            # An unreadable or half-written lock is not evidence of a live daemon.
            return False
        if pid <= 0:
            return False
        return DaemonInfo(
            version=PROTOCOL_VERSION, port=0, token="", pid=pid, profile=""
        ).is_alive()

    def holder(self) -> int | None:
        try:
            return int(self.path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        """Remove the lock, but only if it is still ours."""
        if self.holder() == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass


def new_token() -> str:
    return secrets.token_urlsafe(32)


# ------------------------------------------------------------------ line framing


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read one newline-delimited JSON object. `None` means the peer hung up.

    Both ends must be opened with `limit=MAX_LINE` (see `open_connection`), or the
    ceiling is asyncio's 64 KiB default rather than the one below, and `readline`
    raises `ValueError` instead of ever reaching the size check.
    """
    try:
        line = await reader.readline()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return None
    except ValueError as exc:
        # The stream buffer filled before a newline arrived. Typed, so a caller
        # sees a size problem rather than a connection that simply died.
        raise TransportError("control message exceeded the maximum size", code="TOO_LARGE") from exc
    if not line:
        return None
    if len(line) > MAX_LINE:
        raise TransportError("control message exceeded the maximum size", code="TOO_LARGE")
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise TransportError(f"malformed control message: {exc}", code="BAD_FRAME") from exc
    if not isinstance(message, dict):
        raise TransportError("control message was not an object", code="BAD_FRAME")
    return message


def encode_message(message: dict[str, Any]) -> bytes:
    # separators keep the wire compact; ensure_ascii=False keeps subjects intact.
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


async def write_message(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    writer.write(encode_message(message))
    await writer.drain()

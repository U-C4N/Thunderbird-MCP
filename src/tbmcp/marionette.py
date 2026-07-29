"""A minimal Marionette client, used only to install the add-on without any UI.

Thunderbird ships Mozilla's Marionette server (`chrome/remote/content/marionette/`
is in `omni.ja` on release builds). Started with `-marionette
-remote-allow-system-access` it accepts chrome-privileged script evaluation on
127.0.0.1:2828, which is enough to drive `AddonManager` and install an unsigned XPI
with no doorhanger and no "Install Add-on From File" click.

`-remote-allow-system-access` is required on current builds; without it
`Marionette:SetContext` fails with *"System access is required"*.

Framing is `<byte-length>:<json>`; commands are `[0, id, name, params]` and
responses `[1, id, error, result]`.

Marionette is a wide-open local automation channel, so nothing here leaves it
enabled: the installer always restarts Thunderbird without the flags afterwards.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Any

from .errors import TbmcpError

HOST = "127.0.0.1"
PORT = 2828


class MarionetteError(TbmcpError):
    kind = "internal"


class Marionette:
    def __init__(self, host: str = HOST, port: int = PORT, timeout: float = 120.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""
        self._id = 0
        self.handshake = self._recv()

    # ------------------------------------------------------------------ framing

    def _recv_raw(self) -> bytes:
        while b":" not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise MarionetteError("Marionette closed the connection")
            self._buf += chunk
        length_str, _, rest = self._buf.partition(b":")
        try:
            length = int(length_str)
        except ValueError as exc:
            raise MarionetteError("Marionette sent a malformed frame") from exc
        self._buf = rest
        while len(self._buf) < length:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise MarionetteError("Marionette closed mid-frame")
            self._buf += chunk
        payload, self._buf = self._buf[:length], self._buf[length:]
        return payload

    def _recv(self) -> Any:
        return json.loads(self._recv_raw())

    def send(self, name: str, params: dict[str, Any] | None = None) -> Any:
        self._id += 1
        body = json.dumps([0, self._id, name, params or {}]).encode("utf-8")
        self.sock.sendall(str(len(body)).encode("ascii") + b":" + body)
        while True:
            message = self._recv()
            if isinstance(message, list) and len(message) == 4 and message[1] == self._id:
                _, _, error, result = message
                if error:
                    detail = error.get("message") if isinstance(error, dict) else str(error)
                    raise MarionetteError(f"{name} failed: {detail}")
                return result

    # ------------------------------------------------------------------- session

    def start_chrome_session(self) -> dict[str, Any]:
        session = self.send("WebDriver:NewSession", {"capabilities": {}})
        try:
            self.send("Marionette:SetContext", {"value": "chrome"})
        except MarionetteError as exc:
            if "System access" in str(exc):
                raise MarionetteError(
                    "Thunderbird was started without -remote-allow-system-access, so "
                    "privileged automation is refused. Restart it with both "
                    "-marionette and -remote-allow-system-access."
                ) from exc
            raise
        return session if isinstance(session, dict) else {}

    def execute(
        self, script: str, args: list[Any] | None = None, *, timeout_ms: int = 90_000
    ) -> Any:
        result = self.send(
            "WebDriver:ExecuteAsyncScript",
            {
                "script": script,
                "args": args or [],
                "newSandbox": False,
                "scriptTimeout": timeout_ms,
            },
        )
        # Marionette wraps results in {"value": ...}.
        if isinstance(result, dict) and set(result.keys()) == {"value"}:
            result = result["value"]
        if isinstance(result, dict) and result.get("fatalError"):
            raise MarionetteError(f"chrome script failed: {result['fatalError']}")
        return result

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def quit_application(self) -> None:
        """Ask Thunderbird to shut down cleanly (flushes prefs and folder caches)."""
        try:
            self.send("Marionette:Quit", {"flags": ["eAttemptQuit"]})
        except MarionetteError:
            pass
        finally:
            self.close()


def wait_for_port(host: str = HOST, port: int = PORT, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def connect(timeout: float = 60.0) -> Marionette:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return Marionette()
        except OSError as exc:
            last = exc
            time.sleep(0.5)
    raise MarionetteError(
        f"could not reach Marionette on {HOST}:{PORT} — is Thunderbird running with "
        f"-marionette -remote-allow-system-access? ({last})"
    )

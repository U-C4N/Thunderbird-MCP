"""Shared fixtures.

Two kinds. The `fake_bridge` half needs no Thunderbird and no daemon: the whole
tool layer funnels through `Bridge.call`, so replacing the shared bridge with a
recorder exercises every tool end to end through a real MCP client — argument
validation, gating, and the shape of what comes back.

The `live_*` half at the bottom does the opposite, and exists because the first
half cannot see the add-on at all.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from tbmcp import bridge as bridge_module
from tbmcp import safety
from tbmcp.config import Settings


@pytest.fixture
def anyio_backend() -> str:
    # The SDK is anyio-based; pinning asyncio keeps the suite off trio.
    return "asyncio"


class FakeBridge:
    """Stands in for the daemon connection.

    `responses` maps a bridge method to either a value or a callable taking the
    params. Anything unmapped returns `default`, which is enough for tools that only
    pass the result through.
    """

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        default: Any = None,
        connected: bool = True,
    ) -> None:
        self.responses = responses or {}
        self.default = {} if default is None else default
        self.connected = connected
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        on_progress: Any = None,
    ) -> Any:
        self.calls.append((method, dict(params or {})))
        if method in self.responses:
            handler = self.responses[method]
            return handler(params or {}) if callable(handler) else handler
        return self.default

    async def status(self) -> dict[str, Any]:
        return {"connected": self.connected, "thunderbird": {"experiment": True}}

    async def require_thunderbird(self, *, wait: float = 0.0) -> dict[str, Any]:
        return await self.status()

    async def close(self) -> None:
        return None

    # --- assertions used by the tests -----------------------------------

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    def params_for(self, method: str) -> dict[str, Any]:
        for name, params in self.calls:
            if name == method:
                return params
        raise AssertionError(f"{method} was never called; saw {self.methods()}")


@pytest.fixture
def fake_bridge():
    """Install a FakeBridge for the duration of a test, then restore the real one."""

    created: list[FakeBridge] = []

    def make(responses: dict[str, Any] | None = None, **kwargs: Any) -> FakeBridge:
        instance = FakeBridge(responses, **kwargs)
        bridge_module.set_shared_bridge(instance)  # type: ignore[arg-type]
        created.append(instance)
        return instance

    yield make
    bridge_module.set_shared_bridge(None)


@pytest.fixture(autouse=True)
def reset_settings():
    """Tool modules read the active policy from module state; keep tests isolated."""
    yield
    safety.set_settings(Settings())


# --------------------------------------------------------------- live Thunderbird
#
# The fixtures above replace the bridge with a recorder, which is what lets the
# suite run anywhere — and also why it never exercises the add-on. Five of the six
# bugs in the search rewrite lived on that side and every one of them passed a
# green run. The tests below drive a real Thunderbird instead, and skip when there
# is not one, so CI stays green while a developer with the bridge up gets the
# coverage the mocked suite cannot give.

_MISSING = object()
_corpus_cache: Any = _MISSING


@pytest.fixture
async def live_bridge():
    """A bridge to a running Thunderbird, or skip.

    `autostart=False` deliberately: spawning a daemon on a machine with no
    Thunderbird would pay the spawn and attach timeouts on every CI run only to
    fail at the end. Nothing advertised means nothing to test against.
    """
    from tbmcp import ipc

    if ipc.DaemonInfo.load() is None:
        pytest.skip("no tbmcp daemon advertised — these need a live Thunderbird")
    bridge = bridge_module.Bridge(autostart=False)
    try:
        status = await bridge.status()
    except Exception as exc:  # any transport failure means "not available"
        await bridge.close()
        pytest.skip(f"daemon unreachable: {exc}")
    if not status.get("connected"):
        await bridge.close()
        pytest.skip("daemon is up but Thunderbird is not attached")
    bridge_module.set_shared_bridge(bridge)  # type: ignore[arg-type]
    try:
        yield bridge
    finally:
        bridge_module.set_shared_bridge(None)
        await bridge.close()


@pytest.fixture
async def live_tools(live_bridge):
    """The read-only toolset, over a real client, against real mail."""
    from mcp import Client

    from tbmcp.server import build_server

    settings = Settings().merged_with(toolsets=("mail", "folders", "search"), read_only=True)
    async with Client(build_server(settings, bridge=live_bridge)) as client:
        yield client


async def _call(client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(tool, args)
    assert not result.is_error, " ".join(getattr(block, "text", "") for block in result.content)
    return result.structured_content


async def _discover(client) -> dict[str, Any] | None:
    """The biggest folder this profile has, and a word its subjects share.

    Nothing here may be hardcoded: the corpus is whatever mail the developer
    running the suite happens to have. A profile too small or too uniform to
    support a check skips it rather than failing it.
    """
    folders = (await _call(client, "folder_list", {"limit": 200}))["items"]
    ranked = sorted(folders, key=lambda f: f.get("totalMessageCount") or 0, reverse=True)
    for folder in ranked:
        if (folder.get("totalMessageCount") or 0) < 40:
            break
        listed = await _call(client, "mail_list", {"folder_id": folder["id"], "limit": 50})
        subjects = [m.get("subject") or "" for m in listed["items"]]
        # Group case-insensitively so "Release" and "release" count together, but
        # keep a spelling that was actually observed: `subject` is a case-sensitive
        # substring match (msgHdr.mime2DecodedSubject.includes), so searching for a
        # lowercased word finds only the messages that spell it that way.
        counts: dict[str, int] = {}
        seen_as: dict[str, str] = {}
        for subject in subjects:
            # One vote per subject: a word repeated inside one subject is not
            # evidence that several messages share it.
            words = {w for w in re.split(r"[^A-Za-z0-9]+", subject) if len(w) >= 4}
            for word in {w.lower() for w in words}:
                counts[word] = counts.get(word, 0) + 1
            for word in words:
                seen_as.setdefault(word.lower(), word)
        shared = [word for word, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n >= 5]
        if shared:
            return {
                "folder_id": folder["id"],
                "account_id": folder["accountId"],
                "term": seen_as[shared[0]],
                "total": folder["totalMessageCount"],
            }
    return None


@pytest.fixture
def call(live_tools):
    """`await call("mail_search", {...})` -> the structured payload, or a failure
    naming the tool that refused."""

    async def invoke(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return await _call(live_tools, tool, args)

    return invoke


@pytest.fixture
async def corpus(live_tools):
    """A folder and a search term this particular profile can exercise."""
    global _corpus_cache
    if _corpus_cache is _MISSING:
        _corpus_cache = await _discover(live_tools)
    if _corpus_cache is None:
        pytest.skip("no folder with 40+ messages sharing a common subject word")
    return _corpus_cache

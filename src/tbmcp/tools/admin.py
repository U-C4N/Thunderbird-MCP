"""Is it working, and if not, why: status, events, diagnostics, lifecycle.

Three of these answer from the local daemon rather than from Thunderbird, so they
still work when Thunderbird is closed. That is the whole point of them — it is the
difference between telling the user to start Thunderbird and leaving a caller to
guess at a timeout.
"""

# NOTE: no `from __future__ import annotations` in toolset modules. Tool signatures
# must evaluate at definition time so `Gate(...)` produces a real
# `Annotated[Consent, Resolve(...)]` rather than a string the SDK has to re-evaluate.

from typing import Any, Literal

from ..errors import TbmcpError, UsageError
from ..handshake import describe_handshake
from ..safety import DESTRUCTIVE, MUTATING, Gate, guard_write, large_output, require
from ..server import Registrar
from ._common import call, changed, clamp, one_of, page

AddonKind = Literal["all", "extension", "theme", "dictionary", "locale"]

NOT_CONNECTED_HINT = (
    "Thunderbird is not attached to the bridge. Ask the user to start Thunderbird; if "
    "it is already running, the add-on is missing or disabled — `tbmcp install-addon` "
    "installs it and `tbmcp doctor` reports on the rest of the chain."
)

NO_EXPERIMENT_HINT = (
    "The privileged half of the add-on did not load, so every capability built on it "
    "(preferences, accounts, filters, calendar, diagnostics) is unavailable. Reinstall "
    "with `tbmcp install-addon`, then read Thunderbird's error console."
)


def register(reg: Registrar) -> None:
    register_operations(reg)
    # ------------------------------------------------------------- daemon-local

    @reg.read_tool(title="Thunderbird connection status")
    async def tb_status() -> dict[str, Any]:
        """Whether Thunderbird is attached, and which halves of the add-on loaded.

        Answered by the local daemon, so it works when Thunderbird is closed. Call it
        first whenever another tool reports that it cannot reach Thunderbird.
        """
        result = await call("daemon.status", timeout=15.0)
        thunderbird = result.get("thunderbird") or {}
        features = (thunderbird.get("capabilities") or {}).get("features") or {}
        expected_features = (
            "operationsV1",
            "draftEditNative",
            "batchUndoV1",
            "snapshotSearchV1",
            "followupsV1",
            "attachmentExportV1",
        )
        payload: dict[str, Any] = {
            "connected": bool(result.get("connected")),
            "privilegedHalf": thunderbird.get("experiment"),
            "app": thunderbird.get("app") or {},
            "addonVersion": thunderbird.get("addonVersion"),
            "inFlightCalls": thunderbird.get("inFlight"),
            "profile": result.get("profile"),
            "daemon": result.get("daemon"),
            # What the add-on's connection attempts did, welcomed or not. "Not
            # connected" and "connecting twice a minute and failing" need opposite
            # advice, and only this tells them apart.
            "handshake": result.get("handshake"),
            "operationJournal": result.get("operationJournal"),
            "capabilities": thunderbird.get("capabilities") or {},
            "runtimeEpoch": thunderbird.get("runtimeEpoch"),
            "missingFeatures": [
                key
                for key in expected_features
                if result.get("connected") and features.get(key, False) is False
            ],
            "unverifiedFeatures": [
                key
                for key in expected_features
                if not result.get("connected") or features.get(key, False) is None
            ],
        }
        if not payload["connected"]:
            payload["hint"] = describe_handshake(result.get("handshake")) or NOT_CONNECTED_HINT
        elif payload["privilegedHalf"] is False:
            payload["hint"] = NO_EXPERIMENT_HINT
        return payload

    @reg.read_tool(title="Wait for Thunderbird")
    async def tb_wait(timeout_seconds: int = 30) -> dict[str, Any]:
        """Block until Thunderbird attaches to the bridge, then report status.

        Use it after `tb_restart`, or after asking the user to start Thunderbird.
        Fails with a message naming what is missing if nothing attaches in time.
        """
        seconds = clamp(
            timeout_seconds, default=30, minimum=1, maximum=600, field="timeout_seconds"
        )
        result = await call(
            "daemon.waitForThunderbird", {"timeout": seconds}, timeout=seconds + 10.0
        )
        thunderbird = result.get("thunderbird") or {}
        return {
            "connected": bool(result.get("connected")),
            "waitedUpToSeconds": seconds,
            "app": thunderbird.get("app") or {},
            "privilegedHalf": thunderbird.get("experiment"),
            "addonVersion": thunderbird.get("addonVersion"),
        }

    @reg.read_tool(title="Recent Thunderbird events")
    async def tb_events(
        since: int = 0, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        """Read buffered Thunderbird notifications: new mail, folder and account changes.

        Continue with `cursor=nextCursor`. `gap` and `resyncRequired` explain lost
        history; an old-session cursor is rejected. Legacy since=latestSeq is
        supported but cannot identify a previous daemon session.
        """
        if cursor and since:
            raise UsageError("Pass cursor or since, not both.")
        result = await call(
            "daemon.events",
            {
                "since": clamp(since, default=0, minimum=0, maximum=2**53, field="since"),
                "limit": clamp(limit, default=50, minimum=1, maximum=500, field="limit"),
                "cursor": cursor,
            },
            timeout=15.0,
        )
        return page(
            result.get("events") or [],
            **{k: v for k, v in result.items() if k != "events"},
            hint="Pass cursor=nextCursor to continue without skipping events.",
        )

    # ----------------------------------------------------------------- inside TB

    @reg.read_tool(title="Thunderbird diagnostics", meta=large_output())
    async def tb_diagnostics() -> dict[str, Any]:
        """One report: versions, profile, which capabilities loaded, accounts, indexing.

        The first thing to fetch when anything behaves oddly. It includes whether this
        build permits unsigned add-ons and experiment APIs, which is what explains a
        half-installed bridge, and the message store type per account.
        """
        # A diagnostics tool that refuses to answer while the thing it diagnoses is
        # down would be useless exactly when it is wanted, so degrade in two steps.
        status = await call("daemon.status", timeout=15.0)
        payload: dict[str, Any] = {
            "connected": bool(status.get("connected")),
            "daemon": status.get("daemon"),
            "profile": status.get("profile"),
            "thunderbird": status.get("thunderbird"),
            "handshake": status.get("handshake"),
        }
        if not payload["connected"]:
            payload["hint"] = describe_handshake(status.get("handshake")) or NOT_CONNECTED_HINT
            return payload
        try:
            payload.update(await call("x.admin.diagnostics", timeout=60.0))
        except TbmcpError as exc:
            payload["privilegedHalfError"] = str(exc)
            payload["hint"] = NO_EXPERIMENT_HINT
        return payload

    @reg.read_tool(title="Thunderbird error console", meta=large_output())
    async def tb_console(contains: str | None = None, limit: int = 100) -> dict[str, Any]:
        """Recent lines from Thunderbird's error console, newest last.

        Narrow it with `contains` — `tbmcp` shows this bridge's own complaints, and an
        add-on id or a source filename shows someone else's. Anything shaped like a
        password or token is redacted inside Thunderbird before it is sent.

        Lines the bridge writes with `console.*` (`source: "console"`) are included
        alongside the error console's own entries, merged by time.
        """
        result = await call(
            "x.admin.consoleMessages",
            {
                "filter": contains,
                "limit": clamp(limit, default=100, minimum=1, maximum=500, field="limit"),
            },
            timeout=30.0,
        )
        return page(
            result.get("messages") or [],
            total=result.get("matched"),
            filter=contains,
            buffered=result.get("buffered"),
        )

    @reg.read_tool(title="Installed add-ons")
    async def tb_addons(kind: AddonKind = "all") -> dict[str, Any]:
        """List installed add-ons with their enabled and signature state.

        `isBridge` marks this server's own add-on. A `signedState` of 0 is expected
        for it: the bridge is installed unsigned, which this Thunderbird permits.
        """
        wanted = one_of(
            kind,
            ("all", "extension", "theme", "dictionary", "locale"),
            field="kind",
            default="all",
        )
        result = await call(
            "x.admin.addons",
            {"type": None if wanted == "all" else wanted},
            timeout=30.0,
        )
        return page(
            result.get("addons") or [],
            total=result.get("installed"),
            kind=wanted,
        )

    @reg.write_tool(title="Restart Thunderbird", annotations=DESTRUCTIVE)
    async def tb_restart(
        confirm: bool = False,
        consent: Gate("restart Thunderbird") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Restart Thunderbird. Every call in flight fails, including other clients'.

        The bridge connection drops, so this returns before the restart happens and
        the result says nothing about whether it succeeded — wait with `tb_wait`
        afterwards. Unsent compose windows and unsaved drafts are lost, so ask the
        user before you do it; a stuck sync usually does not need it.
        """
        guard_write("restart Thunderbird")
        require(consent, "restart Thunderbird")
        result = await call("x.admin.restart", {}, timeout=30.0)
        return changed(
            "thunderbird",
            before={"running": True},
            after={"restarting": True, "inMs": (result or {}).get("inMs")},
            note=(result or {}).get("note"),
            hint="Call tb_wait to block until Thunderbird is back on the bridge.",
        )


def register_operations(reg: Registrar) -> None:
    """Also registered for compose/mail-only clients, without duplicate tools."""
    if "tb_operation_status" in reg.registered:
        return

    @reg.read_tool(title="Inspect an operation")
    async def tb_operation_status(
        operation_id: str, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Inspect a queued, completed or uncertain operation without repeating it."""
        return await call(
            "daemon.operations.status",
            {
                "operationId": operation_id,
                "cursor": cursor,
                "limit": clamp(limit, default=50, minimum=1, maximum=500, field="limit"),
            },
        )

    @reg.read_tool(title="Recent operation history")
    async def tb_operation_list(cursor: str | None = None, limit: int = 20) -> dict[str, Any]:
        """List this profile's local, body-free operation history for the last seven days."""
        return await call(
            "daemon.operations.list",
            {
                "cursor": cursor,
                "limit": clamp(limit, default=20, minimum=1, maximum=100, field="limit"),
            },
        )

    @reg.write_tool(title="Stop remaining operation steps", annotations=MUTATING, interactive=False)
    async def tb_operation_cancel(operation_id: str) -> dict[str, Any]:
        """Stop steps not yet started. A send already dispatched cannot be recalled."""
        guard_write("cancel operations")
        return await call("daemon.operations.cancel", {"operationId": operation_id})

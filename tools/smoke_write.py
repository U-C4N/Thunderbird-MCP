#!/usr/bin/env python3
"""Live write-path smoke test.

    python tools/smoke_write.py

Proves the gated write path end to end without touching anything the user cares
about: it exercises the confirmation gate, then round-trips a preference under
`extensions.tbmcp.` (a branch that exists only for this) and resets it. It also
checks that a search runs and that the denylist really refuses.

Nothing is sent, deleted or moved, and no mail content is printed.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from mcp import Client

from tbmcp.config import ALL_TOOLSETS, Settings
from tbmcp.server import build_server

PREF = "extensions.tbmcp.smoketest"
results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    print(f"  {'ok   ' if ok else 'FAIL '} {label:<44} {detail[:90]}")


def text(result) -> str:
    return " ".join(getattr(b, "text", "") for b in result.content)


async def main() -> int:
    mcp = build_server(Settings().merged_with(toolsets=ALL_TOOLSETS))
    async with Client(mcp) as client:
        # 1. The gate refuses without confirmation, and says how to proceed.
        got = await client.call_tool("pref_set", {"name": PREF, "type": "string", "value": "x"})
        check(
            got.is_error and "confirm=true" in text(got),
            "pref_set refuses without confirmation",
            text(got),
        )

        # 2. A dry run previews without asking for approval and without writing.
        got = await client.call_tool(
            "pref_set",
            {"name": PREF, "type": "string", "value": "x", "dry_run_only": True},
        )
        check(
            not got.is_error and got.structured_content.get("dryRun") is True,
            "pref_set dry run needs no approval",
            text(got),
        )
        got = await client.call_tool("pref_get", {"name": PREF})
        check(
            not got.is_error and got.structured_content.get("exists") is False,
            "dry run wrote nothing",
            str(got.structured_content),
        )

        # 3. The real write, with confirmation.
        got = await client.call_tool(
            "pref_set",
            {"name": PREF, "type": "string", "value": "hello-from-tbmcp", "confirm": True},
        )
        check(not got.is_error, "pref_set writes with confirm=true", text(got))
        payload = got.structured_content or {}
        check(
            (payload.get("current") or {}).get("value") == "hello-from-tbmcp",
            "pref_set reports the new value",
            str(payload.get("current")),
        )

        got = await client.call_tool("pref_get", {"name": PREF})
        check(
            (got.structured_content or {}).get("value") == "hello-from-tbmcp",
            "pref_get reads it back from Thunderbird",
            str(got.structured_content),
        )

        # 4. Reset, leaving the profile as we found it.
        got = await client.call_tool("pref_reset", {"name": PREF, "confirm": True})
        check(not got.is_error, "pref_reset removes it", text(got))
        got = await client.call_tool("pref_get", {"name": PREF})
        check(
            (got.structured_content or {}).get("exists") is False,
            "the preference is gone again",
            str(got.structured_content),
        )

        # 5. The denylist must refuse regardless of confirmation.
        got = await client.call_tool(
            "pref_set",
            {"name": "network.proxy.http", "type": "string", "value": "evil", "confirm": True},
        )
        check(got.is_error, "denylisted preference is refused even with confirm", text(got))

        # 6. Read paths that need the mail store, reported as counts only.
        got = await client.call_tool("mail_search", {"unread": True, "limit": 5})
        check(
            not got.is_error,
            "mail_search runs against the live store",
            f"{(got.structured_content or {}).get('count')} unread found",
        )
        got = await client.call_tool("folder_get_unified", {"folder_type": "inbox"})
        check(not got.is_error, "folder_get_unified resolves the unified inbox", text(got))

        # 7. A destructive tool must still refuse, even on an empty id list.
        got = await client.call_tool("mail_delete", {"message_ids": [1]})
        check(
            got.is_error and "confirm=true" in text(got),
            "mail_delete stays gated",
            text(got),
        )

    failed = [label for ok, label in results if not ok]
    print()
    if failed:
        print(f"{len(failed)} of {len(results)} checks failed: {'; '.join(failed)}")
        return 1
    print(f"all {len(results)} write-path checks passed; the profile is unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

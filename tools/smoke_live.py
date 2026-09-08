#!/usr/bin/env python3
"""Live smoke test against a running Thunderbird.

    python tools/smoke_live.py

Read-only by design: it never sends, deletes, moves or changes a setting. It walks
the toolsets that need the privileged half as well as the ones that do not, so a
partial install shows up as a specific failure rather than a vague one.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from mcp import Client

from tbmcp.config import ALL_TOOLSETS, Settings
from tbmcp.server import build_server

#: (tool, arguments, what to print from the result)
CHECKS: list[tuple[str, dict, str]] = [
    ("tb_status", {}, "connected"),
    ("tb_diagnostics", {}, "app"),
    ("account_list", {}, "count"),
    ("folder_list", {}, "count"),
    ("mail_tags", {}, "count"),
    ("search_index_status", {}, "enabled"),
    ("pref_get", {"name": "mail.pane_config.dynamic"}, "value"),
    ("pref_list", {"prefix": "mailnews.tags.", "only_user_set": True}, "count"),
    ("identity_list", {}, "count"),
    ("smtp_list", {}, "count"),
    ("filter_list", {}, "count"),
    ("calendar_list", {}, "count"),
    ("contact_list", {}, "count"),
    ("search_saved_list", {}, "count"),
    ("openpgp_list_keys", {}, "count"),
    ("junk_get", {}, "level"),
    ("settings_describe", {}, "count"),
]


def _pick(payload, key: str):
    if not isinstance(payload, dict):
        return payload
    if key in payload:
        return payload[key]
    for candidate in ("count", "items", "value", "connected"):
        if candidate in payload:
            value = payload[candidate]
            return len(value) if isinstance(value, list) else value
    return list(payload)[:6]


async def main() -> int:
    settings = Settings().merged_with(toolsets=ALL_TOOLSETS)
    mcp = build_server(settings)

    failures: list[str] = []
    async with Client(mcp) as client:
        available = {tool.name for tool in (await client.list_tools()).tools}
        print(f"{len(available)} tools registered\n")
        for name, args, key in CHECKS:
            if name not in available:
                print(f"  SKIP  {name:<22} (not registered)")
                continue
            try:
                result = await client.call_tool(name, args)
            except Exception as exc:
                print(f"  ERROR {name:<22} {exc}")
                failures.append(name)
                continue
            if result.is_error:
                text = " ".join(getattr(b, "text", "") for b in result.content)
                print(f"  FAIL  {name:<22} {text[:150]}")
                failures.append(name)
                continue
            summary = _pick(result.structured_content, key)
            print(f"  ok    {name:<22} {key}={json.dumps(summary, default=str)[:110]}")

    print()
    if failures:
        print(f"{len(failures)} of {len(CHECKS)} checks failed: {', '.join(failures)}")
        return 1
    print("every read-only check passed against the live Thunderbird")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

#!/usr/bin/env python3
"""Generate docs/TOOL-REFERENCE.md and the README's tool catalogue from the code.

    python tools/gen_tool_reference.py

A hand-maintained list of 112 tools drifts within a week. This reads the registered
tools straight from the server, so the documentation cannot disagree with what a
client will actually see.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from tbmcp.config import ALL_TOOLSETS, DEFAULT_TOOLSETS, Settings
from tbmcp.server import build_server

ROOT = pathlib.Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "docs" / "TOOL-REFERENCE.md"
README = ROOT / "README.md"

BEGIN = "<!-- BEGIN GENERATED TOOL CATALOGUE -->"
END = "<!-- END GENERATED TOOL CATALOGUE -->"

BLURBS = {
    "mail": "Search, read, triage and file messages, plus attachments and tags.",
    "folders": "The folder tree, its counts, and creating/renaming/emptying folders.",
    "compose": "Sending, replying, forwarding, drafts and templates.",
    "search": "Ranked whole-corpus search, conversations, and saved searches.",
    "contacts": "Address books, contacts and mailing lists.",
    "calendar": "Calendars, events and tasks, including recurring series.",
    "filters": "Thunderbird's message filters, including running them on demand.",
    "accounts": "Incoming servers, identities, signatures, SMTP servers, per-account junk.",
    "settings": "Preferences, junk training and OpenPGP key listing.",
    "admin": "Connection status, events, diagnostics and the error console.",
}


async def collect() -> dict[str, list[dict]]:
    """Tools per toolset, with the shape a client sees."""
    per_toolset: dict[str, list[dict]] = {}
    for name in ALL_TOOLSETS:
        mcp = build_server(Settings().merged_with(toolsets=(name,)))
        rows = []
        for tool in await mcp.list_tools():
            schema = tool.input_schema or {}
            properties = schema.get("properties") or {}
            required = set(schema.get("required") or [])
            annotations = tool.annotations
            rows.append(
                {
                    "name": tool.name,
                    "title": getattr(tool, "title", None) or "",
                    # First sentence only: the rest is guidance for the model.
                    "summary": (tool.description or "").strip().split("\n")[0],
                    "description": (tool.description or "").strip(),
                    "read_only": bool(annotations and annotations.read_only_hint),
                    "destructive": bool(annotations and annotations.destructive_hint),
                    "gated": "confirm" in properties,
                    "params": [
                        {
                            "name": key,
                            "required": key in required,
                            "type": value.get("type") or ("enum" if value.get("enum") else "any"),
                        }
                        for key, value in properties.items()
                    ],
                }
            )
        per_toolset[name] = rows
    return per_toolset


def marker(row: dict) -> str:
    if row["read_only"]:
        return "read"
    if row["destructive"]:
        return "**destructive**"
    return "write"


def render_reference(per_toolset: dict[str, list[dict]]) -> str:
    total = sum(len(rows) for rows in per_toolset.values())
    out: list[str] = [
        "# Tool reference",
        "",
        f"All {total} tools, generated from the code by `tools/gen_tool_reference.py`.",
        "Do not edit by hand.",
        "",
        "`read` tools cannot change anything and are the only ones registered under",
        "`--read-only`. `write` and `destructive` tools require confirmation — an explicit",
        "`confirm=true`, or an approval prompt where the client supports one.",
        "",
    ]
    for name, rows in per_toolset.items():
        default = " *(in the default toolset)*" if name in DEFAULT_TOOLSETS else ""
        out += [
            f"## `{name}` — {len(rows)} tools{default}",
            "",
            BLURBS.get(name, ""),
            "",
        ]
        for row in rows:
            params = ", ".join(
                f"**{p['name']}**" if p["required"] else p["name"] for p in row["params"]
            )
            out += [
                f"### `{row['name']}` · {marker(row)}",
                "",
                row["description"],
                "",
                f"Parameters: {params or '*none*'}"
                + (
                    "  \n*(bold means required; `confirm` is the confirmation gate)*"
                    if row["params"]
                    else ""
                ),
                "",
            ]
    return "\n".join(out).rstrip() + "\n"


def render_readme_catalogue(per_toolset: dict[str, list[dict]]) -> str:
    total = sum(len(rows) for rows in per_toolset.values())
    read_only = sum(1 for rows in per_toolset.values() for r in rows if r["read_only"])
    out: list[str] = [
        BEGIN,
        f"{total} tools across {len(per_toolset)} toolsets; {read_only} of them read-only.",
        "Full signatures in [docs/TOOL-REFERENCE.md](docs/TOOL-REFERENCE.md).",
        "",
    ]
    for name, rows in per_toolset.items():
        default = " · on by default" if name in DEFAULT_TOOLSETS else ""
        out += [
            "<details>",
            f"<summary><b><code>{name}</code></b> — {len(rows)} tools{default}"
            f" — {BLURBS.get(name, '')}</summary>",
            "",
            "| Tool | | What it does |",
            "| --- | --- | --- |",
        ]
        for row in rows:
            summary = row["summary"].rstrip(".")
            out.append(f"| `{row['name']}` | {marker(row)} | {summary} |")
        out += ["", "</details>", ""]
    out.append(END)
    return "\n".join(out)


def splice_readme(block: str) -> bool:
    text = README.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        print(f"README has no {BEGIN} / {END} markers; skipping")
        return False
    updated = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), block, text, flags=re.DOTALL)
    if updated == text:
        return False
    README.write_text(updated, encoding="utf-8")
    return True


def main() -> int:
    per_toolset = asyncio.run(collect())
    REFERENCE.parent.mkdir(parents=True, exist_ok=True)
    REFERENCE.write_text(render_reference(per_toolset), encoding="utf-8")
    total = sum(len(rows) for rows in per_toolset.values())
    print(f"wrote {REFERENCE.relative_to(ROOT)} ({total} tools)")
    if splice_readme(render_readme_catalogue(per_toolset)):
        print("updated the README catalogue")
    else:
        print("README catalogue unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

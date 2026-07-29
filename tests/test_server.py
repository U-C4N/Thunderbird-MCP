"""Invariants that hold for every tool, whatever toolset it came from.

These are the checks that catch a whole class of client-side breakage which is
otherwise only visible once a real client refuses to load the server.
"""

from __future__ import annotations

import pytest

from tbmcp.config import ALL_TOOLSETS, DEFAULT_TOOLSETS, Settings, parse_toolsets
from tbmcp.server import build_server

pytestmark = pytest.mark.anyio


def _all_tools(**overrides):
    settings = Settings().merged_with(toolsets=ALL_TOOLSETS, **overrides)
    return build_server(settings)


@pytest.mark.parametrize("toolset", ALL_TOOLSETS)
async def test_each_toolset_registers(toolset: str) -> None:
    tools = await build_server(Settings().merged_with(toolsets=(toolset,))).list_tools()
    assert tools, f"{toolset} registered no tools"


async def test_tool_names_are_unique_and_prefixed() -> None:
    tools = await _all_tools().list_tools()
    names = [tool.name for tool in tools]
    assert len(names) == len(set(names)), "duplicate tool names"
    for name in names:
        assert "_" in name, f"{name} is not <domain>_<verb>"
        assert name == name.lower(), f"{name} should be lower case"


async def test_tools_list_order_is_deterministic() -> None:
    # The spec asks servers to be stable here so clients and prompt caches can
    # cache the list; registration order is what guarantees it.
    first = [tool.name for tool in await _all_tools().list_tools()]
    second = [tool.name for tool in await _all_tools().list_tools()]
    assert first == second


async def test_read_only_registers_nothing_that_writes() -> None:
    tools = await _all_tools(read_only=True).list_tools()
    assert tools
    for tool in tools:
        annotations = tool.annotations
        assert annotations is not None, f"{tool.name} has no annotations"
        assert annotations.read_only_hint, f"{tool.name} is not read-only"
        assert not annotations.destructive_hint, f"{tool.name} claims to be destructive"
        properties = (tool.input_schema.get("properties") or {}).keys()
        assert "confirm" not in properties, f"{tool.name} is gated yet registered read-only"


async def test_read_only_is_a_strict_subset() -> None:
    everything = {tool.name for tool in await _all_tools().list_tools()}
    safe = {tool.name for tool in await _all_tools(read_only=True).list_tools()}
    assert safe < everything


async def test_gated_tools_expose_confirm_and_hide_consent() -> None:
    """The consent parameter is resolved server-side and must never reach the model.

    If it leaked into the schema a model could simply pass approve=true, which would
    defeat the whole gate.
    """
    tools = await _all_tools().list_tools()
    gated = []
    for tool in tools:
        properties = (tool.input_schema.get("properties") or {}).keys()
        assert "consent" not in properties, f"{tool.name} leaks its consent parameter"
        if "confirm" in properties:
            gated.append(tool.name)
    assert len(gated) > 30, "expected most mutating tools to be gated"


async def test_no_root_level_schema_combinators() -> None:
    """Claude Code rejects (older versions skip) tools with a root anyOf/oneOf/allOf."""
    for tool in await _all_tools().list_tools():
        for combinator in ("anyOf", "oneOf", "allOf"):
            assert combinator not in tool.input_schema, f"{tool.name} has a root {combinator}"


async def test_every_tool_has_a_description_and_title() -> None:
    for tool in await _all_tools().list_tools():
        assert tool.description, f"{tool.name} has no description"
        # Hosts truncate at 2 KB; anything longer is silently cut mid-sentence.
        assert len(tool.description) < 2048, f"{tool.name} description is too long"
        assert getattr(tool, "title", None), f"{tool.name} has no title"


async def test_descriptions_do_not_depend_on_the_interpreter() -> None:
    """CPython 3.13 strips docstring indentation at compile time; 3.11 does not.

    Left alone, that shipped indented descriptions to clients on 3.11 — wasted tokens
    on every request, and the generated docs differed by interpreter. The server
    normalises them itself, so `cleandoc` must be a no-op here on every version.
    """
    import inspect

    for tool in await _all_tools().list_tools():
        description = tool.description or ""
        assert description == inspect.cleandoc(description), (
            f"{tool.name} description is not normalised"
        )
        assert not description.startswith((" ", "\t")), f"{tool.name} description is indented"


async def test_mutating_tools_ask_the_host_to_prompt() -> None:
    """Claude Code honours this even under bypassPermissions; Codex ignores it, which
    is why the confirm parameter exists as well."""
    for tool in await _all_tools().list_tools():
        properties = (tool.input_schema.get("properties") or {}).keys()
        if "confirm" not in properties:
            continue
        meta = getattr(tool, "meta", None) or {}
        assert meta.get("anthropic/requiresUserInteraction") is True, (
            f"{tool.name} is gated but does not ask the host to prompt"
        )


def test_default_toolset_is_a_lean_subset() -> None:
    assert set(DEFAULT_TOOLSETS) < set(ALL_TOOLSETS)
    assert "settings" not in DEFAULT_TOOLSETS


def test_parse_toolsets() -> None:
    assert parse_toolsets(None) == DEFAULT_TOOLSETS
    assert parse_toolsets("all") == ALL_TOOLSETS
    assert parse_toolsets("mail,settings") == ("mail", "settings")
    # A leading + extends the default set rather than replacing it.
    assert set(parse_toolsets("+settings")) == set(DEFAULT_TOOLSETS) | {"settings"}
    # Order always follows ALL_TOOLSETS, whatever order the caller asked in.
    assert parse_toolsets("settings,mail") == ("mail", "settings")
    with pytest.raises(SystemExit):
        parse_toolsets("nonsense")

"""`--tools`: registering individual tools on top of a toolset selection.

The motivating case is drafting. `mail_draft_save` produces something reviewable and
deletable that never leaves the machine, but it lives in `compose` beside three tools
that do put mail on the wire, and toolsets are all or nothing — so wanting a draft
meant registering `mail_send` too, and a read-only server could not have either.

Naming a tool is the way out, and it has to carry a read-only exemption: a tool that
is registered and then always refuses is worse than one that was never there, because
the model tries it first and reports the failure as Thunderbird's.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp import Client

from tbmcp import clients, safety
from tbmcp.config import Settings, parse_tools
from tbmcp.errors import BlockedError
from tbmcp.safety import guard_write
from tbmcp.server import build_server

pytestmark = pytest.mark.anyio

#: The posture this feature exists to make possible: read everything, draft, send
#: nothing.
READING = ("mail", "folders", "search")
DRAFTING = ("mail_draft_save", "mail_compose_open", "mail_send_status")
OUTBOUND = ("mail_send", "mail_reply", "mail_forward")


def _settings(**overrides) -> Settings:
    return Settings().merged_with(**{"toolsets": READING, "read_only": True, **overrides})


async def _names(settings: Settings) -> set[str]:
    return {tool.name for tool in await build_server(settings).list_tools()}


def _text(result) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


# ----------------------------------------------------------------------- parsing


def test_parse_tools_strips_and_dedupes_in_order() -> None:
    assert parse_tools(" a , b ,, a ,c ") == ("a", "b", "c")
    assert parse_tools(None) == ()
    assert parse_tools("") == ()


# ------------------------------------------------------------------ registration


async def test_drafting_can_be_enabled_without_sending() -> None:
    """The headline case, stated as the property that matters: everything that puts
    mail on the wire stays absent. A model cannot call what was never advertised,
    which is a stronger guarantee than a confirmation gate it fills in itself."""
    names = await _names(_settings(extra_tools=DRAFTING))
    assert set(DRAFTING) <= names
    for outbound in OUTBOUND:
        assert outbound not in names, f"{outbound} registered; drafting must not imply sending"


async def test_naming_a_tool_does_not_drag_in_its_neighbours() -> None:
    """`compose` has to be imported to reach `mail_draft_save`, and importing a
    toolset module runs the whole of its `register()`. Everything it defines that
    nobody asked for has to be dropped on the way past."""
    base = await _names(_settings())
    named = await _names(_settings(extra_tools=("mail_draft_save",)))
    assert named - base == {"mail_draft_save"}


async def test_a_named_tool_can_come_from_an_unselected_toolset() -> None:
    names = await _names(_settings(toolsets=("mail",), extra_tools=("contact_search",)))
    assert "contact_search" in names
    assert "contact_delete" not in names


async def test_an_unknown_name_is_refused_at_startup() -> None:
    """Silence would be the worst outcome: the server starts, the tool is simply
    missing, and the model reports that Thunderbird cannot do the thing."""
    with pytest.raises(SystemExit) as caught:
        build_server(_settings(extra_tools=("mail_draft_sav",)))
    assert "mail_draft_sav" in str(caught.value)


async def test_a_named_write_tool_is_still_advertised_as_writing() -> None:
    """`test_read_only_registers_nothing_that_writes` is the rule and this is its
    one documented exception. The exemption changes what gets registered, never what
    the client is told about it — a host that prompts on `destructive_hint` must go
    on seeing the truth."""
    server = build_server(_settings(extra_tools=("mail_draft_save",)))
    tool = next(t for t in await server.list_tools() if t.name == "mail_draft_save")
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is False
    # Saving a draft is a local write and nothing more; it must not be dressed up as
    # one, or a host will prompt for it as though mail were leaving.
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.open_world_hint is False


# ---------------------------------------------------------------- the exemption


async def test_a_named_write_tool_actually_writes_under_read_only(fake_bridge) -> None:
    """Registering it is only half the job. `guard_write` runs inside the tool body
    and would otherwise refuse the very call it was just registered to allow."""
    bridge = fake_bridge({"compose.save": {"id": 7, "folderId": "account1://Drafts"}})
    server = build_server(_settings(extra_tools=("mail_draft_save",)), bridge=bridge)
    async with Client(server) as client:
        result = await client.call_tool(
            "mail_draft_save", {"subject": "Re: invoice", "body": "Thanks — paid today."}
        )
    assert not result.is_error, _text(result)
    assert bridge.methods() == ["compose.save"]


async def test_the_exemption_is_scoped_to_the_call_that_owns_it() -> None:
    """A ContextVar rather than a flag on the settings, and this is why: tool calls
    share one process and interleave at every await, so a global toggle would hand
    one call's exemption to whatever else happened to be in flight."""
    safety.set_settings(Settings(read_only=True))
    entered, release = asyncio.Event(), asyncio.Event()

    @safety.exempt_write
    async def save_a_draft() -> str:
        guard_write("save drafts")
        entered.set()
        await release.wait()
        guard_write("save drafts")  # still permitted on the far side of the await
        return "saved"

    task = asyncio.create_task(save_a_draft())
    await entered.wait()

    with pytest.raises(BlockedError) as caught:
        guard_write("delete messages")  # a different call, concurrent with the draft
    assert caught.value.code == "READ_ONLY"

    release.set()
    assert await task == "saved"

    # And it is gone again once the call that owned it has finished.
    with pytest.raises(BlockedError):
        guard_write("save drafts")


async def test_the_refusal_names_the_flag_that_would_allow_one_tool() -> None:
    """A model told only "restart without --read-only" will propose lifting the whole
    policy. The narrower fix is the one worth naming."""
    safety.set_settings(Settings(read_only=True))
    with pytest.raises(BlockedError) as caught:
        guard_write("save drafts")
    assert "--tools" in caught.value.needs[0]


# ----------------------------------------------------------------- config round trip


def test_named_tools_survive_into_a_client_config() -> None:
    """`tbmcp setup` regenerates the client config from Settings. A name dropped here
    is a server that silently loses its drafting tools the next time it is
    registered — and nothing about the resulting config looks wrong."""
    _, args = clients.server_command(_settings(extra_tools=DRAFTING))
    assert "--tools" in args, args
    assert args[args.index("--tools") + 1] == ",".join(DRAFTING)
    assert "--read-only" in args


def test_a_plain_config_gains_no_tools_flag() -> None:
    _, args = clients.server_command(Settings())
    assert "--tools" not in args


# ------------------------------------------------------------------------ CLI wiring


@pytest.mark.parametrize("subcommand", ["serve", "doctor", "setup", "tools", "bootstrap"])
def test_tools_is_a_real_flag_wherever_toolsets_is(subcommand: str) -> None:
    """argparse accepts any unambiguous prefix, and `--tools` is a prefix of
    `--toolsets`. A subparser that has one and not the other therefore does not
    reject `--tools mail_draft_save` — it silently reads it as a toolset named
    `mail_draft_save`, which fails later in a different command, or on a dry run gets
    printed back as the command to rerun. Nothing about the failure points at the
    flag that caused it.
    """
    from tbmcp.cli import build_parser

    args = build_parser().parse_args([subcommand, "--tools", "mail_draft_save"])
    assert getattr(args, "tools", None) == "mail_draft_save"
    assert getattr(args, "toolsets", None) is None, "swallowed as an abbreviation"

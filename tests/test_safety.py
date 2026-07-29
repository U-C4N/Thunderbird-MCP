"""The gating layer, tested at the level a client actually hits it."""

from __future__ import annotations

import pytest

from tbmcp import safety
from tbmcp.config import Settings
from tbmcp.errors import BlockedError
from tbmcp.safety import Consent, consent_for, guard_write, require

pytestmark = pytest.mark.anyio


async def test_explicit_confirm_satisfies_the_gate() -> None:
    resolver = consent_for("delete these messages")
    outcome = await resolver(confirm=True, ctx=None)
    assert isinstance(outcome, Consent) and outcome.approve


async def test_yolo_skips_the_gate() -> None:
    safety.set_settings(Settings(yolo=True))
    outcome = await consent_for("delete these messages")(confirm=False, ctx=None)
    assert outcome.approve


async def test_without_confirm_or_elicitation_it_reports_no_channel() -> None:
    """Codex has no elicitation channel, so this branch is the one it always takes.

    The resolver must not raise: it runs before the tool body, and raising would also
    block a dry-run preview. `require()` is what refuses.
    """
    outcome = await consent_for("delete these messages")(confirm=False, ctx=None)
    assert outcome.approve is False
    assert outcome.note == safety.NO_CHANNEL

    with pytest.raises(BlockedError) as caught:
        require(outcome, "delete these messages")
    message = str(caught.value)
    assert "confirm=true" in message
    assert "delete these messages" in message


async def test_a_declined_prompt_is_not_presented_as_workaroundable() -> None:
    """When the user actually said no, the model must not be told how to override."""
    with pytest.raises(BlockedError) as caught:
        require(Consent(approve=False), "delete these messages")
    message = str(caught.value)
    assert "declined" in message.lower()
    assert "confirm=true" not in message


async def test_elicitation_capable_client_is_asked() -> None:
    class Capabilities:
        elicitation = {"form": True}

    class Ctx:
        client_capabilities = Capabilities()

    outcome = await consent_for("send this message")(confirm=False, ctx=Ctx())
    # An Elicit marker, not a Consent: the framework turns it into a real prompt.
    assert type(outcome).__name__ == "Elicit"
    assert "send this message" in outcome.message


async def test_capability_probe_failure_does_not_break_the_call() -> None:
    class Ctx:
        @property
        def client_capabilities(self):
            raise RuntimeError("no session")

    outcome = await consent_for("do the thing")(confirm=False, ctx=Ctx())
    assert outcome.note == safety.NO_CHANNEL


def test_require_treats_a_missing_consent_as_unconfirmed() -> None:
    """Belt and braces: a tool whose Gate somehow did not resolve must still stop."""
    with pytest.raises(BlockedError) as caught:
        require(None, "delete these messages")
    assert "confirm=true" in str(caught.value)


def test_guard_write_blocks_under_read_only() -> None:
    safety.set_settings(Settings(read_only=True))
    with pytest.raises(BlockedError) as caught:
        guard_write("delete messages")
    assert "read-only" in str(caught.value).lower()
    safety.set_settings(Settings())
    guard_write("delete messages")  # no exception


class TestPrefPolicy:
    def test_allowlisted_pref_is_writable(self) -> None:
        ok, reason = Settings().pref_writable("mail.pane_config.dynamic")
        assert ok and not reason

    def test_unlisted_pref_needs_the_flag(self) -> None:
        ok, reason = Settings().pref_writable("browser.newtabpage.enabled")
        assert not ok
        assert "--unsafe-prefs" in reason
        assert Settings(unsafe_prefs=True).pref_writable("browser.newtabpage.enabled")[0]

    @pytest.mark.parametrize(
        "name",
        [
            "network.proxy.http",
            "security.tls.version.min",
            "signon.rememberSignons",
            "xpinstall.signatures.required",
            "extensions.experiments.enabled",
            "general.config.filename",
            "marionette.port",
        ],
    )
    def test_denylisted_prefs_are_refused_even_with_the_flag(self, name: str) -> None:
        """These either hold credentials, redirect traffic, or control the add-on
        trust model this bridge depends on. No flag should open them."""
        ok, reason = Settings(unsafe_prefs=True).pref_writable(name)
        assert not ok, f"{name} should never be writable"
        assert "blocked" in reason.lower()


def test_annotation_sets_are_honest() -> None:
    assert safety.READ_ONLY.read_only_hint and not safety.READ_ONLY.destructive_hint
    assert safety.DESTRUCTIVE.destructive_hint and not safety.DESTRUCTIVE.read_only_hint
    # Sending leaves the machine, so it is both destructive and open-world.
    assert safety.OUTBOUND.destructive_hint and safety.OUTBOUND.open_world_hint
    assert safety.IDEMPOTENT_WRITE.idempotent_hint
    assert safety.NEEDS_INTERACTION == {"anthropic/requiresUserInteraction": True}
    assert safety.large_output(999_999)["anthropic/maxResultSizeChars"] == 500_000

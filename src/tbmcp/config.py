"""Runtime configuration: which toolsets are registered, and how careful to be.

Every option is settable both as a CLI flag and as an environment variable,
because the two clients we target pass configuration differently — Claude Code
takes per-server `env`, and Codex builds the child environment from scratch, so a
flag baked into `args` is the more reliable channel there.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Literal

SendMode = Literal["draft", "send"]

#: Registration order is the order tools are advertised in. The spec asks servers
#: to be deterministic here so clients (and prompt caches) can cache the list.
ALL_TOOLSETS: tuple[str, ...] = (
    "mail",
    "folders",
    "compose",
    "search",
    "contacts",
    "calendar",
    "filters",
    "accounts",
    "settings",
    "admin",
)

#: What a fresh install gets. Lean on purpose: every extra tool costs context on
#: every request, and the rest are one `--toolsets` away.
DEFAULT_TOOLSETS: tuple[str, ...] = ("mail", "folders", "compose", "search", "admin")

#: Preference branches an agent may touch without `--unsafe-prefs`. Anything that
#: could exfiltrate mail, weaken transport security, or reach the credential store
#: is deliberately absent.
PREF_ALLOWLIST: tuple[str, ...] = (
    "mail.pane_config.",
    "mail.threadpane.",
    "mail.ui.",
    "mailnews.",
    "mail.spellcheck.",
    "mail.compose.",
    "mail.identity.",
    "mail.server.",
    "mail.smtpserver.",
    "mail.account.",
    "mail.accountmanager.",
    "mail.biff.",
    "mail.chat.",
    "mail.tabs.",
    "mail.showCondensedAddresses",
    "mail.warn_on_",
    "mail.forward_",
    "mail.default_",
    "mail.operate_on_msgs_in_collapsed_threads",
    "mail.prompt_purge_threshhold",
    "mail.purge_threshhold_mb",
    "calendar.",
    "ldap_2.servers.",
    "font.",
    "layout.css.devPixelsPerPx",
    "browser.display.",
    "intl.",
    "spellchecker.dictionary",
    "app.update.",
    "extensions.tbmcp.",
)

#: Never writable, even with `--unsafe-prefs`. These either hold secrets, silently
#: redirect traffic, or disable the protections a user cannot re-verify afterwards.
PREF_DENYLIST: tuple[str, ...] = (
    "network.proxy.",
    "security.",
    "signon.",
    "privacy.",
    "extensions.experiments.enabled",
    "xpinstall.signatures.required",
    "general.config.",
    "autoadmin.",
    "mail.smtpserver.*.password",
    "devtools.",
    "marionette.",
    "remote.",
)


@dataclass(frozen=True)
class Settings:
    toolsets: tuple[str, ...] = DEFAULT_TOOLSETS
    read_only: bool = False
    """Register only tools that cannot change anything."""

    yolo: bool = False
    """Skip every confirmation gate. Opt-in, never a default."""

    unsafe_prefs: bool = False
    """Allow preference writes outside PREF_ALLOWLIST (the denylist still applies)."""

    send_mode: SendMode = "draft"
    """`draft` makes mail_send produce a reviewable draft unless told otherwise."""

    profile: str | None = None
    default_timeout: float = 30.0
    autostart_daemon: bool = True
    max_result_chars: int = 120_000
    extra_tools: tuple[str, ...] = field(default_factory=tuple)
    """Individual tools to add on top of the selected toolsets."""

    # ------------------------------------------------------------------ parsing

    @staticmethod
    def _flag(name: str, default: bool) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            toolsets=parse_toolsets(os.environ.get("TBMCP_TOOLSETS")),
            read_only=cls._flag("TBMCP_READ_ONLY", False),
            yolo=cls._flag("TBMCP_YOLO", False),
            unsafe_prefs=cls._flag("TBMCP_UNSAFE_PREFS", False),
            send_mode="send" if os.environ.get("TBMCP_SEND", "draft") == "send" else "draft",
            profile=os.environ.get("TBMCP_PROFILE") or None,
            default_timeout=float(os.environ.get("TBMCP_TIMEOUT", "30") or 30),
            autostart_daemon=not cls._flag("TBMCP_NO_AUTOSTART", False),
            extra_tools=tuple(
                t.strip() for t in (os.environ.get("TBMCP_TOOLS") or "").split(",") if t.strip()
            ),
        )

    def merged_with(self, **overrides: object) -> Settings:
        """CLI flags win over the environment; `None` means "not supplied"."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean)  # type: ignore[arg-type]

    def pref_writable(self, name: str) -> tuple[bool, str]:
        """Whether a preference may be written, and why not when it may not."""
        lowered = name.lower()
        for denied in PREF_DENYLIST:
            stem = denied.replace("*", "")
            if lowered.startswith(stem.lower()) or (
                "*" in denied and lowered.endswith(denied.rsplit("*", 1)[-1].lower())
            ):
                return False, (
                    f"{name} is permanently blocked: it controls credentials, transport "
                    "security, or the add-on trust model."
                )
        if self.unsafe_prefs:
            return True, ""
        if any(lowered.startswith(prefix.lower()) for prefix in PREF_ALLOWLIST):
            return True, ""
        return False, (
            f"{name} is outside the reviewed preference allowlist. Start the server with "
            "--unsafe-prefs to permit it."
        )


def parse_toolsets(raw: str | None) -> tuple[str, ...]:
    """`"all"`, `"default"`, `"mail,settings"`, or `"+settings"` to extend the default."""
    if not raw or not raw.strip():
        return DEFAULT_TOOLSETS
    tokens = [t.strip().lower() for t in raw.split(",") if t.strip()]
    if "all" in tokens:
        return ALL_TOOLSETS
    selected: list[str] = []
    if any(t.startswith("+") for t in tokens):
        selected.extend(DEFAULT_TOOLSETS)
    for token in tokens:
        name = token.lstrip("+")
        if name in ("default", ""):
            for item in DEFAULT_TOOLSETS:
                if item not in selected:
                    selected.append(item)
            continue
        if name not in ALL_TOOLSETS:
            known = ", ".join(ALL_TOOLSETS)
            raise SystemExit(f"unknown toolset {name!r}; choose from: {known}, all")
        if name not in selected:
            selected.append(name)
    # Preserve ALL_TOOLSETS order for a deterministic tools/list.
    return tuple(name for name in ALL_TOOLSETS if name in selected)

"""Composing mail — the one toolset whose output leaves the machine.

Everything here defaults to a *draft*. A draft is reviewable, deletable, and costs
the user nothing; a send is none of those. The default is only overridden by the
operator (`tbmcp serve --send`), never by the model, so an agent that forgets to
think about `mode` still lands in the safe place.

The gates are the same four layers as everywhere else, but sends also carry
`OUTBOUND` annotations, which mark them destructive *and* open-world so a host that
reads annotations prompts even when its policy would otherwise auto-approve.
"""

# NOTE: no `from __future__ import annotations` in toolset modules. Tool signatures
# must evaluate at definition time so `Gate(...)` produces a real
# `Annotated[Consent, Resolve(...)]` rather than a string the SDK has to re-evaluate.

import re
from typing import Any, Literal

from ..errors import UsageError
from ..safety import (
    MUTATING,
    OUTBOUND,
    Gate,
    current_settings,
    guard_write,
    require,
)
from ..server import Registrar
from ._common import call, clamp, dry_run, message_summary, one_of, page

MODES = ("draft", "send", "later")

#: A recipient must look like `local@domain.tld`, optionally wrapped in a display
#: name. Deliberately strict: a typo that reaches the SMTP server is a bounce with
#: the user's name on it, and a wrong-but-valid address is worse than a refusal.
_MAILBOX = re.compile(r"^[^\s@<>,;]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$")

#: Thunderbird's own restriction on ComposeDetails.customHeaders, checked here so the
#: model gets a fixable message instead of an ExtensionError from inside the add-on.
_HEADER_NAME = re.compile(r"^(?:X-(?!Mozilla-)[A-Za-z0-9-]+|MSIP_Labels)$", re.IGNORECASE)

Priority = Literal["lowest", "low", "normal", "high", "highest"]
DeliveryFormat = Literal["auto", "plaintext", "html", "both"]


def _mode(mode: str | None) -> str:
    """Resolve the effective send mode.

    An unset `mode` means "use the server's policy", which is `draft` unless the
    operator started with `--send`. That decision belongs to whoever launched the
    server, not to the model composing the message.
    """
    if mode is not None:
        return one_of(mode, MODES, field="mode", default="draft")
    return "send" if current_settings().send_mode == "send" else "draft"


def _addresses(values: list[str] | None, *, field: str, required: bool = False) -> list[str] | None:
    """Validate a recipient list, one recipient per entry."""
    items = [str(v).strip() for v in (values or []) if str(v).strip()]
    if not items:
        if required:
            raise UsageError(
                f"{field} is empty — a message needs at least one recipient. Pass a list "
                'like ["someone@example.com"].'
            )
        return None
    clean: list[str] = []
    for item in items:
        inner = item
        if item.endswith(">") and "<" in item:
            inner = item[item.rindex("<") + 1 : -1].strip()
        if _MAILBOX.match(inner):
            clean.append(item)
            continue
        # `List <List>` is how the compose API addresses an address-book mailing list;
        # anything else without an @ is a mistake worth stopping here.
        if inner and "@" not in inner and item.endswith(">") and "<" in item:
            clean.append(item)
            continue
        raise UsageError(
            f"{field} entry {item!r} is not an email address. Use one entry per recipient, "
            'either "someone@example.com" or "Their Name <someone@example.com>"; an '
            'address-book mailing list is written "List Name <List Name>".'
        )
    return clean


def _attachments(paths: list[str] | None) -> list[dict[str, str]] | None:
    """Turn local file paths into the add-on's attachment shape.

    The paths travel as paths: the privileged half reads them with `x.files.read`, so
    a large file never has to be base64'd through a JSON frame on the bridge.
    """
    if not paths:
        return None
    clean: list[dict[str, str]] = []
    for path in paths:
        text = str(path).strip()
        if not text:
            raise UsageError("attachments must be non-empty file paths.")
        clean.append({"path": text})
    return clean


def _headers(headers: dict[str, str] | None) -> list[dict[str, str]] | None:
    if not headers:
        return None
    out: list[dict[str, str]] = []
    for name, value in headers.items():
        if not _HEADER_NAME.match(str(name)):
            raise UsageError(
                f"custom_headers cannot set {name!r}. Thunderbird only allows X-* headers "
                "(and not X-Mozilla-*); use `priority`, `return_receipt` or `reply_to_"
                "message_id` for the standard ones."
            )
        out.append({"name": str(name), "value": "" if value is None else str(value)})
    return out


def _details(
    *,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    subject: str | None = None,
    body: str | None = None,
    is_html: bool = False,
    attachments: list[str] | None = None,
    identity_id: str | None = None,
    priority: str | None = None,
    return_receipt: bool = False,
    delivery_format: str | None = None,
    custom_headers: dict[str, str] | None = None,
    require_recipients: bool = False,
) -> dict[str, Any]:
    """The parameter block every compose method shares.

    Keys that are `None` are omitted rather than sent: on a reply the add-on treats a
    present key as "replace what Thunderbird derived", and clearing an auto-filled To
    line is never what the caller meant.
    """
    params: dict[str, Any] = {
        "to": _addresses(to, field="to", required=require_recipients),
        "cc": _addresses(cc, field="cc"),
        "bcc": _addresses(bcc, field="bcc"),
        "subject": subject,
        "body": body,
        "isHtml": bool(is_html),
        "attachments": _attachments(attachments),
        "identityId": identity_id,
        "priority": priority,
        "deliveryFormat": delivery_format,
        "customHeaders": _headers(custom_headers),
    }
    if return_receipt:
        params["returnReceipt"] = True
    return {key: value for key, value in params.items() if value is not None}


def _result(raw: dict[str, Any], mode: str, **extra: Any) -> dict[str, Any]:
    """One envelope for every outcome, with the draft case spelled out in words.

    The difference between a helpful default and a surprising one is whether the
    caller can tell, without reading the code, that nothing was sent.
    """
    folder = raw.get("folderPath") or raw.get("folderId")
    payload: dict[str, Any] = {
        "mode": mode,
        "sent": mode == "send",
        "queued": mode == "later",
        "messageId": raw.get("messageId"),
        "headerMessageId": raw.get("headerMessageId"),
        "folderId": raw.get("folderId"),
        "folderPath": raw.get("folderPath"),
        "copies": raw.get("copies") or [],
        "via": raw.get("transport"),
    }
    if mode in ("draft", "template"):
        payload["saved"] = True
        payload["draftId"] = raw.get("messageId")
        payload["note"] = (
            f"Nothing was sent. The message is saved as a {mode}"
            + (f" in {folder}" if folder else "")
            + ". Tell the user it is waiting for them to read over, and repeat the call "
            'with mode="send" only once they have agreed.'
        )
    elif mode == "later":
        payload["note"] = (
            "Queued in the Outbox and not yet delivered. Thunderbird sends it on the "
            "next 'Send Unsent Messages'; check with mail_send_status."
        )
    else:
        payload["note"] = "Sent. It has left the machine and cannot be recalled."
    payload.update(extra)
    return payload


def register(reg: Registrar) -> None:
    # ------------------------------------------------------------------- sending

    @reg.write_tool(title="Send or draft a message", annotations=OUTBOUND)
    async def mail_send(
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        is_html: bool = False,
        attachments: list[str] | None = None,
        identity_id: str | None = None,
        mode: Literal["draft", "send", "later"] | None = None,
        reply_to_message_id: int | None = None,
        priority: Priority | None = None,
        return_receipt: bool = False,
        delivery_format: DeliveryFormat | None = None,
        custom_headers: dict[str, str] | None = None,
        confirm: bool = False,
        consent: Gate("send this message from the user's account") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Write a message. Saves a reviewable draft unless `mode="send"`.

        `mode="later"` queues it in the Outbox instead. Recipients are one address per
        list entry. `attachments` are paths to files on this machine. A draft still
        asks for confirmation, because the identical call with `mode="send"` would
        deliver it. Set `reply_to_message_id` to thread the message under an existing
        one — but `mail_reply` is usually what you want, since it also quotes.
        """
        guard_write("send mail")
        effective = _mode(mode)
        params = _details(
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            is_html=is_html,
            attachments=attachments,
            identity_id=identity_id,
            priority=priority,
            return_receipt=return_receipt,
            delivery_format=delivery_format,
            custom_headers=custom_headers,
            require_recipients=True,
        )
        params["mode"] = effective
        if reply_to_message_id is not None:
            params["replyToMessageId"] = reply_to_message_id
            params["quoteOriginal"] = False
        if dry_run_only:
            return dry_run("compose.send", params)
        require(consent, "send this message")
        result = await call("compose.send", params, timeout=240.0)
        return _result(result, effective, recipients=params.get("to"), subject=subject)

    @reg.write_tool(title="Reply to a message", annotations=OUTBOUND)
    async def mail_reply(
        message_id: int,
        body: str,
        reply_all: bool = False,
        reply_to_list: bool = False,
        quote_original: bool = True,
        is_html: bool = False,
        subject: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[str] | None = None,
        identity_id: str | None = None,
        mode: Literal["draft", "send", "later"] | None = None,
        confirm: bool = False,
        consent: Gate("send this reply from the user's account") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Reply to a message. Saves a reviewable draft unless `mode="send"`.

        Thunderbird derives the recipients, the subject and the quoted original;
        `body` goes above the quote. `reply_all` copies everyone, `reply_to_list`
        answers the mailing list. Passing `cc` replaces the addresses Thunderbird
        derived, so leave it unset unless that is the intent.
        """
        guard_write("send mail")
        effective = _mode(mode)
        params = _details(
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            is_html=is_html,
            attachments=attachments,
            identity_id=identity_id,
        )
        params.update(
            {
                "messageId": message_id,
                "mode": effective,
                "replyType": "replyToList"
                if reply_to_list
                else ("replyToAll" if reply_all else "replyToSender"),
                "quoteOriginal": quote_original,
            }
        )
        require(consent, "send this reply")
        result = await call("compose.reply", params, timeout=240.0)
        return _result(result, effective, repliedTo=message_id, replyType=params["replyType"])

    @reg.write_tool(title="Forward a message", annotations=OUTBOUND)
    async def mail_forward(
        message_id: int,
        to: list[str],
        body: str | None = None,
        forward_as: Literal["inline", "attachment"] = "inline",
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        subject: str | None = None,
        is_html: bool = False,
        attachments: list[str] | None = None,
        identity_id: str | None = None,
        mode: Literal["draft", "send", "later"] | None = None,
        confirm: bool = False,
        consent: Gate("forward this message to someone else") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Forward a message. Saves a reviewable draft unless `mode="send"`.

        `inline` quotes the original in the body; `attachment` attaches it as a
        `.eml`, which preserves the headers a recipient may need. `body` is your
        covering note and goes above the forwarded text.
        """
        guard_write("send mail")
        effective = _mode(mode)
        params = _details(
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            is_html=is_html,
            attachments=attachments,
            identity_id=identity_id,
            require_recipients=True,
        )
        params.update(
            {
                "messageId": message_id,
                "mode": effective,
                "forwardType": "forwardInline" if forward_as == "inline" else "forwardAsAttachment",
            }
        )
        require(consent, "forward this message")
        result = await call("compose.forward", params, timeout=240.0)
        return _result(result, effective, forwarded=message_id, forwardAs=forward_as)

    # -------------------------------------------------------- drafts and windows

    @reg.write_tool(title="Save a draft or template", annotations=MUTATING, interactive=False)
    async def mail_draft_save(
        subject: str | None = None,
        body: str | None = None,
        to: list[str] | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        kind: Literal["draft", "template"] = "draft",
        is_html: bool = False,
        attachments: list[str] | None = None,
        identity_id: str | None = None,
    ) -> dict[str, Any]:
        """Save a message without sending it, as a draft or a template.

        Nothing leaves the machine, so this is not gated — a draft is exactly the
        thing to produce when you want the user to review before anything is sent.
        A template is the reusable kind: Thunderbird keeps it in Templates and opens
        a copy when the user picks it. Recipients are optional here, unlike a send.
        """
        guard_write("save drafts")
        params = _details(
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            is_html=is_html,
            attachments=attachments,
            identity_id=identity_id,
        )
        params["mode"] = one_of(kind, ("draft", "template"), field="kind", default="draft")
        result = await call("compose.save", params, timeout=120.0)
        return _result(result, params["mode"])

    @reg.write_tool(title="Open a compose window", annotations=MUTATING, interactive=False)
    async def mail_compose_open(
        to: list[str] | None = None,
        subject: str | None = None,
        body: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        is_html: bool = False,
        attachments: list[str] | None = None,
        identity_id: str | None = None,
        reply_to_message_id: int | None = None,
        forward_message_id: int | None = None,
        reply_all: bool = False,
        quote_original: bool = True,
    ) -> dict[str, Any]:
        """Open a populated compose window for the user to finish by hand.

        The right answer whenever the wording matters more than the automation, or
        when the user declined a send: they get the draft in front of them with the
        cursor in it. Nothing is sent or saved, and the user sees the window appear,
        so this is not gated.
        """
        guard_write("open compose windows")
        if reply_to_message_id is not None and forward_message_id is not None:
            raise UsageError(
                "Pass either reply_to_message_id or forward_message_id, not both — one "
                "window cannot be a reply and a forward at once."
            )
        params = _details(
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            is_html=is_html,
            attachments=attachments,
            identity_id=identity_id,
        )
        if reply_to_message_id is not None:
            params["replyToMessageId"] = reply_to_message_id
            params["replyType"] = "replyToAll" if reply_all else "replyToSender"
        if forward_message_id is not None:
            params["forwardMessageId"] = forward_message_id
        params["quoteOriginal"] = quote_original
        result = await call("compose.open", params, timeout=120.0)
        return {
            "opened": True,
            "sent": False,
            "tabId": result.get("tabId"),
            "windowId": result.get("windowId"),
            "canSendNow": result.get("canSendNow"),
            "note": (
                "The window is on the user's screen with this content in it. Ask them to "
                "review and send it themselves; nothing has been sent or saved."
            ),
        }

    # -------------------------------------------------------------------- status

    @reg.read_tool(title="Check what is waiting to be sent")
    async def mail_send_status(limit: int = 25) -> dict[str, Any]:
        """List messages sitting in the Outbox, unsent.

        An empty list is the normal answer. Anything here was queued with
        `mode="later"`, or written while Thunderbird was offline, and will go out on
        the next 'Send Unsent Messages'.
        """
        result = await call(
            "compose.status",
            {"limit": clamp(limit, default=25, minimum=1, maximum=200, field="limit")},
            timeout=60.0,
        )
        return page(
            [message_summary(m) for m in result.get("messages", [])],
            total=result.get("queued"),
            folders=result.get("folders") or [],
            queued=result.get("queued", 0),
            note=result.get("note"),
        )

"""Reading, triaging and moving mail — the reference toolset.

Every other toolset follows the shape established here: read tools via
`registrar.read_tool`, mutating tools via `registrar.write_tool` with a `confirm`
parameter, a `Gate(...)` consent, `guard_write()`, and a `changed()`/`dry_run()`
return envelope.
"""

# NOTE: no `from __future__ import annotations` in toolset modules. Tool signatures
# must evaluate at definition time so `Gate(...)` produces a real
# `Annotated[Consent, Resolve(...)]` rather than a string the SDK has to re-evaluate.

from typing import Any, Literal

from ..errors import UsageError
from ..safety import (
    DESTRUCTIVE,
    IDEMPOTENT_WRITE,
    MUTATING,
    Gate,
    guard_write,
    large_output,
    require,
)
from ..server import Registrar
from ._common import (
    call,
    changed,
    clamp,
    coerce_date,
    dry_run,
    message_summary,
    one_of,
    page,
    require_ids,
    trim,
)


def register(reg: Registrar) -> None:
    # ------------------------------------------------------------------ searching

    @reg.read_tool(title="Search mail", meta=large_output())
    async def mail_search(
        full_text: str | None = None,
        subject: str | None = None,
        author: str | None = None,
        recipients: str | None = None,
        body: str | None = None,
        folder_id: str | None = None,
        account_id: str | None = None,
        include_subfolders: bool = True,
        unread: bool | None = None,
        flagged: bool | None = None,
        junk: bool | None = None,
        has_attachment: bool | None = None,
        tags: list[str] | None = None,
        tag_mode: Literal["all", "any", "none"] = "any",
        from_date: str | None = None,
        to_date: str | None = None,
        to_me: bool | None = None,
        from_me: bool | None = None,
        min_size: int | None = None,
        max_size: int | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Search the user's mail. Combine `full_text` with any filters below.

        `full_text` uses Thunderbird's global index and searches headers and bodies
        of already-indexed messages; `subject`/`author`/`body` are substring matches
        evaluated per folder. Dates are ISO-8601. Results are summaries — call
        `mail_get` for a body. Continue with `cursor=nextCursor`.
        """
        query: dict[str, Any] = {}
        if full_text:
            query["fullText"] = full_text
        if subject:
            query["subject"] = subject
        if author:
            query["author"] = author
        if recipients:
            query["recipients"] = recipients
        if body:
            query["body"] = body
        if folder_id:
            query["folderId"] = folder_id
        if account_id:
            query["accountId"] = account_id
        if folder_id or account_id:
            query["includeSubFolders"] = include_subfolders
        for name, value in (
            ("unread", unread),
            ("flagged", flagged),
            ("junk", junk),
            ("attachment", has_attachment),
            ("toMe", to_me),
            ("fromMe", from_me),
        ):
            if value is not None:
                query[name] = value
        if tags:
            query["tags"] = {
                "tags": dict.fromkeys(tags, True),
                "mode": one_of(tag_mode, ("all", "any", "none"), field="tag_mode", default="any"),
            }
        start = coerce_date(from_date, field="from_date")
        end = coerce_date(to_date, field="to_date")
        if start:
            query["fromDate"] = start
        if end:
            query["toDate"] = end
        if min_size is not None or max_size is not None:
            size: dict[str, int] = {}
            if min_size is not None:
                size["min"] = min_size
            if max_size is not None:
                size["max"] = max_size
            query["size"] = size
        if not query:
            raise UsageError(
                "Give at least one filter — a bare search over every folder would be "
                "slow and useless. `full_text` is usually what you want."
            )

        result = await call(
            "messages.query",
            {
                "query": query,
                "limit": clamp(limit, default=25, minimum=1, maximum=200, field="limit"),
                "cursor": cursor,
            },
            timeout=90.0,
        )
        return page(
            [message_summary(m) for m in result.get("messages", [])],
            cursor=result.get("cursor"),
            total=result.get("totalAvailable"),
            searchedFolders=result.get("searchedFolders"),
            indexNote=result.get("indexNote"),
        )

    @reg.read_tool(title="List a folder")
    async def mail_list(
        folder_id: str,
        limit: int = 25,
        cursor: str | None = None,
        sort_by: Literal["date", "subject", "author", "size", "read", "flagged"] = "date",
        descending: bool = True,
    ) -> dict[str, Any]:
        """List messages in one folder, newest first by default.

        Use `folder_list` to discover folder ids. For anything selective, prefer
        `mail_search`.
        """
        result = await call(
            "messages.list",
            {
                "folderId": folder_id,
                "limit": clamp(limit, default=25, minimum=1, maximum=200, field="limit"),
                "cursor": cursor,
                "sortType": sort_by,
                "sortOrder": "descending" if descending else "ascending",
            },
            timeout=60.0,
        )
        return page(
            [message_summary(m) for m in result.get("messages", [])],
            cursor=result.get("cursor"),
            total=result.get("totalAvailable"),
            folderId=folder_id,
        )

    # ------------------------------------------------------------------- reading

    @reg.read_tool(title="Read a message", meta=large_output())
    async def mail_get(
        message_id: int,
        detail: Literal["summary", "text", "full"] = "text",
        decrypt: bool = True,
    ) -> dict[str, Any]:
        """Read one message. `text` gives headers plus the plain-text body.

        `summary` skips the body entirely; `full` adds the MIME part tree and every
        header. Encrypted mail is decrypted when Thunderbird can.
        """
        detail = one_of(detail, ("summary", "text", "full"), field="detail", default="text")
        result = await call(
            "messages.read",
            {"messageId": message_id, "detail": detail, "decrypt": decrypt},
            timeout=60.0,
        )
        body, truncated = trim(result.get("body"))
        payload: dict[str, Any] = {
            **message_summary(result.get("header") or {}),
            "headerMessageId": (result.get("header") or {}).get("headerMessageId"),
        }
        if detail != "summary":
            payload["body"] = body
            payload["bodyIsHtml"] = result.get("bodyIsHtml", False)
            if truncated:
                payload["bodyTruncated"] = True
        if detail == "full":
            payload["headers"] = result.get("headers")
            payload["parts"] = result.get("parts")
            payload["decryptionStatus"] = result.get("decryptionStatus")
        payload["attachments"] = result.get("attachments") or []
        return payload

    @reg.read_tool(title="Read several messages at once", meta=large_output())
    async def mail_get_many(
        message_ids: list[int],
        detail: Literal["summary", "text"] = "summary",
    ) -> dict[str, Any]:
        """Read up to 50 messages in one round trip — for triaging a search result."""
        ids = require_ids(message_ids)
        if len(ids) > 50:
            raise UsageError("mail_get_many takes at most 50 ids; page through instead.")
        result = await call(
            "messages.readMany",
            {"messageIds": ids, "detail": detail},
            timeout=120.0,
        )
        return page(result.get("messages", []), failures=result.get("failures") or [])

    @reg.read_tool(title="Get raw message source", meta=large_output())
    async def mail_get_source(message_id: int, decrypt: bool = False) -> dict[str, Any]:
        """Fetch a message's raw RFC 5322 source, for header forensics.

        On IMAP this needs the message to be available offline; the tool says so
        rather than returning a partial.
        """
        result = await call(
            "messages.raw", {"messageId": message_id, "decrypt": decrypt}, timeout=120.0
        )
        source, truncated = trim(result.get("source"))
        return {
            "messageId": message_id,
            "source": source,
            "truncated": truncated,
            "bytes": result.get("bytes"),
        }

    @reg.read_tool(title="List attachments")
    async def mail_attachments(message_id: int) -> dict[str, Any]:
        """List a message's attachments with part names, sizes and content types."""
        result = await call("messages.listAttachments", {"messageId": message_id})
        return page(result.get("attachments", []), messageId=message_id)

    @reg.write_tool(title="Save an attachment", annotations=MUTATING, interactive=False)
    async def mail_save_attachment(
        message_id: int,
        part_name: str,
        directory: str,
        filename: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Write one attachment to a directory on this machine.

        `part_name` comes from `mail_attachments`. Refuses to clobber an existing
        file unless `overwrite=true`.
        """
        guard_write("save attachments")
        result = await call(
            "messages.saveAttachment",
            {
                "messageId": message_id,
                "partName": part_name,
                "directory": directory,
                "filename": filename,
                "overwrite": overwrite,
            },
            timeout=120.0,
        )
        return {"saved": True, **result}

    # ------------------------------------------------------------------ triaging

    @reg.write_tool(title="Mark messages", annotations=IDEMPOTENT_WRITE, interactive=False)
    async def mail_mark(
        message_ids: list[int],
        read: bool | None = None,
        flagged: bool | None = None,
        junk: bool | None = None,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Set read/flagged/junk state or adjust tags on one or more messages.

        Cheap and reversible, so no confirmation is required. Tag keys come from
        `mail_tags`.
        """
        guard_write("change message flags")
        ids = require_ids(message_ids)
        if all(v is None for v in (read, flagged, junk)) and not add_tags and not remove_tags:
            raise UsageError(
                "Nothing to change — set read, flagged, junk, add_tags or remove_tags."
            )
        result = await call(
            "messages.mark",
            {
                "messageIds": ids,
                "read": read,
                "flagged": flagged,
                "junk": junk,
                "addTags": add_tags or [],
                "removeTags": remove_tags or [],
            },
            timeout=90.0,
        )
        return {
            "updated": result.get("updated", len(ids)),
            "failures": result.get("failures") or [],
        }

    @reg.write_tool(title="Move messages", annotations=MUTATING)
    async def mail_move(
        message_ids: list[int],
        destination_folder_id: str,
        confirm: bool = False,
        consent: Gate("move these messages to another folder") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Move messages into another folder.

        On IMAP the move is asynchronous — the tool waits for Thunderbird to confirm
        before returning, so a following search reflects the change.
        """
        guard_write("move messages")
        ids = require_ids(message_ids)
        if dry_run_only:
            return dry_run(
                "messages.move", {"messageIds": ids, "destinationFolderId": destination_folder_id}
            )
        require(consent, "move these messages")
        result = await call(
            "messages.move",
            {"messageIds": ids, "destinationFolderId": destination_folder_id},
            timeout=180.0,
        )
        return changed(
            "messages.move",
            before={"messageIds": ids, "folderIds": result.get("sourceFolderIds")},
            after={"folderId": destination_folder_id},
            moved=result.get("moved", len(ids)),
            note="Message ids change after a move; re-query to get the new ones.",
        )

    @reg.write_tool(title="Copy messages", annotations=MUTATING)
    async def mail_copy(
        message_ids: list[int],
        destination_folder_id: str,
        confirm: bool = False,
        consent: Gate("copy these messages to another folder") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Copy messages into another folder, leaving the originals in place."""
        guard_write("copy messages")
        ids = require_ids(message_ids)
        require(consent, "copy these messages")
        result = await call(
            "messages.copy",
            {"messageIds": ids, "destinationFolderId": destination_folder_id},
            timeout=180.0,
        )
        return {
            "copied": result.get("copied", len(ids)),
            "destinationFolderId": destination_folder_id,
        }

    @reg.write_tool(title="Archive messages", annotations=MUTATING)
    async def mail_archive(
        message_ids: list[int],
        confirm: bool = False,
        consent: Gate("archive these messages") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Archive messages using each account's configured archive layout."""
        guard_write("archive messages")
        ids = require_ids(message_ids)
        require(consent, "archive these messages")
        result = await call("messages.archive", {"messageIds": ids}, timeout=180.0)
        return {"archived": result.get("archived", len(ids))}

    @reg.write_tool(title="Delete messages", annotations=DESTRUCTIVE)
    async def mail_delete(
        message_ids: list[int],
        permanent: bool = False,
        confirm: bool = False,
        consent: Gate("delete these messages") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Delete messages. Moves to Trash unless `permanent=true`.

        A permanent delete cannot be undone from Thunderbird, so prefer the default
        and let the user empty Trash themselves.
        """
        guard_write("delete messages")
        ids = require_ids(message_ids)
        if dry_run_only:
            return dry_run("messages.delete", {"messageIds": ids, "permanent": permanent})
        require(consent, "delete these messages")
        result = await call(
            "messages.delete", {"messageIds": ids, "permanent": permanent}, timeout=180.0
        )
        return changed(
            "messages.delete",
            before={"messageIds": ids},
            after={"deleted": result.get("deleted", len(ids)), "permanent": permanent},
            recoverable=not permanent,
        )

    # ---------------------------------------------------------------------- tags

    @reg.read_tool(title="List message tags")
    async def mail_tags() -> dict[str, Any]:
        """List the tags defined in Thunderbird, with keys, labels and colours."""
        result = await call("tags.list")
        return page(result.get("tags", []))

    @reg.write_tool(title="Create or update a tag", annotations=IDEMPOTENT_WRITE)
    async def mail_tag_upsert(
        label: str,
        key: str | None = None,
        color: str | None = None,
        confirm: bool = False,
        consent: Gate("add or change a message tag") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Create a tag, or recolour/rename an existing one.

        `color` is `#RRGGBB`. Omit `key` to create a new tag; pass an existing key to
        update it.
        """
        guard_write("change tags")
        require(consent, "change message tags")
        result = await call(
            "tags.upsert", {"key": key, "label": label, "color": color}, timeout=30.0
        )
        return changed("tags", before=result.get("previous"), after=result.get("current"))

    @reg.write_tool(title="Delete a tag", annotations=DESTRUCTIVE)
    async def mail_tag_delete(
        key: str,
        confirm: bool = False,
        consent: Gate("delete a message tag") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Remove a tag definition. Messages keep the raw keyword but lose the label."""
        guard_write("delete tags")
        require(consent, "delete this tag")
        result = await call("tags.delete", {"key": key}, timeout=30.0)
        return changed("tags", before=result.get("previous"), after=None)

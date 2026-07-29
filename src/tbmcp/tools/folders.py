"""The folder tree: browsing it, reshaping it, and emptying it out.

`folder_list` is the entry point for most other tools — folder ids are opaque and
only valid while Thunderbird stays open, so anything that needs one comes here
first. Its output is deliberately terse for that reason.
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
    require,
)
from ..server import Registrar
from ._common import call, changed, clamp, dry_run, page

SpecialUse = Literal["inbox", "drafts", "sent", "trash", "templates", "archives", "junk"]

#: Depth 1 is account roots plus their top-level folders — enough to orient a model
#: without paying for a 400-folder IMAP tree it did not ask for.
DEFAULT_DEPTH = 1
MAX_DEPTH = 20


def _slim(folder: dict[str, Any]) -> dict[str, Any]:
    """Drop the fields that carry no information, and recurse into `children`.

    `folder_list` runs on nearly every conversation, so a key that is always null
    or always empty is a key not worth spending the model's context on.
    """
    out = {
        key: value
        for key, value in folder.items()
        if key != "children" and value is not None and value != [] and value != ""
    }
    children = folder.get("children")
    if children:
        out["children"] = [_slim(child) for child in children]
    return out


def register(reg: Registrar) -> None:
    # ------------------------------------------------------------------- browsing

    @reg.read_tool(title="List folders")
    async def folder_list(
        account_id: str | None = None,
        name: str | None = None,
        parent_id: str | None = None,
        special_use: SpecialUse | None = None,
        is_virtual: bool | None = None,
        is_tag: bool | None = None,
        is_unified: bool | None = None,
        is_favorite: bool | None = None,
        is_root: bool | None = None,
        has_unread_messages: bool | None = None,
        depth: int | None = None,
        tree: bool = False,
        include_counts: bool = True,
        limit: int = 200,
    ) -> dict[str, Any]:
        """List mail folders with their ids and message counts.

        With no filters this browses: every account's root folder plus one level
        below it. Raise `depth` to walk further, or give a filter — `name` is a
        case-insensitive substring — and the whole tree is searched instead. Pass
        `tree=true` to get folders nested under `children` rather than flat.

        Counts come from Thunderbird's own folder database. On IMAP that database
        can lag until the folder has been selected once in Thunderbird, so treat
        unread and total counts as close rather than exact.
        """
        query: dict[str, Any] = {}
        if account_id:
            query["accountId"] = account_id
        if name:
            query["name"] = name
        if parent_id:
            query["folderId"] = parent_id
            query["recursive"] = True
        if special_use:
            query["specialUse"] = [special_use]
        for key, value in (
            ("isVirtual", is_virtual),
            ("isTag", is_tag),
            ("isUnified", is_unified),
            ("isFavorite", is_favorite),
            ("isRoot", is_root),
            ("hasUnreadMessages", has_unread_messages),
        ):
            if value is not None:
                query[key] = value

        if depth is not None:
            depth = clamp(depth, default=DEFAULT_DEPTH, minimum=0, maximum=MAX_DEPTH, field="depth")
        result = await call(
            "folders.query",
            {
                "query": query,
                "depth": depth,
                "tree": tree,
                "includeCounts": include_counts,
                "limit": clamp(limit, default=200, minimum=1, maximum=500, field="limit"),
            },
            timeout=90.0,
        )
        return page(
            [_slim(folder) for folder in result.get("folders", [])],
            truncated=bool(result.get("truncated")),
            searchedWholeTree=result.get("searched"),
        )

    @reg.read_tool(title="Get a folder")
    async def folder_get(folder_id: str, include_subfolders: bool = False) -> dict[str, Any]:
        """Get one folder: counts, special use, flags and IMAP quota.

        Use this to re-check a count after a move or a delete; `folder_list` is the
        cheaper way to find the id in the first place.
        """
        result = await call(
            "folders.get",
            {"folderId": folder_id, "includeSubFolders": include_subfolders},
            timeout=60.0,
        )
        return _slim(result.get("folder") or {})

    @reg.read_tool(title="Get folder capabilities")
    async def folder_capabilities(folder_id: str) -> dict[str, Any]:
        """Report what may be done to a folder before attempting it.

        Answers whether the folder can hold messages, take subfolders, be renamed,
        be deleted, or have messages deleted from it. Worth a call before offering
        the user a plan that a server would refuse.
        """
        result = await call("folders.capabilities", {"folderId": folder_id}, timeout=30.0)
        return {"folderId": folder_id, **(result.get("capabilities") or {})}

    @reg.read_tool(title="Get a unified folder")
    async def folder_get_unified(
        folder_type: SpecialUse, include_subfolders: bool = False
    ) -> dict[str, Any]:
        """Get the unified folder that spans every account, e.g. all inboxes at once.

        Its id works anywhere a folder id is accepted, so `mail_list` on the unified
        inbox lists new mail across all accounts in one call.
        """
        result = await call(
            "folders.getUnified",
            {"type": folder_type, "includeSubFolders": include_subfolders},
            timeout=60.0,
        )
        return _slim(result.get("folder") or {})

    # ------------------------------------------------------------------- reshaping

    @reg.write_tool(title="Create a folder", annotations=MUTATING)
    async def folder_create(
        name: str,
        parent_id: str | None = None,
        account_id: str | None = None,
        confirm: bool = False,
        consent: Gate("create a folder") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Create a folder inside another folder, or at the top of an account."""
        guard_write("create folders")
        if not parent_id and not account_id:
            raise UsageError(
                "Give parent_id to nest the folder inside another, or account_id to "
                "create it at the top level of an account."
            )
        require(consent, "create this folder")
        result = await call(
            "folders.create",
            {"name": name, "parentId": parent_id, "accountId": account_id},
            timeout=60.0,
        )
        return changed("folders.create", before=None, after=_slim(result.get("folder") or {}))

    @reg.write_tool(title="Rename a folder", annotations=MUTATING)
    async def folder_rename(
        folder_id: str,
        new_name: str,
        confirm: bool = False,
        consent: Gate("rename a folder") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Rename a folder, keeping its messages and subfolders."""
        guard_write("rename folders")
        require(consent, "rename this folder")
        result = await call(
            "folders.rename", {"folderId": folder_id, "newName": new_name}, timeout=120.0
        )
        return changed(
            "folders.rename",
            before=_slim(result.get("previous") or {}),
            after=_slim(result.get("folder") or {}),
            note="A folder's id encodes its path, so it changes on rename. Use the new id.",
        )

    @reg.write_tool(title="Move a folder", annotations=MUTATING)
    async def folder_move(
        folder_id: str,
        destination_id: str,
        confirm: bool = False,
        consent: Gate("move a folder and everything in it") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Move a folder under a different parent, with its subfolders.

        `destination_id` is the new parent folder — use an account's root folder to
        move it to the top level. Across accounts this copies then deletes, which on
        IMAP can take a while.
        """
        guard_write("move folders")
        if dry_run_only:
            return dry_run("folders.move", {"folderId": folder_id, "destinationId": destination_id})
        require(consent, "move this folder")
        result = await call(
            "folders.move",
            {"folderId": folder_id, "destinationId": destination_id},
            timeout=300.0,
        )
        return changed(
            "folders.move",
            before=_slim(result.get("previous") or {}),
            after=_slim(result.get("folder") or {}),
            note="Folder and message ids below the moved folder all change; re-query.",
        )

    @reg.write_tool(title="Copy a folder", annotations=MUTATING)
    async def folder_copy(
        folder_id: str,
        destination_id: str,
        confirm: bool = False,
        consent: Gate("copy a folder and everything in it") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Copy a folder and its contents under another parent, leaving the original."""
        guard_write("copy folders")
        require(consent, "copy this folder")
        result = await call(
            "folders.copy",
            {"folderId": folder_id, "destinationId": destination_id},
            timeout=300.0,
        )
        return {
            "copied": True,
            "sourceFolderId": result.get("sourceFolderId", folder_id),
            "folder": _slim(result.get("folder") or {}),
        }

    @reg.write_tool(title="Delete a folder", annotations=DESTRUCTIVE)
    async def folder_delete(
        folder_id: str,
        confirm: bool = False,
        consent: Gate("delete a folder and everything in it") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Delete a folder, its subfolders and every message in them.

        Thunderbird moves the folder to Trash unless it is already inside Trash, in
        which case it goes for good. Check `folder_get` first if the count matters —
        the reply reports what was removed, but cannot put it back.
        """
        guard_write("delete folders")
        if dry_run_only:
            return dry_run("folders.delete", {"folderId": folder_id})
        require(consent, "delete this folder")
        result = await call("folders.delete", {"folderId": folder_id}, timeout=300.0)
        permanent = bool(result.get("permanent"))
        return changed(
            "folders.delete",
            before=_slim(result.get("deleted") or {}),
            after=None,
            subFolderCount=result.get("subFolderCount"),
            permanent=permanent,
            recoverable=not permanent,
        )

    # -------------------------------------------------------------------- triaging

    @reg.write_tool(title="Mark a folder read", annotations=IDEMPOTENT_WRITE)
    async def folder_mark_read(
        folder_id: str,
        include_subfolders: bool = False,
        confirm: bool = False,
        consent: Gate("mark a whole folder as read") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Mark every message in a folder as read.

        There is no per-message undo for this, which is why it asks first. On IMAP
        the flags are pushed to the server.
        """
        guard_write("mark folders as read")
        require(consent, "mark this folder as read")
        result = await call(
            "folders.markAsRead",
            {"folderId": folder_id, "includeSubFolders": include_subfolders},
            timeout=180.0,
        )
        return changed(
            "folders.markAsRead",
            before={"unreadMessageCount": result.get("previousUnread")},
            after={"unreadMessageCount": result.get("unread")},
            folderId=folder_id,
            foldersAffected=result.get("folders"),
            failures=result.get("failures") or [],
        )

    @reg.write_tool(title="Favourite a folder", annotations=IDEMPOTENT_WRITE, interactive=False)
    async def folder_set_favorite(folder_id: str, favorite: bool = True) -> dict[str, Any]:
        """Add or remove a folder from the user's favourites.

        Cosmetic and reversible — it only affects the folder pane's Favourites view,
        so it is not gated. Prompting for something this harmless would only train
        the user to click through the prompts that do matter.
        """
        guard_write("change folder favourites")
        result = await call(
            "folders.update", {"folderId": folder_id, "isFavorite": favorite}, timeout=30.0
        )
        return changed(
            "folders.update",
            before=_slim(result.get("previous") or {}),
            after=_slim(result.get("folder") or {}),
        )

    # ---------------------------------------------------------------- emptying out

    @reg.write_tool(title="Empty Trash", annotations=DESTRUCTIVE)
    async def folder_empty_trash(
        account_id: str | None = None,
        folder_id: str | None = None,
        remove_subfolders: bool = True,
        confirm: bool = False,
        consent: Gate("permanently delete everything in Trash") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Permanently delete everything in one account's Trash.

        This is not recoverable from Thunderbird. Give `account_id` and the account's
        configured Trash is used; give `folder_id` to empty a specific one. Subfolders
        of Trash are removed too unless `remove_subfolders=false`.
        """
        guard_write("empty Trash")
        if not account_id and not folder_id:
            raise UsageError(
                "Give account_id (its configured Trash) or folder_id (a specific Trash "
                "folder) — emptying every account's Trash at once is not offered."
            )
        target = {
            "accountId": account_id,
            "folderId": folder_id,
            "removeSubFolders": remove_subfolders,
        }
        if dry_run_only:
            return dry_run("folders.emptyTrash", target)
        require(consent, "permanently delete everything in Trash")
        result = await call("folders.emptyTrash", target, timeout=600.0)
        return changed(
            "folders.emptyTrash",
            before={"path": result.get("path"), "messages": result.get("deleted")},
            after={"messages": 0},
            deleted=result.get("deleted"),
            foldersRemoved=result.get("foldersRemoved"),
            recoverable=False,
        )

    @reg.write_tool(title="Empty Junk", annotations=DESTRUCTIVE)
    async def folder_empty_junk(
        account_id: str | None = None,
        folder_id: str | None = None,
        confirm: bool = False,
        consent: Gate("permanently delete everything in Junk") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Permanently delete everything in one account's Junk folder.

        Subfolders of Junk are emptied but kept, since they are usually filter
        targets the user set up deliberately. Not recoverable.
        """
        guard_write("empty Junk")
        if not account_id and not folder_id:
            raise UsageError(
                "Give account_id (its configured Junk folder) or folder_id (a specific one)."
            )
        target = {"accountId": account_id, "folderId": folder_id}
        if dry_run_only:
            return dry_run("folders.emptyJunk", target)
        require(consent, "permanently delete everything in Junk")
        result = await call("folders.emptyJunk", target, timeout=600.0)
        return changed(
            "folders.emptyJunk",
            before={"path": result.get("path"), "messages": result.get("deleted")},
            after={"messages": 0},
            deleted=result.get("deleted"),
            subFoldersEmptied=result.get("subFoldersEmptied"),
            recoverable=False,
        )

    # ---------------------------------------------------------------- housekeeping

    @reg.write_tool(title="Download a folder for offline use", annotations=MUTATING)
    async def folder_sync_offline(
        folder_id: str,
        include_subfolders: bool = False,
        confirm: bool = False,
        consent: Gate("download a folder for offline use") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Fetch an IMAP folder's message bodies so they are available offline.

        This is what `mail_get_source` needs before it can return raw source for an
        IMAP message. A large folder means a long download and real network traffic.
        """
        guard_write("download folders for offline use")
        require(consent, "download this folder for offline use")
        result = await call(
            "x.admin.downloadForOffline",
            {"folderId": folder_id, "includeSubFolders": include_subfolders},
            timeout=600.0,
        )
        return {"folderId": folder_id, "started": True, **(result or {})}

    @reg.write_tool(title="Compact folders", annotations=MUTATING)
    async def folder_compact(
        folder_id: str | None = None,
        account_id: str | None = None,
        confirm: bool = False,
        consent: Gate("compact folders and rewrite their message stores") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Reclaim the disk space left behind by deleted messages.

        Deleted mail stays in the message store until the folder is compacted. Pass
        `folder_id` for one folder or `account_id` for all of an account's. Nothing
        readable is lost, but the store is rewritten, so do not interrupt it.
        """
        guard_write("compact folders")
        if not folder_id and not account_id:
            raise UsageError("Give folder_id for one folder, or account_id for a whole account.")
        require(consent, "compact these folders")
        result = await call(
            "x.admin.compactFolders",
            {"folderId": folder_id, "accountId": account_id},
            timeout=600.0,
        )
        return {"compacted": True, **(result or {})}

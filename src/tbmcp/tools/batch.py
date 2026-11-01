"""Preview, apply and undo a fixed set of reversible message changes."""

from typing import Any, Literal

from ..errors import UsageError
from ..safety import IDEMPOTENT_WRITE, Gate, guard_write, require
from ..server import Registrar
from ._common import call, require_ids


def register(reg: Registrar) -> None:
    @reg.read_tool(title="Preview a reversible batch")
    async def mail_batch_preview(
        message_ids: list[int],
        action: Literal["mark", "move"],
        changes: dict[str, Any] | None = None,
        destination_folder_id: str | None = None,
        read: bool | None = None,
        flagged: bool | None = None,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Preview changes to up to 500 messages without changing mail.

        A plan fixes the exact messages and expires after 15 minutes. Mark changes
        read/flagged state and tags; move supports concrete folders in one account.
        Any invalid message prevents applying the plan. History and undo metadata
        stay local for seven days; message bodies are never stored in the journal.
        `changes` accepts read, flagged, add_tags and remove_tags. The separate
        fields are aliases; do not combine them with a changes object.
        """
        ids = list(dict.fromkeys(require_ids(message_ids)))
        if len(ids) > 500:
            raise UsageError("A batch preview takes at most 500 unique message ids.")
        if changes is not None:
            if any(value is not None for value in (read, flagged, add_tags, remove_tags)):
                raise UsageError("Use changes or the separate change fields, not both.")
            aliases = {"addTags": "add_tags", "removeTags": "remove_tags"}
            normalised = {}
            for key, value in changes.items():
                key = aliases.get(key, key)
                if key not in {"read", "flagged", "add_tags", "remove_tags"} or key in normalised:
                    raise UsageError("changes contains an unsupported or duplicate field.")
                if key in {"read", "flagged"}:
                    if type(value) is not bool:
                        raise UsageError(f"changes.{key} must be a boolean.")
                elif not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
                    raise UsageError(f"changes.{key} must be a list of tag keys.")
                normalised[key] = value
            read, flagged = normalised.get("read"), normalised.get("flagged")
            add_tags, remove_tags = normalised.get("add_tags"), normalised.get("remove_tags")
        changes = {}
        for key, value in (("read", read), ("flagged", flagged)):
            if value is not None:
                changes[key] = value
        for key, values in (("addTags", add_tags), ("removeTags", remove_tags)):
            if values is not None:
                if any(not tag.strip() for tag in values):
                    raise UsageError("Tag keys must be non-empty strings.")
                if values:
                    changes[key] = list(dict.fromkeys(values))
        if set(changes.get("addTags", [])) & set(changes.get("removeTags", [])):
            raise UsageError("A tag cannot be both added and removed.")
        if action == "mark":
            if not changes:
                raise UsageError("A mark preview needs a read, flagged or tag change.")
            if destination_folder_id is not None:
                raise UsageError("A mark preview cannot have destination_folder_id.")
        elif action == "move":
            if changes:
                raise UsageError("A move preview cannot also change flags or tags.")
            if not destination_folder_id or not destination_folder_id.strip():
                raise UsageError("A move preview requires destination_folder_id.")
        else:
            raise UsageError("action must be mark or move.")
        params: dict[str, Any] = {"messageIds": ids, "action": action}
        if changes:
            params["changes"] = changes
        if destination_folder_id is not None:
            params["destinationFolderId"] = destination_folder_id
        return await call(
            "daemon.operations.plan", {"kind": "batch", "params": params}, timeout=180.0
        )

    @reg.write_tool(title="Apply a reviewed batch", annotations=IDEMPOTENT_WRITE)
    async def mail_batch_apply(
        plan_id: str,
        confirm: bool = False,
        consent: Gate("apply the reviewed batch of message changes") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Apply a preview once and return its operation id for tb_operation_status.

        Repeating the same plan returns the existing operation, never a second job.
        Conflicts are reported per message. An uncertain outcome stops further work.
        """
        guard_write("apply batch changes")
        if not plan_id.strip():
            raise UsageError("plan_id is required; create a batch preview first.")
        require(consent, "apply the reviewed batch")
        return await call("daemon.operations.apply", {"planId": plan_id}, timeout=30.0)

    @reg.write_tool(title="Undo a completed batch", annotations=IDEMPOTENT_WRITE)
    async def mail_batch_undo(
        operation_id: str,
        confirm: bool = False,
        consent: Gate("undo confirmed changes from this batch") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Undo confirmed batch changes whose touched fields have not changed again.

        Messages changed by someone else are reported as conflicts. Unknown results
        are never guessed. Repeating the call returns the existing undo operation.
        """
        guard_write("undo batch changes")
        if not operation_id.strip():
            raise UsageError("operation_id is required.")
        require(consent, "undo this batch")
        return await call("daemon.operations.undo", {"operationId": operation_id}, timeout=30.0)

"""Freeze and export an explicit attachment selection."""

from typing import Any

from ..errors import UsageError
from ..safety import IDEMPOTENT_WRITE, Gate, guard_write, require
from ..server import Registrar
from ._common import call, require_ids


def register(reg: Registrar) -> None:
    @reg.read_tool(title="Preview attachment downloads")
    async def mail_attachments_plan(
        message_ids: list[int],
        directory: str,
        filename_contains: str | None = None,
        content_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Preview attachment downloads from up to 100 selected messages.

        The existing absolute directory, source attachments and safe collision-free
        names are frozen for 15 minutes. Filters use filename substrings and exact
        MIME types, not PDF contents. Limits are 200 attachments, 25 MiB per file and
        250 MiB total. Invalid selections must be corrected before saving.
        """
        ids = list(dict.fromkeys(require_ids(message_ids)))
        if len(ids) > 100:
            raise UsageError("An attachment plan takes at most 100 unique message ids.")
        if not directory.strip():
            raise UsageError("directory is required and must already exist.")
        params: dict[str, Any] = {"messageIds": ids, "directory": directory}
        if filename_contains is not None:
            params["filenameContains"] = filename_contains
        if content_types is not None:
            if not content_types or any(not item.strip() for item in content_types):
                raise UsageError("content_types must contain non-empty exact MIME types.")
            params["contentTypes"] = content_types
        return await call(
            "daemon.operations.plan", {"kind": "attachments", "params": params}, timeout=180.0
        )

    @reg.write_tool(title="Save reviewed attachments", annotations=IDEMPOTENT_WRITE)
    async def mail_attachments_save(
        plan_id: str,
        confirm: bool = False,
        consent: Gate("save the reviewed attachments to the selected directory") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Save a frozen attachment plan once, without overwriting existing files.

        Returns an operation id for tb_operation_status. Repeating the same plan
        returns the existing job. Results identify each source, path and checksum;
        successfully written files are not automatically deleted on cancellation.
        """
        guard_write("save attachments")
        if not plan_id.strip():
            raise UsageError("plan_id is required; create an attachment plan first.")
        require(consent, "save the reviewed attachments")
        return await call("daemon.operations.apply", {"planId": plan_id}, timeout=30.0)

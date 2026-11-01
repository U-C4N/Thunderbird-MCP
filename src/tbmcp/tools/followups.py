"""Evidence-based, read-only follow-up candidates."""

from typing import Any

from ..errors import UsageError
from ..safety import large_output
from ..server import Registrar
from ._common import call, clamp


def register(reg: Registrar) -> None:
    @reg.read_tool(title="Find conversations awaiting a reply", meta=large_output())
    async def mail_followups(
        account_id: str | None = None,
        lookback_days: int = 30,
        waiting_days: int = 7,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Find possible follow-ups using outgoing mail and indexed reply headers.

        A candidate is not proof that a reply is required. By default inspect the
        last 30 days for conversations waiting at least seven days. Human replies
        close a conversation; automatic replies, mailing lists and self-only mail
        are excluded. Results name their evidence and incomplete coverage. No
        message is sent and no reminder is created. Continue with only cursor and
        limit; snapshots expire after five minutes.
        """
        size = clamp(limit, default=25, minimum=1, maximum=100, field="limit")
        if cursor:
            if account_id is not None or lookback_days != 30 or waiting_days != 7:
                raise UsageError("Pass only cursor and limit to continue follow-ups.")
            params: dict[str, Any] = {"cursor": cursor, "limit": size}
        else:
            lookback = clamp(
                lookback_days, default=30, minimum=1, maximum=180, field="lookback_days"
            )
            waiting = clamp(
                waiting_days, default=7, minimum=1, maximum=lookback, field="waiting_days"
            )
            params = {"lookbackDays": lookback, "waitingDays": waiting, "limit": size}
            if account_id is not None:
                params["accountId"] = account_id
        return await call("messages.followups", params, timeout=75.0)

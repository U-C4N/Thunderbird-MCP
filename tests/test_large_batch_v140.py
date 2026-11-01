"""Exercise the maximum frozen batch through the durable worker and status pages."""

import asyncio
from types import SimpleNamespace

import pytest

from tbmcp.operations import Operations


@pytest.mark.anyio
async def test_maximum_batch_applies_once_and_returns_every_item_in_order(tmp_path):
    touched = []

    async def dispatch(method, params, **kwargs):
        assert method == "messages.batchStep"
        reference = params["item"]["reference"]
        touched.append(reference["messageId"])
        return {"status": "succeeded", "changed": True, "reference": reference}

    daemon = SimpleNamespace(
        _write_lock=asyncio.Lock(),
        _guarded_call=dispatch,
        session=SimpleNamespace(
            hello={"runtimeEpoch": "fixture"}, capabilities={"methods": ["messages.batchStep"]}
        ),
    )
    operations = Operations(daemon, tmp_path / "operations.sqlite")
    ids = list(range(1, 501))
    items = [
        {
            "action": "mark",
            "reference": {
                "messageId": identifier,
                "headerMessageId": f"{identifier}@example.invalid",
                "fingerprint": str(identifier),
                "accountId": "a",
                "folderId": "a://Inbox",
                "folderPath": "/Inbox",
            },
            "previous": {"read": False},
            "desired": {"read": True},
        }
        for identifier in ids
    ]
    try:
        plan = operations.journal.plan("batch", {"items": items})
        accepted = await operations.call("apply", {"planId": plan})
        replay = await operations.call("apply", {"planId": plan})
        operation_id = accepted["operationId"]
        assert replay["operationId"] == operation_id
        await asyncio.gather(*tuple(operations.tasks.values()))
        assert touched == ids
        cursor = None
        reported = []
        pages = 0
        while True:
            page = operations.status(operation_id, cursor=cursor, limit=50)
            assert page["status"] == "succeeded"
            assert page["counts"] == {"succeeded": 500}
            assert len(page["items"]) == 50
            reported.extend(item["reference"]["messageId"] for item in page["items"])
            pages += 1
            if not page["hasMore"]:
                assert page["nextCursor"] is None
                break
            cursor = page["nextCursor"]
        assert pages == 10
        assert reported == ids
    finally:
        operations.journal.db.close()

"""Run indexed search and follow-up acceptance in an isolated Thunderbird profile.

Usage: python tools/smoke_followups.py --session build/live-fixture/session.json

Only the exact profile beneath the session directory is accessed. All messages
are synthetic example.invalid mail. The suite enables indexing in that fixture,
then restores its prior setting. It never discovers or uses a production profile.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from pathlib import Path

import smoke_drafts as native
from mcp import Client
from smoke_workflows import privileged

from tbmcp.bridge import Bridge
from tbmcp.config import Settings
from tbmcp.daemon import Daemon
from tbmcp.profile import ThunderbirdProfile
from tbmcp.server import build_server


def fixture_message(
    run_id: str,
    key: str,
    *,
    days: int = 10,
    author: str = "sender@example.invalid",
    recipients: str = "peer@example.invalid",
    reply_to: str | None = None,
    subject: str | None = None,
    headers: dict | None = None,
) -> str:
    message = EmailMessage(policy=SMTP)
    message["From"], message["To"] = author, recipients
    message["Subject"] = subject or f"Mcpfollowup{run_id} {key}"
    message["Date"] = format_datetime(datetime.now(UTC) - timedelta(days=days))
    message["Message-ID"] = f"<{run_id}-{key}@example.invalid>"
    if reply_to:
        message["References"] = message["In-Reply-To"] = f"<{run_id}-{reply_to}@example.invalid>"
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(
        "Synthetic acceptance fixture. Türkçe yanıt ve arama denetimi.",  # noqa: RUF001
        cte="quoted-printable",
    )
    return message.as_bytes().decode("ascii")


async def main(session: Path) -> None:
    native.configure(session)
    run_id = secrets.token_hex(5)
    run_root = native.ROOT / f"search-followups-{run_id}"
    run_root.mkdir()
    os.environ["TBMCP_STATE_DIR"] = str(run_root / "state")
    folder_name, draft_name = f"Followups-{run_id}", f"FollowupDrafts-{run_id}"
    report = {"runId": run_id, "profile": str(native.PROFILE), "checks": []}

    def check(name, condition, detail=None):
        entry = {"name": name, "passed": bool(condition)}
        if detail is not None:
            entry["detail"] = detail
        report["checks"].append(entry)
        print(json.dumps(entry), flush=True)
        assert condition, f"{name}: {detail}"

    def save_report():
        (native.ROOT / "search-followups-results.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )

    setup = privileged(
        r"""
    const {MailServices}=ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
    const servers=[...MailServices.accounts.accounts].map(a=>a.incomingServer);
    if(servers.some(s=>s.type!=="none" && !["127.0.0.1","localhost"].includes(s.hostname??s.hostName)))throw new Error("Non-fixture account present");
    const old=Services.prefs.getBoolPref("mailnews.database.global.indexer.enabled",false);
    Services.prefs.setBoolPref("mailnews.database.global.indexer.enabled",true);
    const {GlodaIndexer}=ChromeUtils.importESModule("resource:///modules/gloda/GlodaIndexer.sys.mjs");
    GlodaIndexer.enabled=true;
    const root=MailServices.accounts.localFoldersServer.rootFolder;
    for(const name of arguments[0])root.createSubfolder(name,null);
    root.getChildNamed(arguments[0][1]).setFlag(Ci.nsMsgFolderFlags.Drafts);
    const account=MailServices.accounts.findAccountForServer(MailServices.accounts.localFoldersServer);
    const identity=MailServices.accounts.createIdentity();identity.email="alias@example.invalid";identity.fullName="Fixture alias";account.addIdentity(identity);
    return {version:Services.appinfo.version,indexEnabledBefore:old,accountId:account.key,identityId:identity.key};
    """,
        [[folder_name, draft_name]],
    )
    report["thunderbird"] = setup
    raw = [
        fixture_message(run_id, "pending"),
        fixture_message(run_id, "human"),
        fixture_message(
            run_id,
            "human-reply",
            days=8,
            author="peer@example.invalid",
            recipients="sender@example.invalid",
            reply_to="human",
        ),
        fixture_message(run_id, "automatic"),
        fixture_message(
            run_id,
            "automatic-reply",
            days=8,
            author="peer@example.invalid",
            recipients="sender@example.invalid",
            reply_to="automatic",
            headers={"Auto-Submitted": "auto-replied"},
        ),
        fixture_message(run_id, "list", headers={"List-Id": "Fixture list <list.example.invalid>"}),
        fixture_message(run_id, "self", recipients="sender@example.invalid, alias@example.invalid"),
        fixture_message(
            run_id, "group", recipients="peer@example.invalid, colleague@example.invalid"
        ),
        fixture_message(
            run_id,
            "group-reply",
            days=8,
            author="colleague@example.invalid",
            recipients="sender@example.invalid",
            reply_to="group",
        ),
        fixture_message(run_id, "recent"),
        fixture_message(run_id, "recent-followup", days=2, reply_to="recent"),
        fixture_message(run_id, "alias", author="alias@example.invalid"),
        fixture_message(
            run_id,
            "moved-draft",
            headers={"X-Unsent": "1", "X-Mozilla-Draft-Info": "internal/draft; vcard=0"},
        ),
        fixture_message(run_id, "unrelated"),
        fixture_message(
            run_id,
            "unrelated-incoming",
            days=8,
            author="peer@example.invalid",
            recipients="sender@example.invalid",
            subject=f"Mcpfollowup{run_id} unrelated",
        ),
    ]

    def add_messages(name, messages, *, index=True):
        return privileged(
            r"""
        const {MailServices}=ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
        const folder=MailServices.accounts.localFoldersServer.rootFolder.getChildNamed(arguments[0]);
        const local=folder.QueryInterface(Ci.nsIMsgLocalMailFolder);
        const added=arguments[1].map(raw=>local.addMessage(raw));
        // Direct fixture injection has no filter/classifier pipeline. Complete
        // that synthetic step before asking the real indexer to read the mail.
        for(const header of added){
          header.setStringProperty("junkscore","0");
          folder.andProcessingFlags(header.messageKey,~(Ci.nsMsgProcessingFlags.NotReportedClassified|Ci.nsMsgProcessingFlags.ClassifyJunk));
        }
        MailServices.mfn.notifyMsgsClassified(added,false,false);
        if(arguments[2]){
          const {GlodaMsgIndexer}=ChromeUtils.importESModule("resource:///modules/gloda/IndexMsg.sys.mjs");
          return await new Promise((resolve,reject)=>{
            const timer=setTimeout(()=>reject(new Error("Fixture indexing deadline exceeded")),60000);
            if(!GlodaMsgIndexer.indexFolder(folder,{force:true,callback:ok=>{clearTimeout(timer);resolve({indexed:ok,count:folder.getTotalMessages(false)});}})){
              clearTimeout(timer);reject(new Error("Fixture folder cannot be indexed"));
            }
          });
        } return {count:folder.getTotalMessages(false)};
        """,
            [name, messages, index],
        )

    daemon = None
    runner = None
    bridge = None
    try:
        check("fixture_indexing", add_messages(folder_name, raw)["indexed"])
        add_messages(draft_name, [fixture_message(run_id, "draft-folder")], index=False)
        daemon = Daemon(
            ThunderbirdProfile(native.PROFILE, "isolated-followups", False, native.ROOT),
            idle_timeout=0,
        )
        runner = asyncio.create_task(daemon.run())
        bridge = Bridge(profile_hint=str(native.PROFILE), autostart=False)
        await asyncio.wait_for(daemon._session_ready.wait(), 30)
        server = build_server(
            Settings(
                profile=str(native.PROFILE),
                autostart_daemon=False,
                toolsets=("mail", "folders", "search", "admin"),
            ),
            bridge=bridge,
        )
        async with Client(server) as client:

            async def tool(name, **arguments):
                result = await client.call_tool(name, arguments)
                if result.is_error:
                    raise RuntimeError(f"{name}: {result.content}")
                return result.structured_content

            folders = (await bridge.call("folders.query", {"depth": 2, "limit": 500}))["folders"]
            folder = next(value["id"] for value in folders if value["name"] == folder_name)
            all_messages = (
                await bridge.call("messages.query", {"query": {"folderId": folder}, "limit": 100})
            )["messages"]
            by_header = {value["headerMessageId"].strip("<>"): value for value in all_messages}
            term = f"Mcpfollowup{run_id}"
            deadline = time.monotonic() + 60
            while True:
                initial = await tool("search_global", query=term, folder_id=folder, limit=100)
                if initial["count"] == len(raw):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Fixture search indexed {initial['count']} of {len(raw)}: {initial}"
                    )
                await asyncio.sleep(0.25)
            check("indexed_search_fixture_count", initial["count"] == len(raw), initial["count"])
            check(
                "indexed_scope_is_honest",
                initial["coverage"] == "indexed_only"
                and initial["requestedScope"] == {"folderId": folder},
                initial["requestedScope"],
            )
            first = await tool("search_global", query=term, folder_id=folder, limit=4)
            expected_ids = [item["id"] for item in initial["items"]]
            old_cursor = first["nextCursor"]
            added = fixture_message(
                run_id,
                "arrived-after-snapshot",
                days=0,
                author="peer@example.invalid",
                recipients="sender@example.invalid",
            )
            check("new_message_indexed", add_messages(folder_name, [added])["indexed"])
            walked = list(first["items"])
            cursor = old_cursor
            second = await tool("search_global", cursor=cursor, limit=4)
            replay = await tool("search_global", cursor=cursor, limit=4)
            check("independent_snapshot_cursor_replay", replay == second)
            while cursor:
                page = await tool("search_global", cursor=cursor, limit=4)
                walked.extend(page["items"])
                cursor = page.get("nextCursor")
            check(
                "snapshot_stable_while_new_mail_arrives",
                [item["id"] for item in walked] == expected_ids,
                len(walked),
            )
            fresh = await tool("search_global", query=term, folder_id=folder, limit=100)
            check("fresh_search_sees_new_mail", fresh["count"] == len(raw) + 1, fresh["count"])
            conversation = await tool(
                "search_conversation",
                message_id=by_header[f"{run_id}-human@example.invalid"]["id"],
                limit=1,
            )
            tail = await tool("search_conversation", cursor=conversation["nextCursor"], limit=1)
            check(
                "conversation_snapshot_pagination",
                conversation["count"] == tail["count"] == 1 and not tail["hasMore"],
            )
            numeric = await tool("search_global", query="2.0", folder_id=folder)
            check(
                "numeric_term_explains_changed_alternative",
                "unmatchable_terms" in numeric["partialReasons"]
                and numeric["alternativeSearch"]["semanticsChanged"],
            )
            followups = await tool("mail_followups", account_id=setup["accountId"], limit=100)
            report["followups"] = followups
            candidates = {item["headerMessageId"].strip("<>") for item in followups["items"]}
            for key in ("pending", "automatic", "alias"):
                check(
                    f"followup_includes_{key}",
                    f"{run_id}-{key}@example.invalid" in candidates,
                    sorted(candidates),
                )
            for key in (
                "human",
                "list",
                "self",
                "group",
                "recent",
                "recent-followup",
                "moved-draft",
                "draft-folder",
            ):
                check(
                    f"followup_excludes_{key}", f"{run_id}-{key}@example.invalid" not in candidates
                )
            check(
                "same_subject_is_not_human_reply_evidence",
                f"{run_id}-unrelated@example.invalid" in candidates
                or "unlinked_reply" in followups["partialReasons"],
            )
            check(
                "followup_coverage_is_indexed_only",
                followups["coverage"] == "indexed_only"
                and all(
                    item.get("evidence", {}).get("kind") == "thread_headers"
                    for item in followups["items"]
                ),
            )
            first_candidates = await tool("mail_followups", account_id=setup["accountId"], limit=1)
            continuation = await tool(
                "mail_followups", cursor=first_candidates["nextCursor"], limit=1
            )
            repeated = await tool("mail_followups", cursor=first_candidates["nextCursor"], limit=1)
            check("followup_cursor_is_nonconsuming", continuation == repeated)
            privileged(r"""Services.prefs.setBoolPref("mailnews.database.global.indexer.enabled",false);
            const {GlodaIndexer}=ChromeUtils.importESModule("resource:///modules/gloda/GlodaIndexer.sys.mjs");
            GlodaIndexer.enabled=false;return true;""")
            disabled = await tool("search_global", query=term)
            check(
                "disabled_index_is_explicit",
                disabled["count"] == 0
                and "index_disabled" in disabled["partialReasons"]
                and disabled["alternativeSearch"]["semanticsChanged"],
            )
            unavailable = await tool("mail_followups", account_id=setup["accountId"])
            check(
                "disabled_index_never_claims_no_reply",
                unavailable["count"] == 0
                and unavailable["partial"]
                and "index_unavailable" in unavailable["partialReasons"],
            )
            bad_cursor = await client.call_tool(
                "search_global",
                {"cursor": old_cursor.replace(old_cursor.split(":")[1], "old-session")},
            )
            check("old_session_cursor_rejected", bad_cursor.is_error)
            save_report()
    except BaseException as exc:
        report["error"] = repr(exc)
        save_report()
        raise
    finally:
        privileged(
            r"""Services.prefs.setBoolPref("mailnews.database.global.indexer.enabled",arguments[0]);
        const {GlodaIndexer}=ChromeUtils.importESModule("resource:///modules/gloda/GlodaIndexer.sys.mjs");
        GlodaIndexer.enabled=arguments[0];return true;""",
            [setup["indexEnabledBefore"]],
        )
        if bridge:
            await bridge.close()
        if daemon:
            daemon._stop.set()
        if runner:
            await asyncio.wait_for(runner, 10)
        if daemon and daemon._operations:
            daemon._operations.journal.db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    asyncio.run(main(parser.parse_args().session))

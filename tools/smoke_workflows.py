"""Exercise 1.4 workflows through MCP and a real daemon in an isolated profile.

Requires an already-running isolated Thunderbird with Marionette and an installed
candidate add-on. Pass --session build/live-v140/session.json. Every privileged
script verifies the exact profile first. No production discovery or termination
is performed. Fixtures use example.invalid mail and a loopback-only IMAP server.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import time
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

import smoke_drafts as native
from live_fixtures import ImapFixture
from mcp import Client

from tbmcp.addon_build import build_xpi
from tbmcp.bridge import Bridge
from tbmcp.config import Settings
from tbmcp.daemon import Daemon
from tbmcp.profile import ThunderbirdProfile
from tbmcp.server import build_server


def message(identifier: str, attachments: bool = False) -> bytes:
    result = EmailMessage(policy=SMTP)
    result["From"] = "fixture@example.invalid"
    result["To"] = "sender@example.invalid"
    result["Date"] = "Sat, 31 Oct 2026 09:00:00 +0000"
    result["Message-ID"] = f"<{identifier}@example.invalid>"
    result["Subject"] = f"Isolated workflow {identifier}"
    result.set_content("Synthetic local acceptance fixture. No external mail.")
    if attachments:
        for payload in (b"first synthetic attachment\n", b"second synthetic attachment\n"):
            result.add_attachment(
                payload, maintype="application", subtype="pdf", filename="invoice.pdf"
            )
    return result.as_bytes()


def privileged(body: str, args=None):
    value = native.chrome(
        "const done=arguments[arguments.length-1];(async()=>{"
        + body
        + "})().then(done).catch(e=>done({fatalError:String(e)+'\\n'+e.stack}));",
        args,
    )
    if isinstance(value, dict) and value.get("fatalError"):
        raise RuntimeError(value["fatalError"])
    return value


async def main(session: Path, *, skip_imap: bool = False, skip_install: bool = False):
    native.configure(session)
    run_id = secrets.token_hex(5)
    run_root = native.ROOT / f"workflows-{run_id}"
    run_root.mkdir()
    os.environ["TBMCP_STATE_DIR"] = str(run_root / "state")
    report = {"runId": run_id, "profile": str(native.PROFILE), "checks": [], "imap": None}

    def check(name: str, condition: bool, detail=None):
        entry = {"name": name, "passed": bool(condition)}
        if detail is not None:
            entry["detail"] = detail
        report["checks"].append(entry)
        print(json.dumps(entry), flush=True)
        assert condition, f"{name}: {detail}"

    def save_report():
        (native.ROOT / "workflow-results.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )

    setup = privileged(r"""
    const {MailServices}=ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
    const servers=[...MailServices.accounts.accounts].map(a=>a.incomingServer).map(s=>({type:s.type,host:s.hostname??s.hostName}));
    if(servers.some(s=>s.type!=="none" && !["127.0.0.1","localhost"].includes(s.host)))throw new Error("Non-fixture account present");
    return {version:Services.appinfo.version,servers};
    """)
    report["thunderbird"] = setup
    if not skip_install:
        xpi = build_xpi(run_root / "addon")
        privileged(
            r"""
        const {AddonManager}=ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
        const file=Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);file.initWithPath(arguments[0]);
        const install=await AddonManager.getInstallForFile(file);await install.install();return {installed:true};
        """,
            [str(xpi)],
        )
    source_name, filed_name = f"WorkflowSource-{run_id}", f"WorkflowFiled-{run_id}"
    raw = [message(f"{run_id}-{index}", attachments=index == 0).decode() for index in range(125)]
    privileged(
        r"""
    const {MailServices}=ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
    const root=MailServices.accounts.localFoldersServer.rootFolder;
    for(const name of arguments[0])root.createSubfolder(name,null);
    const source=root.getChildNamed(arguments[0][0]).QueryInterface(Ci.nsIMsgLocalMailFolder);
    for(const raw of arguments[1])source.addMessage(raw);return {count:source.getTotalMessages(false)};
    """,
        [[source_name, filed_name], raw],
    )

    profile = ThunderbirdProfile(native.PROFILE, "isolated-workflows", False, native.ROOT)
    daemon = Daemon(profile, idle_timeout=0, event_buffer=1000)
    runner = asyncio.create_task(daemon.run())
    bridge = Bridge(profile_hint=str(native.PROFILE), autostart=False)
    other = Bridge(profile_hint=str(native.PROFILE), autostart=False)
    fixture = None
    imap_account = None
    try:
        await asyncio.wait_for(daemon._session_ready.wait(), 30)
        report["addon"] = daemon.session.app
        server = build_server(
            Settings(profile=str(native.PROFILE), autostart_daemon=False), bridge=bridge
        )
        async with Client(server) as client:

            async def tool(name, **arguments):
                result = await client.call_tool(name, arguments)
                if result.is_error:
                    raise RuntimeError(f"{name}: {result.content}")
                return result.structured_content

            async def done(operation, timeout=90):
                operation_id = operation["operationId"]
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    current = await tool(
                        "tb_operation_status", operation_id=operation_id, limit=500
                    )
                    if current["status"] not in {"queued", "running", "prepared", "dispatching"}:
                        return current
                    await asyncio.sleep(0.1)
                raise TimeoutError(f"Operation did not finish: {operation_id}")

            async def batch(ids, action="mark", **changes):
                plan = await tool("mail_batch_preview", message_ids=ids, action=action, **changes)
                check("batch_preview_valid", not plan.get("invalid"), plan.get("invalid"))
                operation = await tool("mail_batch_apply", plan_id=plan["planId"], confirm=True)
                return plan, await done(operation)

            async def messages(folder):
                return (
                    await bridge.call(
                        "messages.query", {"query": {"folderId": folder}, "limit": 500}
                    )
                )["messages"]

            folders = (
                await bridge.call(
                    "folders.query", {"depth": 2, "limit": 500, "includeCounts": False}
                )
            )["folders"]
            source = next(f["id"] for f in folders if f["name"] == source_name)
            filed = next(f["id"] for f in folders if f["name"] == filed_name)
            initial = await messages(source)
            check("synthetic_local_messages", len(initial) == 125, len(initial))
            ids = [m["id"] for m in initial]
            attachment_id = next(m["id"] for m in initial if m["subject"].endswith(f"{run_id}-0"))
            tags = (await bridge.call("tags.list", {}))["tags"]
            touched, unrelated = tags[0]["key"], tags[1]["key"]
            selected = ids[1:4]
            _, marked = await batch(selected, read=True, flagged=True, add_tags=[touched])
            check("mark_read_flag_tags", marked["status"] == "succeeded", marked)
            await other.call("messages.mark", {"messageIds": selected, "addTags": [unrelated]})
            undone = await done(
                await tool("mail_batch_undo", operation_id=marked["operationId"], confirm=True)
            )
            check("mark_undo_success", undone["status"] == "succeeded", undone)
            after = {m["id"]: m for m in await messages(source)}
            check(
                "mark_undo_preserves_unrelated_tags",
                all(
                    not after[i]["read"]
                    and not after[i]["flagged"]
                    and touched not in after[i]["tags"]
                    and unrelated in after[i]["tags"]
                    for i in selected
                ),
            )
            conflict_plan = await tool(
                "mail_batch_preview", message_ids=[ids[4]], action="mark", read=True
            )
            await other.call("messages.mark", {"messageIds": [ids[4]], "read": True})
            conflict = await done(
                await tool("mail_batch_apply", plan_id=conflict_plan["planId"], confirm=True)
            )
            check(
                "preview_external_change_conflict",
                conflict["counts"].get("conflict") == 1,
                conflict,
            )
            move_plan, moved = await batch(ids[5:7], "move", destination_folder_id=filed)
            check("local_move_success", moved["status"] == "succeeded", moved)
            check(
                "local_move_reference_mapping",
                all(
                    i["reference"]["folderId"] == filed
                    and i["reference"]["messageId"] not in ids[5:7]
                    for i in moved["items"]
                ),
                moved["items"],
            )
            replay = await tool("mail_batch_apply", plan_id=move_plan["planId"], confirm=True)
            check("apply_replays_same_operation", replay["operationId"] == moved["operationId"])
            moved_back = await done(
                await tool("mail_batch_undo", operation_id=moved["operationId"], confirm=True)
            )
            check(
                "local_move_undo",
                moved_back["status"] == "succeeded" and not await messages(filed),
                moved_back,
            )

            baseline = await bridge.call("daemon.events", {"since": 0, "limit": 500})
            while baseline["hasMore"]:
                baseline = await bridge.call(
                    "daemon.events", {"cursor": baseline["nextCursor"], "limit": 500}
                )
            cursor = baseline["nextCursor"]
            live_ids = [m["id"] for m in await messages(source)]
            await other.call("messages.mark", {"messageIds": live_ids, "flagged": True})
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                available = await bridge.call("daemon.events", {"cursor": cursor, "limit": 500})
                if len(available["events"]) >= 125:
                    break
                await asyncio.sleep(0.1)
            first = await bridge.call("daemon.events", {"cursor": cursor, "limit": 50})
            same = await other.call("daemon.events", {"cursor": cursor, "limit": 50})
            check(
                "events_two_clients_same_page",
                first["events"] == same["events"] and len(first["events"]) == 50,
            )
            events, page = list(first["events"]), first
            pages = 1
            while page["hasMore"]:
                page = await bridge.call(
                    "daemon.events", {"cursor": page["nextCursor"], "limit": 50}
                )
                events.extend(page["events"])
                pages += 1
            seqs = [e["seq"] for e in events]
            check(
                "events_pagination_contiguous",
                len(events) >= 125 and seqs == list(range(seqs[0], seqs[-1] + 1)) and pages >= 3,
                {"events": len(events), "pages": pages},
            )
            check("two_daemon_clients_connected", daemon._clients >= 2, daemon._clients)

            destination = run_root / "exports"
            destination.mkdir()
            existing = destination / "invoice.pdf"
            existing.write_bytes(b"pre-existing fixture file")
            export_plan = await tool(
                "mail_attachments_plan", message_ids=[attachment_id], directory=str(destination)
            )
            check(
                "attachment_plan_allocates_unique_names",
                len(export_plan["preview"]) == 2
                and len({p["path"] for p in export_plan["preview"]}) == 2
                and all(Path(p["path"]) != existing for p in export_plan["preview"]),
                export_plan,
            )
            exported = await done(
                await tool("mail_attachments_save", plan_id=export_plan["planId"], confirm=True)
            )
            check(
                "attachment_save_success",
                exported["status"] == "succeeded" and exported["counts"].get("succeeded") == 2,
                exported,
            )
            check(
                "attachment_saved_checksums",
                all(
                    Path(i["path"]).is_file()
                    and hashlib.sha256(Path(i["path"]).read_bytes()).hexdigest() == i["sha256"]
                    for i in exported["items"]
                ),
            )
            check(
                "attachment_existing_file_preserved",
                existing.read_bytes() == b"pre-existing fixture file",
            )
            exported_replay = await tool(
                "mail_attachments_save", plan_id=export_plan["planId"], confirm=True
            )
            check(
                "attachment_save_replay", exported_replay["operationId"] == exported["operationId"]
            )
            collision_plan = await tool(
                "mail_attachments_plan", message_ids=[attachment_id], directory=str(destination)
            )
            collided = Path(collision_plan["preview"][0]["path"])
            collided.write_bytes(b"concurrent fixture file")
            collision = await done(
                await tool("mail_attachments_save", plan_id=collision_plan["planId"], confirm=True)
            )
            check(
                "attachment_concurrent_collision",
                collision["counts"].get("conflict") == 1
                and collided.read_bytes() == b"concurrent fixture file",
                collision,
            )

            if not skip_imap:
                fixture = ImapFixture([message(f"{run_id}-imap-{n}") for n in range(2)])
                fixture.__enter__()
                imap_account = privileged(
                    r"""
                const {MailServices}=ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
                const server=MailServices.accounts.createIncomingServer("fixture","127.0.0.1","imap");
                server.port=arguments[0];server.socketType=0;server.authMethod=Ci.nsMsgAuthMethod.passwordCleartext;
                server.password="fixture";server.valid=true;server.prettyName="Isolated IMAP "+arguments[1];
                const imap=server.QueryInterface(Ci.nsIImapIncomingServer);imap.maximumConnectionsNumber=1;imap.useIdle=false;
                const account=MailServices.accounts.createAccount();account.incomingServer=server;
                const identity=MailServices.accounts.createIdentity();identity.email="fixture@example.invalid";
                identity.fullName="Isolated IMAP fixture";identity.smtpServerKey="smtp1";
                account.addIdentity(identity);
                server.performExpand(null);return {accountId:account.key,serverKey:server.key};
                """,
                    [fixture.port, run_id],
                )
                deadline = time.monotonic() + 30
                imap_folders = []
                while time.monotonic() < deadline:
                    imap_folders = (
                        await bridge.call(
                            "folders.query",
                            {
                                "query": {"accountId": imap_account["accountId"]},
                                "depth": 2,
                                "includeCounts": False,
                            },
                        )
                    )["folders"]
                    if any(f["name"] == "Filed" for f in imap_folders):
                        break
                    await asyncio.sleep(0.2)
                check(
                    "imap_folder_discovery",
                    any(f["name"] == "Filed" for f in imap_folders),
                    imap_folders,
                )
                inbox = next(f["id"] for f in imap_folders if f["name"].upper() == "INBOX")
                remote_filed = next(f["id"] for f in imap_folders if f["name"] == "Filed")
                privileged(
                    r"""
                const {MailServices}=ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
                const server=MailServices.accounts.getIncomingServer(arguments[0]);
                server.rootFolder.getFolderWithFlags(Ci.nsMsgFolderFlags.Inbox).updateFolder(Services.wm.getMostRecentWindow("mail:3pane").msgWindow);return true;
                """,
                    [imap_account["serverKey"]],
                )
                deadline = time.monotonic() + 30
                remote_messages = []
                while time.monotonic() < deadline:
                    remote_messages = await messages(inbox)
                    if len(remote_messages) == 2:
                        break
                    await asyncio.sleep(0.2)
                check("imap_message_download", len(remote_messages) == 2, remote_messages)
                _, remote_marked = await batch([remote_messages[0]["id"]], read=True)
                check("imap_mark_read", remote_marked["status"] == "succeeded", remote_marked)
                _, remote_moved = await batch(
                    [remote_messages[0]["id"]], "move", destination_folder_id=remote_filed
                )
                check("imap_move_success", remote_moved["status"] == "succeeded", remote_moved)
                original_data = {message(f"{run_id}-imap-{n}") for n in range(2)}
                selected_header = remote_messages[0]["headerMessageId"]
                selected_data = next(
                    data
                    for data in original_data
                    if f"Message-ID: <{selected_header}>\r\n".encode() in data
                )

                async def server_state(expected_inbox: set[bytes], expected_filed: set[bytes]):
                    # Some Thunderbird versions first update their local IMAP
                    # view, then dispatch the pending network move. Undoing that
                    # optimistic view immediately can cancel both wire actions,
                    # so wait for independent server evidence before undo.
                    deadline = time.monotonic() + 30
                    while True:
                        with fixture.lock:
                            inbox_data = [m.data for m in fixture.mailboxes["INBOX"].messages]
                            filed_data = [m.data for m in fixture.mailboxes["Filed"].messages]
                        if (
                            len(inbox_data) == len(expected_inbox)
                            and set(inbox_data) == expected_inbox
                            and len(filed_data) == len(expected_filed)
                            and set(filed_data) == expected_filed
                        ):
                            return True
                        if time.monotonic() >= deadline:
                            return False
                        await asyncio.sleep(0.1)

                check(
                    "imap_server_state_after_move",
                    await server_state(original_data - {selected_data}, {selected_data}),
                    {"commands": fixture.commands},
                )
                remote_undo = await done(
                    await tool(
                        "mail_batch_undo", operation_id=remote_moved["operationId"], confirm=True
                    )
                )
                check("imap_move_undo", remote_undo["status"] == "succeeded", remote_undo)
                restored = await server_state(original_data, set())
                check(
                    "imap_native_copy_store_expunge",
                    any("COPY " in c.upper() for c in fixture.commands)
                    and any(
                        "STORE " in c.upper() and "\\DELETED" in c.upper() for c in fixture.commands
                    )
                    and any("EXPUNGE" in c.upper() for c in fixture.commands),
                    fixture.commands,
                )
                with fixture.lock:
                    check(
                        "imap_server_state_after_undo",
                        restored
                        and len(fixture.mailboxes["INBOX"].messages) == 2
                        and not fixture.mailboxes["Filed"].messages
                        and {m.data for m in fixture.mailboxes["INBOX"].messages}
                        == {message(f"{run_id}-imap-{n}") for n in range(2)},
                    )
                report["imap"] = {
                    "transport": "loopback IMAP4rev1 UIDPLUS",
                    "commands": fixture.commands,
                }
            save_report()
    except BaseException as exc:
        report["error"] = repr(exc)
        if fixture:
            report["imap"] = {"commands": fixture.commands}
        save_report()
        raise
    finally:
        if imap_account:
            privileged(
                r"""
            const {MailServices}=ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
            const account=MailServices.accounts.getAccount(arguments[0]);
            if((account.incomingServer.hostname??account.incomingServer.hostName)!=="127.0.0.1"||account.incomingServer.username!=="fixture")throw new Error("Wrong fixture account");
            MailServices.accounts.removeAccount(account,true);return true;
            """,
                [imap_account["accountId"]],
            )
        if fixture:
            fixture.__exit__()
        await bridge.close()
        await other.close()
        daemon._stop.set()
        try:
            await asyncio.wait_for(runner, 10)
        except TimeoutError:
            report["daemonShutdown"] = "timed out after isolated test; runner cancelled"
            save_report()
        if daemon._operations:
            daemon._operations.journal.db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--skip-imap", action="store_true")
    parser.add_argument("--skip-install", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.session, skip_imap=args.skip_imap, skip_install=args.skip_install))

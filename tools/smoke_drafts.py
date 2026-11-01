"""Run draft acceptance checks against an already running ISOLATED Thunderbird.

Usage: python tools/smoke_drafts.py --session build/live-v140/session.json
Or bootstrap everything: python tools/live_profile.py --binary /path/to/thunderbird

The JSON must contain profile (beneath the session file's directory) and port
(Marionette). The isolated profile must have account1 local folders, identity id1
(sender@example.invalid), and smtp1 at the session's loopback smtpPort (37902 for
older fixture files). tools/live_profile.py prepares this automatically. Launch with
MOZ_REMOTE_ALLOW_SYSTEM_ACCESS=1, install the candidate add-on in this profile,
then run. No production profile discovery, installation, or process termination
is performed. A temporary loopback SMTP accepts only example.invalid recipients.
Fixture messages stay in this isolated profile for inspection.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import socketserver
import threading
from pathlib import Path
from typing import ClassVar

from websockets.asyncio.server import serve

from tbmcp.marionette import Marionette

ROOT: Path
STATE: dict
PROFILE: Path


def configure(session: Path):
    global ROOT, STATE, PROFILE
    session = session.resolve()
    ROOT = session.parent
    STATE = json.loads(session.read_text())
    PROFILE = Path(STATE["profile"]).resolve()
    if not PROFILE.is_relative_to(ROOT) or PROFILE == ROOT:
        raise ValueError("The test profile must be beneath the isolated session directory.")


class SMTP(socketserver.StreamRequestHandler):
    messages: ClassVar[list[bytes]] = []

    def handle(self):
        self.wfile.write(b"220 isolated localhost test\r\n")
        content = []
        data = False
        while line := self.rfile.readline():
            if data:
                if line == b".\r\n":
                    self.messages.append(b"".join(content))
                    content = []
                    data = False
                    self.wfile.write(b"250 accepted locally\r\n")
                else:
                    content.append(line)
                continue
            command = line.upper()
            if command.startswith(b"EHLO") or command.startswith(b"HELO"):
                self.wfile.write(b"250 localhost\r\n")
            elif command.startswith(b"MAIL FROM:"):
                self.wfile.write(b"250 ok\r\n")
            elif command.startswith(b"RCPT TO:"):
                assert b"EXAMPLE.INVALID" in command, "Only fixture recipients accepted"
                self.wfile.write(b"250 ok\r\n")
            elif command.startswith(b"DATA"):
                data = True
                self.wfile.write(b"354 data\r\n")
            elif command.startswith(b"QUIT"):
                self.wfile.write(b"221 bye\r\n")
                break
            elif command.startswith(b"RSET") or command.startswith(b"NOOP"):
                self.wfile.write(b"250 ok\r\n")
            else:
                self.wfile.write(b"502 unsupported\r\n")


def chrome(script, args=None):
    m = Marionette(port=STATE["port"])
    m.start_chrome_session()
    found = m.execute(
        'arguments[arguments.length-1](Services.dirsvc.get("ProfD",Ci.nsIFile).path);'
    )
    assert Path(found).resolve() == PROFILE
    try:
        return m.execute(script, args or [])
    finally:
        m.close()


async def check_unpatched_editor(rpc, checked, hello, run_id):
    """Exercise the explicitly unsupported editor on Mozilla bug 1925747 builds."""
    capabilities = hello["capabilities"]["features"]
    checked(
        "advertises saved reads and unavailable native editing",
        capabilities.get("draftRead") is True and capabilities.get("draftEditNative") is False,
    )
    # Import the original test message as MIME: creating a compose window itself
    # can hang on this release. No saved-draft mutation should open one either.
    raw = (
        "From: sender@example.invalid\r\nTo: recipient@example.invalid\r\n"
        "Subject: Read-only fixture\r\nX-Identity-Key: id1\r\n"
        f"Message-ID: <{run_id}@example.invalid>\r\n"
        "Date: Sat, 31 Oct 2026 12:00:00 +0000\r\n"
        "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        "Read-only fixture body\r\n"
    )
    saved = await rpc(
        "messages.import",
        {"folderId": "account1://Drafts", "base64": base64.b64encode(raw.encode()).decode()},
    )
    message_id = saved["message"]["id"]
    start_probe = r"""
    const {MailServices}=ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
    const root=MailServices.accounts.localFoldersServer.rootFolder;
    const main=Services.wm.getMostRecentWindow("mail:3pane");
    const state={count:0,observe(){this.count++;}};
    main.__mcpDraftSmokeProbe=state;
    Services.ww.registerNotification(state);
    arguments[arguments.length-1]([...root.subFolders].map(f=>[f.URI,f.getTotalMessages(false)]));
    """
    final_probe = r"""
    const {MailServices}=ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
    const root=MailServices.accounts.localFoldersServer.rootFolder;
    const main=Services.wm.getMostRecentWindow("mail:3pane");
    const state=main.__mcpDraftSmokeProbe;
    Services.ww.unregisterNotification(state);
    delete main.__mcpDraftSmokeProbe;
    arguments[arguments.length-1]({windowChanges:state.count,
      folders:[...root.subFolders].map(f=>[f.URI,f.getTotalMessages(false)])});
    """
    folders = chrome(start_probe)
    try:
        draft = await rpc("compose.draftGet", {"messageId": message_id})
        checked(
            "saved content, reference and revision remain readable",
            draft.get("body", "").strip() == "Read-only fixture body"
            and bool(draft.get("reference") and draft.get("revision")),
        )
        checked(
            "read explains why edits and sends are unavailable",
            draft.get("editable") is False
            and draft.get("sendable") is False
            and draft.get("reason") == "unsupported_draft_editor"
            and "128.4.3" in draft["note"],
        )
        args = {"reference": draft["reference"], "expectedRevision": draft["revision"]}
        for method, extra in [
            ("compose.draftUpdate", {"changes": {"subject": "must not save"}}),
            ("compose.draftSend", {"mode": "send"}),
            ("compose.draftSend", {"mode": "later"}),
            ("x.drafts.open", {"messageId": message_id, "identityId": "id1"}),
        ]:
            key = f"{run_id}-{method}-{extra.get('mode', '')}"
            try:
                await rpc(method, {**args, **extra, "operationId": key, "requestKey": key})
            except RuntimeError as ex:
                checked(
                    " ".join(filter(None, [method, extra.get("mode"), "refused before effects"])),
                    json.loads(str(ex)).get("code") == "UNSUPPORTED_CAPABILITY",
                )
            else:
                raise AssertionError(f"{method} unexpectedly succeeded")
        after = await rpc("compose.draftGet", {"reference": draft["reference"]})
        checked(
            "saved draft revision and identity unchanged",
            after["messageId"] == message_id and after["revision"] == draft["revision"],
        )
    finally:
        final = chrome(final_probe)
    checked(
        "no windows opened, no folders changed and no SMTP accepted",
        final["windowChanges"] == 0
        and sorted(folders) == sorted(final["folders"])
        and len(SMTP.messages) == 0,
    )


async def main():
    SMTP.messages = []
    setup = chrome(r"""const done=arguments[arguments.length-1];
    (async()=>{ const {MailServices}=ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
    for(const w of Services.wm.getEnumerator("")){if(w.gMsgCompose){w.SetContentAndBodyAsUnmodified();w.close();}}
    const server=MailServices.accounts.localFoldersServer;const root=server.rootFolder;
    for(const [name,flag] of [["Drafts",Ci.nsMsgFolderFlags.Drafts],["Sent",Ci.nsMsgFolderFlags.SentMail],["Templates",Ci.nsMsgFolderFlags.Templates]]){
      let folder;try{folder=root.getChildNamed(name);}catch(e){}if(!folder){root.createSubfolder(name,null);folder=root.getChildNamed(name);}folder.setFlag(flag);
    }
    const id=MailServices.accounts.getIdentity("id1");id["draftsFolderURI" in id?"draftsFolderURI":"draftFolder"]=root.getChildNamed("Drafts").URI;id["fccFolderURI" in id?"fccFolderURI":"fccFolder"]=root.getChildNamed("Sent").URI;id.doFcc=true;id.smtpServerKey="smtp1";
    const smtp=(MailServices.outgoingServer || MailServices.smtp).getServerByKey("smtp1");
    done({version:Services.appinfo.version,root:root.URI,identities:[...MailServices.accounts.allIdentities].map(i=>({key:i.key,email:i.email})),smtp:smtp.hostname || smtp.serverURI.host,smtpPort:smtp.port || smtp.serverURI.port});
    })().catch(e=>done({fatalError:String(e)+"\n"+e.stack}));""")
    print("setup", setup, flush=True)
    assert setup["smtp"] == "127.0.0.1"
    smtp_port = STATE.get("smtpPort", 37902)
    assert setup["smtpPort"] == smtp_port
    connected = asyncio.Future()
    stop = asyncio.Event()

    async def handler(ws):
        hello = json.loads(await ws.recv())
        assert hello["token"] == token
        await ws.send(json.dumps({"t": "welcome", "protocol": 1, "server": "isolated-test"}))
        if not connected.done():
            connected.set_result((ws, hello))
        await ws.wait_closed()

    token = secrets.token_urlsafe(32)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    smtp = socketserver.ThreadingTCPServer(("127.0.0.1", smtp_port), SMTP)
    smtp_thread = threading.Thread(target=smtp.serve_forever, daemon=True)
    smtp_thread.start()
    checks = []
    run_id = secrets.token_hex(4)
    try:
        async with serve(handler, "127.0.0.1", 0, max_size=16 * 1024 * 1024) as bridge:
            port = bridge.sockets[0].getsockname()[1]
            pairing = PROFILE / "tbmcp-bridge.json"
            pairing.write_text(
                json.dumps({"version": 1, "port": port, "token": token, "pid": os.getpid()})
            )
            ws, hello = await asyncio.wait_for(connected, 25)
            print("connected", hello.get("app"), flush=True)
            seq = 0

            async def rpc(method, params):
                nonlocal seq
                seq += 1
                ident = seq
                await ws.send(
                    json.dumps(
                        {
                            "t": "req",
                            "id": ident,
                            "method": method,
                            "params": params,
                            "deadlineMs": 45000,
                        }
                    )
                )
                async with asyncio.timeout(50):
                    while True:
                        frame = json.loads(await ws.recv())
                        if frame.get("t") == "ping":
                            await ws.send(json.dumps({"t": "pong", "ts": frame.get("ts")}))
                            continue
                        if frame.get("t") == "res" and frame.get("id") == ident:
                            if not frame.get("ok"):
                                raise RuntimeError(json.dumps(frame.get("error")))
                            return frame["result"]

            def checked(name, condition):
                checks.append({"check": name, "passed": bool(condition)})
                print(name, condition, flush=True)
                assert condition, name

            # Determine the expected path from the OS build, independently of
            # the add-on's advertisement; an incorrect negative cannot skip tests.
            needs_editor_fix = chrome(r"""
              const v=Services.appinfo.version, major=parseInt(v,10);
              arguments[arguments.length-1](major===128 ? Services.vc.compare(v,"128.4.3")<0 :
                Services.vc.compare(v,"133.0b3")<0);
            """)
            if needs_editor_fix:
                await check_unpatched_editor(rpc, checked, hello, run_id)
                stop.set()
                await ws.close()
                pairing.unlink(missing_ok=True)
                return

            features = hello["capabilities"]["features"]
            assert features.get("draftRead") is True and features.get("draftEditNative") is True
            saved = await rpc(
                "compose.save",
                {
                    "mode": "draft",
                    "identityId": "id1",
                    "subject": "Isolated draft smoke",
                    "to": ["recipient@example.invalid"],
                    "body": "Original body",
                    "isHtml": False,
                },
            )
            print("saved draft", saved["messageId"], flush=True)
            draft = await rpc("compose.draftGet", {"messageId": saved["messageId"]})
            print("draft snapshot", draft["messageId"], flush=True)
            checked("saved draft is editable", draft["editable"])
            changed = await rpc(
                "compose.draftUpdate",
                {
                    "reference": draft["reference"],
                    "expectedRevision": draft["revision"],
                    "operationId": run_id + "smoke-update",
                    "requestKey": "smoke-update",
                    "changes": {
                        "subject": "Changed isolated draft",
                        "body": "Revised body",
                        "cc": [],
                    },
                },
            )
            print("changed draft", changed["messageId"], flush=True)
            checked("new saved revision", changed["revision"] != draft["revision"])
            after = await rpc("compose.draftGet", {"reference": changed["reference"]})
            print("after snapshot", after["messageId"], flush=True)
            checked(
                "body updated and own editor closed",
                after["body"].rstrip("\r\n") == "Revised body" and not after["openInComposer"],
            )
            sent = await rpc(
                "compose.draftSend",
                {
                    "reference": after["reference"],
                    "expectedRevision": after["revision"],
                    "operationId": run_id + "smoke-send",
                    "requestKey": "smoke-send",
                    "mode": "send",
                },
            )
            print("sent result", sent["status"], flush=True)
            checked("one local SMTP acceptance", len(SMTP.messages) == 1 and sent["sent"])
            # Request-replay must use the add-on cache even though the draft has been consumed.
            replay = await rpc(
                "compose.draftSend",
                {
                    "reference": after["reference"],
                    "expectedRevision": after["revision"],
                    "operationId": run_id + "smoke-send",
                    "requestKey": "smoke-send",
                    "mode": "send",
                },
            )
            checked("replay does not send twice", len(SMTP.messages) == 1 and replay == sent)

            async def expect_error(method, params, code):
                try:
                    await rpc(method, params)
                except RuntimeError as ex:
                    checked(code, json.loads(str(ex)).get("code") == code)
                else:
                    raise AssertionError("Expected " + code)

            async def fresh(subject, body="Body", **details):
                saved = await rpc(
                    "compose.save",
                    {
                        "mode": "draft",
                        "identityId": "id1",
                        "subject": subject,
                        "to": ["recipient@example.invalid"],
                        "body": body,
                        "isHtml": False,
                        **details,
                    },
                )
                return await rpc("compose.draftGet", {"messageId": saved["messageId"]})

            def op(draft, name, **kwargs):
                return {
                    "reference": draft["reference"],
                    "expectedRevision": draft["revision"],
                    "operationId": run_id + name,
                    "requestKey": run_id + name,
                    **kwargs,
                }

            html = await fresh(
                "HTML fixture",
                '<p>Original HTML</p><div class="moz-signature">Signature fixture</div>',
                isHtml=True,
                deliveryFormat="html",
                attachments=[
                    {
                        "filename": "keep.txt",
                        "base64": base64.b64encode(b"keep attachment").decode(),
                        "contentType": "text/plain",
                    }
                ],
            )
            print(
                "html fixture", html.get("bodyIsHtml"), len(html.get("attachments", [])), flush=True
            )
            checked(
                "HTML draft and regular attachment readable",
                html["bodyIsHtml"] and len(html["attachments"]) == 1,
            )
            html_change = await rpc(
                "compose.draftUpdate",
                op(html, "html-metadata", changes={"subject": "HTML preserved"}),
            )
            html_after = await rpc("compose.draftGet", {"reference": html_change["reference"]})
            checked(
                "HTML signature and attachment preserved",
                "Signature fixture" in html_after["body"] and len(html_after["attachments"]) == 1,
            )
            extra_file = ROOT / "added.txt"
            extra_file.write_text("new attachment", encoding="utf-8")
            html_change = await rpc(
                "compose.draftUpdate",
                op(
                    html_after,
                    "html-replace",
                    changes={
                        "body": "<p>Replacement HTML</p>",
                        "removeAttachmentRefs": [html_after["attachments"][0]["attachmentRef"]],
                        "addAttachments": [{"path": str(extra_file), "filename": "added.txt"}],
                    },
                ),
            )
            html_after = await rpc("compose.draftGet", {"reference": html_change["reference"]})
            checked(
                "HTML body and attachments changed",
                "Replacement HTML" in html_after["body"]
                and [a["name"] for a in html_after["attachments"]] == ["added.txt"],
            )
            await expect_error(
                "compose.draftUpdate",
                {
                    **op(html_after, "stale", changes={"subject": "must not save"}),
                    "expectedRevision": "sha256:" + "0" * 64,
                },
                "DRAFT_CONFLICT",
            )
            raw_base = "From: Sender <sender@example.invalid>\r\nTo: recipient@example.invalid\r\nSubject: MIME fixture\r\nDate: Sat, 31 Oct 2026 12:00:00 +0000\r\nX-Identity-Key: id1\r\nX-Mozilla-Draft-Info: internal/draft; vcard=0; receipt=0; DSN=0; uuencode=0; attachmentreminder=0; deliveryformat=4\r\nMIME-Version: 1.0\r\n"

            async def imported(name, mime):
                raw = raw_base + "Message-ID: <" + run_id + name + "@example.invalid>\r\n" + mime
                result = await rpc(
                    "messages.import",
                    {
                        "folderId": "account1://Drafts",
                        "base64": base64.b64encode(raw.encode()).decode(),
                    },
                )
                return await rpc("compose.draftGet", {"messageId": result["message"]["id"]})

            cid = await imported(
                "cid",
                'Content-Type: multipart/related; boundary="xx"\r\n\r\n--xx\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n<p>Inline<img src="cid:tiny"></p>\r\n--xx\r\nContent-Type: image/png\r\nContent-ID: <tiny>\r\nContent-Transfer-Encoding: base64\r\nContent-Disposition: inline; filename="tiny.png"\r\n\r\niVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z0SIAAAAASUVORK5CYII=\r\n--xx--\r\n',
            )
            checked("CID inline content detected", cid["hasInlineContent"])
            await expect_error(
                "compose.draftUpdate",
                op(cid, "cid-reject", changes={"body": "replacement"}),
                "DRAFT_UNSUPPORTED_MIME",
            )
            cid_change = await rpc(
                "compose.draftUpdate",
                op(cid, "cid-preserve", changes={"subject": "Inline preserved"}),
            )
            cid_after = await rpc("compose.draftGet", {"reference": cid_change["reference"]})
            print("CID metadata retained", len(cid_after["attachments"]), flush=True)
            checked(
                "CID preserved during metadata update",
                cid_after["hasInlineContent"] and "cid:" in cid_after["body"],
            )
            protected = await imported(
                "signed",
                'Content-Type: multipart/signed; boundary="signed"; protocol="application/pgp-signature"\r\n\r\n--signed\r\nContent-Type: text/plain\r\n\r\nSigned fixture\r\n--signed\r\nContent-Type: application/pgp-signature\r\n\r\nfixture-not-a-real-signature\r\n--signed--\r\n',
            )
            checked(
                "protected MIME stays metadata-only",
                protected["reason"] == "protected_draft"
                and not protected["editable"]
                and "body" not in protected,
            )
            large = await fresh("Large fixture", "a" * 200001)
            checked(
                "large body cannot be rewritten from preview",
                large["reason"] == "body_too_large"
                and not large["editable"]
                and "body" not in large,
            )
            busy = await fresh("Busy fixture")
            chrome(
                r"""const done=arguments[arguments.length-1];const mid=arguments[0];const {MailServices}=ChromeUtils.importESModule('resource:///modules/MailServices.sys.mjs');const f=MailServices.accounts.localFoldersServer.rootFolder.getChildNamed('Drafts');const h=f.msgDatabase.getMsgHdrForMessageID(mid);MailServices.compose.OpenComposeWindow(null,h,f.getUriForMsg(h),Ci.nsIMsgCompType.Draft,Ci.nsIMsgCompFormat.Default,MailServices.accounts.getIdentity('id1'),null,null);done(true);""",
                [busy["reference"]["headerMessageId"]],
            )
            await asyncio.sleep(1)
            busy_get = await rpc("compose.draftGet", {"reference": busy["reference"]})
            checked(
                "user-style composer is busy",
                busy_get["openInComposer"] and not busy_get["editable"],
            )
            await expect_error(
                "compose.draftUpdate",
                op(busy, "busy-update", changes={"subject": "must not save"}),
                "DRAFT_BUSY",
            )
            remaining = chrome(
                'const done=arguments[arguments.length-1];const wins=[...Services.wm.getEnumerator("")].filter(w=>w.gMsgCompose);done(wins.map(w=>w.GetComposeDetails().subject));'
            )
            checked("busy editor remains untouched", remaining == ["Busy fixture"])
            chrome(
                'for(const w of Services.wm.getEnumerator("")){if(w.gMsgCompose){w.SetContentAndBodyAsUnmodified();w.close();}}arguments[arguments.length-1](true);'
            )
            later = await fresh("Later fixture")
            queued = await rpc("compose.draftSend", op(later, "later", mode="later"))
            checked(
                "send later queues without SMTP",
                queued["queued"] and not queued["sent"] and len(SMTP.messages) == 1,
            )
            queue = await rpc("compose.status", {})
            if queue["messages"]:
                await rpc(
                    "messages.delete",
                    {"messageIds": [m["id"] for m in queue["messages"]], "permanent": True},
                )
            stop.set()
            await ws.close()
            pairing.unlink(missing_ok=True)
    finally:
        stop.set()
        smtp.shutdown()
        smtp.server_close()
        (ROOT / "native-results.json").write_text(
            json.dumps(
                {
                    "thunderbird": setup.get("version", "unknown"),
                    "checks": checks,
                    "smtpAccepted": len(SMTP.messages),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    args = parser.parse_args()
    configure(args.session)
    asyncio.run(main())

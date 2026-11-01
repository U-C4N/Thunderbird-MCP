"""Loopback-only, synthetic IMAP fixture for real Thunderbird acceptance tests.

This is a deliberately small test server, not an IMAP service for real accounts.
It has no outbound connections, accepts one fixed dummy login, and keeps messages
only in memory. UIDPLUS copy/store/expunge exercises Thunderbird's native move.
"""

from __future__ import annotations

import email
import re
import shlex
import socketserver
import threading
from dataclasses import dataclass, field
from email.policy import SMTP


@dataclass
class Message:
    uid: int
    data: bytes
    flags: set[str] = field(default_factory=set)


class Mailbox:
    def __init__(self, messages: list[bytes]) -> None:
        self.messages = [Message(index + 1, data) for index, data in enumerate(messages)]
        self.next_uid = len(messages) + 1


class ImapFixture:
    def __init__(self, messages: list[bytes]) -> None:
        self.mailboxes = {"INBOX": Mailbox(messages), "Filed": Mailbox([])}
        self.lock = threading.RLock()
        self.commands: list[str] = []
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            selected = "INBOX"

            def line(self, value: str) -> None:
                self.wfile.write((value + "\r\n").encode())

            def chosen(self, spec: str, by_uid: bool) -> list[tuple[int, Message]]:
                box = owner.mailboxes[self.selected]
                maximum = (
                    max((msg.uid for msg in box.messages), default=0)
                    if by_uid
                    else len(box.messages)
                )
                wanted = set()
                for part in spec.split(","):
                    ends = part.split(":")
                    values = [maximum if end == "*" else int(end) for end in ends]
                    wanted.update(range(min(values), max(values) + 1))
                return [
                    (index, msg)
                    for index, msg in enumerate(box.messages, 1)
                    if (msg.uid if by_uid else index) in wanted
                ]

            def fetch(self, spec: str, attributes: str, by_uid: bool) -> None:
                for sequence, message in self.chosen(spec, by_uid):
                    header, _, body = message.data.partition(b"\r\n\r\n")
                    header += b"\r\n\r\n"
                    upper = attributes.upper()
                    parts = [
                        f"UID {message.uid}",
                        f"FLAGS ({' '.join(sorted(message.flags))})",
                        f"RFC822.SIZE {len(message.data)}",
                    ]
                    if "INTERNALDATE" in upper:
                        parts.append('INTERNALDATE "31-Oct-2026 09:00:00 +0000"')
                    if "BODYSTRUCTURE" in upper:
                        lines = body.count(b"\n")
                        parts.append(
                            f'BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" {len(body)} {lines})'
                        )
                    output = f"* {sequence} FETCH ({' '.join(parts)}".encode()
                    sections = re.findall(
                        r"BODY(?:\.PEEK)?\[([^\]]*)\](?:<(\d+)\.(\d+)>)?", attributes, re.I
                    )
                    for section, offset, count in sections:
                        if section.upper().startswith("HEADER.FIELDS"):
                            names = set(re.search(r"\(([^)]*)\)", section)[1].upper().split())
                            parsed = email.message_from_bytes(message.data, policy=SMTP)
                            data = (
                                "".join(
                                    f"{name}: {value}\r\n"
                                    for name, value in parsed.raw_items()
                                    if name.upper() in names
                                ).encode()
                                + b"\r\n"
                            )
                        elif section.upper() == "HEADER":
                            data = header
                        elif section.upper() in {"TEXT", "1"}:
                            data = body
                        else:
                            data = message.data
                        suffix = f"<{offset}>" if offset else ""
                        if offset:
                            data = data[int(offset) : int(offset) + int(count)]
                        output += f" BODY[{section}]{suffix} {{{len(data)}}}\r\n".encode() + data
                    if not sections:
                        for attribute, data in [("RFC822.HEADER", header), ("RFC822.TEXT", body)]:
                            if attribute in upper:
                                output += f" {attribute} {{{len(data)}}}\r\n".encode() + data
                    self.wfile.write(output + b")\r\n")

            def expunge(self, spec: str | None = None) -> None:
                permitted = {msg.uid for _, msg in self.chosen(spec, True)} if spec else None
                box = owner.mailboxes[self.selected]
                index = 0
                while index < len(box.messages):
                    message = box.messages[index]
                    if "\\Deleted" in message.flags and (
                        permitted is None or message.uid in permitted
                    ):
                        box.messages.pop(index)
                        self.line(f"* {index + 1} EXPUNGE")
                    else:
                        index += 1

            def handle(self) -> None:
                self.connection.settimeout(120)
                self.line("* OK [CAPABILITY IMAP4rev1 UIDPLUS NAMESPACE] isolated fixture")
                while raw := self.rfile.readline():
                    text = raw.decode("utf-8").rstrip("\r\n")
                    if not text:
                        continue
                    tag, command, *rest = text.split(" ", 2)
                    args = rest[0] if rest else ""
                    command = command.upper()
                    owner.commands.append(
                        command + " " + ("<dummy login>" if command == "LOGIN" else args)
                    )
                    try:
                        with owner.lock:
                            if command == "CAPABILITY":
                                self.line("* CAPABILITY IMAP4rev1 UIDPLUS NAMESPACE")
                            elif command == "LOGIN":
                                if shlex.split(args) != ["fixture", "fixture"]:
                                    self.line(f"{tag} NO dummy login only")
                                    continue
                            elif command == "NAMESPACE":
                                self.line('* NAMESPACE (("" "/")) NIL NIL')
                            elif command in {"LIST", "LSUB"}:
                                tokens = shlex.split(args)
                                pattern = tokens[-1]
                                names = (
                                    owner.mailboxes
                                    if pattern in {"*", "%"}
                                    else [pattern]
                                    if pattern in owner.mailboxes
                                    else []
                                )
                                for name in names:
                                    self.line(f'* {command} (\\HasNoChildren) "/" "{name}"')
                            elif command in {"SELECT", "EXAMINE"}:
                                self.selected = shlex.split(args)[0]
                                box = owner.mailboxes[self.selected]
                                self.line("* FLAGS (\\Seen \\Answered \\Flagged \\Deleted \\Draft)")
                                self.line(
                                    "* OK [PERMANENTFLAGS (\\Seen \\Answered \\Flagged \\Deleted \\Draft \\*)] flags"
                                )
                                self.line(f"* {len(box.messages)} EXISTS")
                                self.line("* 0 RECENT")
                                self.line("* OK [UIDVALIDITY 1] stable test mailbox")
                                self.line(f"* OK [UIDNEXT {box.next_uid}] next")
                                self.line(f"{tag} OK [READ-WRITE] selected")
                                continue
                            elif command == "STATUS":
                                name = shlex.split(args)[0]
                                box = owner.mailboxes[name]
                                values = {
                                    "MESSAGES": len(box.messages),
                                    "RECENT": 0,
                                    "UNSEEN": sum(
                                        "\\Seen" not in msg.flags for msg in box.messages
                                    ),
                                    "UIDVALIDITY": 1,
                                    "UIDNEXT": box.next_uid,
                                }
                                wanted = re.search(r"\(([^)]+)\)", args)[1].split()
                                self.line(
                                    f'* STATUS "{name}" ('
                                    + " ".join(
                                        f"{key} {values.get(key.upper(), 0)}" for key in wanted
                                    )
                                    + ")"
                                )
                            elif command == "CREATE":
                                owner.mailboxes.setdefault(shlex.split(args)[0], Mailbox([]))
                            elif command == "UID" or command in {"FETCH", "STORE", "SEARCH"}:
                                by_uid = command == "UID"
                                if by_uid:
                                    action, args = args.split(" ", 1)
                                else:
                                    action = command
                                action = action.upper()
                                if action == "SEARCH":
                                    self.line(
                                        "* SEARCH "
                                        + " ".join(
                                            str(msg.uid if by_uid else index)
                                            for index, msg in enumerate(
                                                owner.mailboxes[self.selected].messages, 1
                                            )
                                            if "\\Deleted" not in msg.flags
                                        )
                                    )
                                elif action == "EXPUNGE":
                                    self.expunge(args.strip())
                                else:
                                    spec, parameters = args.split(" ", 1)
                                    if action == "FETCH":
                                        self.fetch(spec, parameters, by_uid)
                                    elif action == "COPY":
                                        target = owner.mailboxes[shlex.split(parameters)[0]]
                                        sources, destinations = [], []
                                        for _, message in self.chosen(spec, by_uid):
                                            sources.append(str(message.uid))
                                            destinations.append(str(target.next_uid))
                                            target.messages.append(
                                                Message(
                                                    target.next_uid,
                                                    message.data,
                                                    message.flags.copy(),
                                                )
                                            )
                                            target.next_uid += 1
                                        self.line(
                                            f"{tag} OK [COPYUID 1 {','.join(sources)} {','.join(destinations)}] copied"
                                        )
                                        continue
                                    elif action == "STORE":
                                        mode = parameters.split(" ", 1)[0].upper()
                                        flags = set(
                                            re.search(r"\(([^)]*)\)", parameters)[1].split()
                                        )
                                        for index, message in self.chosen(spec, by_uid):
                                            if mode.startswith("+"):
                                                message.flags |= flags
                                            elif mode.startswith("-"):
                                                message.flags -= flags
                                            else:
                                                message.flags = flags.copy()
                                            if ".SILENT" not in mode:
                                                self.line(
                                                    f"* {index} FETCH (UID {message.uid} FLAGS ({' '.join(sorted(message.flags))}))"
                                                )
                                    else:
                                        self.line(f"{tag} BAD unsupported UID command")
                                        continue
                            elif command == "EXPUNGE":
                                self.expunge()
                            elif command == "LOGOUT":
                                self.line("* BYE fixture closed")
                                self.line(f"{tag} OK logout")
                                return
                            elif command not in {
                                "NOOP",
                                "CHECK",
                                "CLOSE",
                                "SUBSCRIBE",
                                "UNSUBSCRIBE",
                                "ID",
                            }:
                                self.line(f"{tag} BAD unsupported {command}")
                                continue
                            self.line(f"{tag} OK {command} completed")
                    except (KeyError, ValueError, IndexError, TypeError) as exc:
                        self.line(f"{tag} BAD fixture {type(exc).__name__}")

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

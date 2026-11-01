"""Profile-scoped, body-free operation journal and daemon-owned workers.

Requests containing mail bodies live only in the worker's memory. The database
stores hashes, references and explicitly selected receipts, never request payloads.
An interrupted dispatch is deliberately not replayed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .errors import BlockedError, TbmcpError, TransportError, UsageError, from_wire

RETENTION = 7 * 86400
PLAN_TTL = 900
TERMINAL = {"succeeded", "partial", "failed", "cancelled", "interrupted", "unknown"}
PLAN_METHODS = {"batch": "messages.batchPreview", "attachments": "messages.attachmentsPlan"}
STEP_METHODS = {"batch": "messages.batchStep", "attachments": "messages.attachmentsSave"}
SUBMIT_METHODS = {
    "draft_update": "compose.draftUpdate",
    "draft_send": "compose.draftSend",
    "send": "compose.send",
    "reply": "compose.reply",
    "forward": "compose.forward",
}
# Errors and receipts must not accidentally persist a body or an address list.
RECEIPT_KEYS = {
    "status",
    "reference",
    "revision",
    "messageId",
    "headerMessageId",
    "folderId",
    "draftRef",
    "path",
    "name",
    "bytes",
    "sha256",
    "mode",
    "sent",
    "queued",
    "saved",
    "previous",
    "current",
    "desired",
    "changed",
    "changedFields",
    "code",
    "itemId",
    "transport",
    "windowId",
    "tabId",
    "recoveryWindowOpen",
    "cancellationTooLate",
    "reason",
}
FORBIDDEN_KEYS = {
    "body",
    "bodyPreview",
    "plainTextBody",
    "htmlBody",
    "source",
    "base64",
    "subject",
    "author",
    "recipients",
    "to",
    "cc",
    "bcc",
    "preview",
}


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def stable_reference(reference: dict) -> dict:
    return {k: v for k, v in reference.items() if k not in {"messageId", "runtimeEpoch"}}


def safe_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: dict(v)
            if k == "tags"
            and isinstance(v, dict)
            and all(isinstance(flag, bool) for flag in v.values())
            else safe_data(v)
            for k, v in value.items()
            if k not in FORBIDDEN_KEYS
        }
    if isinstance(value, list):
        return [safe_data(v) for v in value]
    return value


def receipt(value: dict) -> dict:
    return safe_data({k: v for k, v in value.items() if k in RECEIPT_KEYS})


def _windows_private_paths(paths: list[Path]) -> None:
    """Replace, rather than augment, ACLs; child SQLite files inherit privacy."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        pointer,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(pointer),
        pointer,
    ]
    advapi.GetSecurityDescriptorDacl.argtypes = [
        pointer,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(pointer),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        pointer,
        pointer,
        pointer,
        pointer,
    ]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    token = wintypes.HANDLE()
    sid_text = wintypes.LPWSTR()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        length = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not length.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(pointer)).contents
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        user_sid = sid_text.value
    finally:
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, pointer))
        kernel.CloseHandle(token)
    for path in paths:
        inheritance = "OICI" if path.is_dir() else ""
        descriptor = pointer()
        sddl = f"D:P(A;{inheritance};FA;;;{user_sid})(A;{inheritance};FA;;;SY)"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), pointer()
            if (
                not advapi.GetSecurityDescriptorDacl(
                    descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
                )
                or not present.value
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            result = advapi.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None)
            if result:
                raise ctypes.WinError(result)
        finally:
            kernel.LocalFree(descriptor)


def _private_database(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        paths = [
            path.parent,
            *(
                candidate
                for candidate in [
                    path,
                    *[Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm")],
                ]
                if candidate.exists()
            ),
        ]
        if any(p.is_symlink() or (getattr(p, "is_junction", lambda: False)()) for p in paths):
            raise OSError("Operation history cannot use a linked directory or file.")
        if sys.platform == "win32":
            _windows_private_paths(paths)
        else:
            for entry in paths:
                entry.chmod(0o700 if entry.is_dir() else 0o600)
    except OSError as exc:
        raise BlockedError(
            "Could not restrict operation history to this user; no operation was started.",
            code="JOURNAL_PERMISSIONS",
        ) from exc


class Journal:
    def __init__(self, path: Path, *, now=time.time) -> None:
        self.now = now
        _private_database(path)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise BlockedError(
                "Operation history uses a newer schema; upgrade the package.", code="JOURNAL_SCHEMA"
            )
        self.db.executescript("""
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, created REAL NOT NULL,
                expires REAL NOT NULL, payload TEXT NOT NULL, valid INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, hash TEXT NOT NULL,
                kind TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL,
                cancel INTEGER NOT NULL DEFAULT 0, result TEXT, subject_hash TEXT);
            CREATE TABLE IF NOT EXISTS items (
                operation_id TEXT NOT NULL, n INTEGER NOT NULL, task TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, PRIMARY KEY(operation_id,n),
                FOREIGN KEY(operation_id) REFERENCES operations(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS refs (
                id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS barrier (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            PRAGMA user_version=1;
            PRAGMA foreign_keys=ON;
        """)
        if os.name != "nt":
            path.chmod(0o600)
        with self.db:
            self.db.execute("UPDATE items SET status='unknown' WHERE status='running'")
            self.db.execute("UPDATE items SET status='interrupted' WHERE status='queued'")
            self.db.execute("UPDATE operations SET status='unknown' WHERE status='running'")
            self.db.execute("UPDATE operations SET status='interrupted' WHERE status='queued'")
        self.purge()

    def barrier(self) -> dict | None:
        row = self.db.execute("SELECT payload FROM barrier WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def set_barrier(self, value: dict | None) -> None:
        with self.db:
            if value is None:
                self.db.execute("DELETE FROM barrier")
            else:
                self.db.execute("INSERT OR REPLACE INTO barrier VALUES(1,?)", (encoded(value),))

    def purge(self) -> None:
        cutoff = self.now() - RETENTION
        with self.db:
            self.db.execute(
                "DELETE FROM operations WHERE created < ? AND status NOT IN ('queued','running')",
                (cutoff,),
            )
            self.db.execute("DELETE FROM plans WHERE created < ?", (cutoff,))
            self.db.execute("DELETE FROM refs WHERE created < ?", (cutoff,))

    def plan(self, kind: str, payload: dict) -> str:
        self.purge()
        ident = secrets.token_urlsafe(24)
        with self.db:
            self.db.execute(
                "INSERT INTO plans VALUES(?,?,?,?,?,?)",
                (
                    ident,
                    kind,
                    self.now(),
                    self.now() + PLAN_TTL,
                    encoded(safe_data({"items": payload.get("items", [])})),
                    not bool(payload.get("invalid")),
                ),
            )
        return ident

    def get_plan(self, ident: str) -> dict:
        row = self.db.execute("SELECT * FROM plans WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise UsageError("Plan not found in this profile.", code="PLAN_NOT_FOUND")
        if row["expires"] < self.now():
            raise UsageError("Plan expired. Prepare a new preview.", code="PLAN_EXPIRED")
        if not row["valid"]:
            raise UsageError(
                "Preview contains ineligible items; prepare a valid selection.", code="PLAN_INVALID"
            )
        return {**dict(row), **json.loads(row["payload"])}

    def create(
        self, key: str, kind: str, request_hash: str, tasks: list[dict], subject_hash=None
    ) -> tuple[str, bool]:
        self.purge()
        old = self.db.execute("SELECT id,hash FROM operations WHERE key=?", (key,)).fetchone()
        if old:
            if old["hash"] != request_hash:
                raise UsageError(
                    "Request key was already used for different content.", code="REQUEST_KEY_REUSED"
                )
            return old["id"], False
        if (
            subject_hash
            and self.db.execute(
                "SELECT 1 FROM operations WHERE subject_hash=? AND status IN ('running','queued','unknown')",
                (subject_hash,),
            ).fetchone()
        ):
            raise BlockedError(
                "This draft revision already has a pending or uncertain send. Inspect its operation.",
                code="OUTCOME_UNKNOWN",
            )
        ident = secrets.token_urlsafe(24)
        with self.db:
            self.db.execute(
                "INSERT INTO operations(id,key,hash,kind,created,status,subject_hash) VALUES(?,?,?,?,?,'queued',?)",
                (ident, key, request_hash, kind, self.now(), subject_hash),
            )
            self.db.executemany(
                "INSERT INTO items VALUES(?,?,?,'queued',NULL)",
                [(ident, n, encoded(safe_data(task))) for n, task in enumerate(tasks)],
            )
        return ident, True

    def operation(self, ident: str) -> dict:
        self.purge()
        row = self.db.execute("SELECT * FROM operations WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise UsageError(
                "Operation not found in this profile or its history expired.",
                code="OPERATION_NOT_FOUND",
            )
        return dict(row)

    def set_status(self, ident: str, status: str, result: dict | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE operations SET status=?,result=? WHERE id=?",
                (status, encoded(receipt(result or {})), ident),
            )

    def item_status(self, ident: str, n: int, status: str, result: dict | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE items SET status=?,result=? WHERE operation_id=? AND n=?",
                (status, encoded(receipt(result or {})), ident, n),
            )

    def item_task(self, ident: str, n: int, task: dict) -> None:
        with self.db:
            self.db.execute(
                "UPDATE items SET task=? WHERE operation_id=? AND n=?",
                (encoded(safe_data(task)), ident, n),
            )

    def items(self, ident: str) -> list[dict]:
        return [
            {**dict(r), "task": json.loads(r["task"]), "result": json.loads(r["result"] or "{}")}
            for r in self.db.execute(
                "SELECT * FROM items WHERE operation_id=? ORDER BY n", (ident,)
            )
        ]

    def remember(self, reference: dict, ident: str | None = None) -> str:
        self.purge()
        if not isinstance(reference, dict) or not reference:
            raise UsageError(
                "The add-on did not return a persistent reference.", code="REFERENCE_UNAVAILABLE"
            )
        if ident is None:
            target = stable_reference(safe_data(reference))
            old = next(
                (
                    r
                    for r in self.db.execute("SELECT id,payload FROM refs")
                    if stable_reference(json.loads(r["payload"])) == target
                ),
                None,
            )
            ident = old["id"] if old else secrets.token_urlsafe(24)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO refs VALUES(?,?,?)",
                (ident, self.now(), encoded(safe_data(reference))),
            )
        return ident

    def resolve(self, ident: str) -> dict:
        self.purge()
        row = self.db.execute("SELECT payload FROM refs WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise UsageError(
                "Draft reference expired or belongs to another profile. Read the draft again.",
                code="REFERENCE_NOT_FOUND",
            )
        return json.loads(row[0])


class Operations:
    def __init__(self, daemon, path: Path) -> None:
        self.daemon = daemon
        self.journal = Journal(path)
        self.tasks: dict[str, asyncio.Task] = {}
        self.health_error: dict[str, str] | None = None

    @property
    def busy(self) -> bool:
        return any(not t.done() for t in self.tasks.values())

    def _start(self, ident: str, coro) -> None:
        async def supervised() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                self._worker_failed(ident)
                raise
            except Exception:
                self._worker_failed(ident)

        task = asyncio.create_task(supervised())
        self.tasks[ident] = task

        def completed(finished: asyncio.Task) -> None:
            self.tasks.pop(ident, None)
            if not finished.cancelled():
                finished.exception()

        task.add_done_callback(completed)

    def require_healthy(self) -> None:
        if self.health_error:
            raise BlockedError(self.health_error["message"], code=self.health_error["code"])

    def _worker_failed(self, ident: str) -> None:
        # Do not persist/log exception text: native errors can contain addresses
        # or message content. A health failure stays latched until daemon restart.
        self.health_error = {
            "code": "JOURNAL_UNHEALTHY",
            "message": "Operation history could not be completed safely. No further writes are allowed; restart the daemon and inspect operation status.",
        }
        try:
            rows = self.journal.items(ident)
            uncertain = any(row["status"] == "running" for row in rows)
            for row in rows:
                if row["status"] in {"running", "queued"}:
                    self.journal.item_status(
                        ident,
                        row["n"],
                        "unknown" if row["status"] == "running" else "interrupted",
                        {"code": "JOURNAL_UNHEALTHY"},
                    )
            self.journal.set_status(
                ident, "unknown" if uncertain else "interrupted", {"code": "JOURNAL_UNHEALTHY"}
            )
        except Exception:
            # A full/unavailable disk may also reject the uncertainty marker. The
            # in-memory health latch still blocks writes; startup marks running
            # journal rows unknown when persistence becomes available again.
            pass

    def require_capability(self, *methods: str) -> None:
        session = self.daemon.session
        if session is None:
            raise self.daemon._not_connected()
        present = session.capabilities.get("methods", [])
        if (
            not session.hello.get("runtimeEpoch")
            or any(m not in present for m in methods)
            or (
                any(m.startswith("x.") for m in methods)
                and not session.capabilities.get("experiment")
            )
        ):
            raise BlockedError(
                "This feature needs the 1.4.0 add-on. Run tbmcp install-addon.",
                code="UNSUPPORTED_CAPABILITY",
            )

    async def call(self, action: str, params: dict) -> dict:
        j = self.journal
        if action == "status":
            await self._reconcile(params["operationId"])
            return self.status(params["operationId"], params.get("cursor"), params.get("limit", 50))
        if action == "list":
            return self.list(params.get("cursor"), params.get("limit", 20))
        if action == "cancel":
            ident = params["operationId"]
            op = j.operation(ident)
            too_late = op["kind"] in SUBMIT_METHODS and op["status"] != "queued"
            if not too_late:
                with j.db:
                    j.db.execute("UPDATE operations SET cancel=1 WHERE id=?", (ident,))
            return {**self.status(ident), "cancellationTooLate": too_late}
        if action == "plan":
            self.require_healthy()
            kind = params.get("kind")
            if kind not in PLAN_METHODS:
                raise UsageError("Unknown plan kind.")
            self.require_capability(PLAN_METHODS[kind], STEP_METHODS[kind])
            if kind == "batch" and (params.get("params") or {}).get("action") == "move":
                self.require_capability("x.batch.move")
            if kind == "attachments":
                self.require_capability(
                    "messages.attachmentsPrepare", "messages.attachmentsReconcile"
                )
            result = await self.daemon._addon_call(
                PLAN_METHODS[kind], params.get("params") or {}, timeout=180
            )
            ident = j.plan(kind, result)
            return {k: v for k, v in result.items() if k != "items"} | {
                "planId": ident,
                "expiresAt": j.now() + PLAN_TTL,
            }
        if action == "apply":
            plan_id = params["planId"]
            old = j.db.execute(
                "SELECT id FROM operations WHERE key=?", ("plan:" + plan_id,)
            ).fetchone()
            if old:
                return self.status(old[0])
            self.require_healthy()
            plan = j.get_plan(plan_id)
            self.require_capability(STEP_METHODS[plan["kind"]])
            if plan["kind"] == "batch" and any(
                item.get("action") == "move" for item in plan["items"]
            ):
                self.require_capability("x.batch.move")
            ident, fresh = j.create(
                "plan:" + plan_id, plan["kind"], digest(plan["items"]), plan["items"]
            )
            if fresh:
                self._start(ident, self._run_items(ident, STEP_METHODS[plan["kind"]]))
            return self.status(ident)
        if action == "undo":
            source_id = params["operationId"]
            j.purge()
            old = j.db.execute(
                "SELECT id FROM operations WHERE key=?", ("undo:" + source_id,)
            ).fetchone()
            if old:
                return self.status(old[0])
            self.require_healthy()
            source = j.operation(source_id)
            if source["kind"] != "batch" or source["status"] not in TERMINAL:
                raise UsageError(
                    "Only completed batch operations can be undone.", code="UNDO_UNAVAILABLE"
                )
            self.require_capability("messages.batchUndo")
            tasks = [
                {"item": item["task"], "previousResult": item["result"]}
                for item in j.items(source_id)
                if item["status"] == "succeeded"
            ]
            if not tasks:
                raise UsageError(
                    "No verified successful items can be undone.", code="UNDO_UNAVAILABLE"
                )
            if any(task["item"].get("action") == "move" for task in tasks):
                self.require_capability("x.batch.move")
            ident, fresh = j.create("undo:" + source_id, "undo", digest(tasks), tasks)
            if fresh:
                self._start(ident, self._run_items(ident, "messages.batchUndo"))
            return self.status(ident)
        if action == "submit":
            return await self.submit(params)
        raise UsageError("Unknown operation action.")

    async def draft_get(self, params: dict) -> dict:
        self.require_capability("compose.draftGet")
        ident = params.get("draftRef")
        args = (
            {"reference": self.journal.resolve(ident)}
            if ident
            else {"messageId": params.get("messageId")}
        )
        result = await self.daemon._addon_call("compose.draftGet", args, timeout=120)
        if result.get("reference"):
            result["draftRef"] = self.journal.remember(result["reference"], ident)
        return result

    async def submit(self, params: dict) -> dict:
        kind = params.get("kind")
        if kind not in SUBMIT_METHODS:
            raise UsageError("Unknown operation kind.")
        method = SUBMIT_METHODS[kind]
        args = dict(params.get("params") or {})
        key = params.get("requestKey")
        if key is None:
            if kind in {"draft_update", "draft_send"}:
                raise UsageError("A saved-draft mutation requires an explicit request key.")
            key = secrets.token_urlsafe(24)
        if not isinstance(key, str) or not key.strip() or len(key) > 256:
            raise UsageError("request_key must contain 1-256 characters.")
        request_hash = digest({"kind": kind, "params": args})
        draft_ref = args.get("draftRef")
        # A replay must work even when the reference has since moved or expired.
        self.journal.purge()
        stored_key = "request:" + digest(key)
        old = self.journal.db.execute(
            "SELECT id,hash FROM operations WHERE key=?", (stored_key,)
        ).fetchone()
        if old:
            if old["hash"] != request_hash:
                raise UsageError(
                    "Request key was already used for different content.", code="REQUEST_KEY_REUSED"
                )
            return self.status(old["id"])
        self.require_healthy()
        if kind in {"send", "reply", "forward"} and params.get("requestKey") is None:
            # The existing unkeyed tools remain usable with a 1.3 add-on. Their
            # dispatch is still journalled and serialized, but an absent runtime
            # receipt can never justify replaying an uncertain send.
            session = self.daemon.session
            if session is None:
                raise self.daemon._not_connected()
            if method not in session.capabilities.get("methods", []):
                raise BlockedError(
                    "The connected add-on does not support this compose tool.",
                    code="UNSUPPORTED_CAPABILITY",
                )
        else:
            self.require_capability(method, "operations.receipt")
        if kind in {"draft_update", "draft_send"}:
            features = self.daemon.session.capabilities.get("features", {})
            if "draftEditNative" in features:
                supported = features["draftEditNative"]
                if supported is None:
                    # The first hello can precede the bounded native probes.
                    info = await self.daemon._addon_call("x.admin.appInfo", {}, timeout=15)
                    supported = info.get("draftEditorSupported") is True
                    features["draftEditNative"] = supported
                if not supported:
                    raise BlockedError(
                        "This Thunderbird cannot safely automate its native draft editor. "
                        "Use a fixed Thunderbird build (128 ESR: 128.4.3 or later) with the privileged add-on enabled.",
                        code="UNSUPPORTED_CAPABILITY",
                    )
        if draft_ref:
            args["reference"] = self.journal.resolve(draft_ref)
        subject_hash = (
            digest([stable_reference(args["reference"]), args.get("expectedRevision")])
            if kind == "draft_send"
            else None
        )
        ident, fresh = self.journal.create(
            stored_key,
            kind,
            request_hash,
            [{"draftRef": draft_ref}] if draft_ref else [{}],
            subject_hash,
        )
        if fresh:
            self._start(ident, self._run_submit(ident, method, args, key, draft_ref))
        # Compose callers historically receive a completed result. Shield the job:
        # a caller timing out must not cancel a send already dispatched.
        task = self.tasks.get(ident)
        if task:
            try:
                await asyncio.wait_for(asyncio.shield(task), 230)
            except TimeoutError:
                pass
        return self.status(ident)

    async def _run_submit(
        self, ident: str, method: str, args: dict, key: str, draft_ref: str | None
    ) -> None:
        j = self.journal
        try:
            async with self.daemon._write_lock:
                self.require_healthy()
                if j.operation(ident)["cancel"]:
                    j.item_status(ident, 0, "cancelled")
                    j.set_status(ident, "cancelled")
                    return
                j.set_status(ident, "running")
                j.item_status(ident, 0, "running")
                result = await self.daemon._guarded_call(
                    method,
                    {**args, "operationId": ident, "itemId": 0, "requestKey": key},
                    timeout=240,
                )
                result.setdefault("status", "succeeded")
                if (
                    method in {"compose.send", "compose.reply", "compose.forward"}
                    and result["status"] == "succeeded"
                    and args.get("mode") in {"send", "later"}
                ):
                    # Older handlers return only after native completion and do
                    # not include the explicit outcome fields added in 1.4.
                    result.setdefault("sent", args["mode"] == "send")
                    result.setdefault("queued", args["mode"] == "later")
                if (
                    method in {"compose.send", "compose.reply", "compose.forward"}
                    and result["status"] == "succeeded"
                    and args.get("mode") == "draft"
                ):
                    result.update(saved=True, sent=False, queued=False)
                if result.get("reference"):
                    result["draftRef"] = j.remember(result["reference"], draft_ref)
                j.item_status(ident, 0, result["status"], result)
                j.set_status(ident, result["status"], result)
        except sqlite3.Error:
            self._worker_failed(ident)
        except Exception as exc:
            status = self._error_status(exc)
            result = self._error_receipt(exc)
            j.item_status(ident, 0, status, result)
            j.set_status(ident, status, result)

    @staticmethod
    def _error_receipt(exc: Exception) -> dict:
        result = {"code": getattr(exc, "code", None) or "OPERATION_FAILED"}
        extra = getattr(exc, "extra", {})
        if isinstance(extra.get("recoveryWindowOpen"), bool):
            result["recoveryWindowOpen"] = extra["recoveryWindowOpen"]
            if extra["recoveryWindowOpen"]:
                result["reason"] = (
                    "The helper's compose window remains open with recoverable changes. "
                    "Review and save or close that window in Thunderbird; autosave is paused."
                )
        if type(extra.get("tabId")) is int:
            result["tabId"] = extra["tabId"]
        return result

    @staticmethod
    def _error_status(exc: Exception) -> str:
        if isinstance(exc, TransportError) or getattr(exc, "code", None) in {
            "OUTCOME_UNKNOWN",
            "TIMEOUT",
            "NATIVE_TIMEOUT",
        }:
            return "unknown"
        return (
            "failed"
            if isinstance(exc, TbmcpError) and exc.kind in {"usage", "blocked", "unsupported"}
            else "unknown"
        )

    async def _run_items(self, ident: str, method: str) -> None:
        j = self.journal
        async with self.daemon._write_lock:
            self.require_healthy()
            j.set_status(ident, "running")
            interrupted = False
            for row in j.items(ident):
                if interrupted or j.operation(ident)["cancel"]:
                    j.item_status(ident, row["n"], "interrupted" if interrupted else "cancelled")
                    continue
                if method == "messages.attachmentsSave":
                    try:
                        self.require_capability("messages.attachmentsPrepare")
                        prepared = await self.daemon._addon_call(
                            "messages.attachmentsPrepare", {"item": row["task"]}, timeout=180
                        )
                        task = prepared.get("item")
                        if (
                            not isinstance(task, dict)
                            or not task.get("expectedSha256")
                            or not isinstance(task.get("expectedBytes"), int)
                        ):
                            raise UsageError(
                                "Attachment preparation did not return a content fingerprint.",
                                code="ATTACHMENT_NOT_PREPARED",
                            )
                        # The hash must be durable before a byte is written to disk.
                        j.item_task(ident, row["n"], task)
                        row["task"] = task
                    except sqlite3.Error:
                        raise
                    except Exception as exc:
                        j.item_status(
                            ident,
                            row["n"],
                            "failed",
                            {"code": getattr(exc, "code", None) or "ATTACHMENT_NOT_PREPARED"},
                        )
                        continue
                    if j.operation(ident)["cancel"]:
                        j.item_status(ident, row["n"], "cancelled")
                        continue
                j.item_status(ident, row["n"], "running")
                args = {"operationId": ident, "itemId": row["n"]}
                if method == "messages.batchUndo":
                    args.update(row["task"])
                else:
                    args["item"] = row["task"]
                try:
                    result = await self.daemon._guarded_call(method, args, timeout=180)
                    status = result.get("status", "succeeded")
                    if status not in {"succeeded", "failed", "conflict", "unknown", "skipped"}:
                        status = "unknown"
                    j.item_status(ident, row["n"], status, result)
                except sqlite3.Error:
                    raise
                except Exception as exc:
                    status = self._error_status(exc)
                    j.item_status(
                        ident,
                        row["n"],
                        status,
                        {"code": getattr(exc, "code", None) or "OPERATION_FAILED"},
                    )
                interrupted = status == "unknown"
            j.set_status(ident, self._batch_status(j.items(ident)))

    @staticmethod
    def _batch_status(items: list[dict]) -> str:
        counts = Counter(row["status"] for row in items)
        if counts["unknown"]:
            return "unknown"
        if counts["interrupted"]:
            return "interrupted"
        if counts["cancelled"]:
            return "cancelled"
        if counts["failed"] or counts["conflict"]:
            return "partial" if counts["succeeded"] else "failed"
        return "succeeded"

    async def _reconcile(self, ident: str) -> None:
        """Read receipts/files only. An absent receipt never authorizes a replay."""
        op = self.journal.operation(ident)
        session = self.daemon.session
        if op["status"] not in TERMINAL or session is None:
            return
        kind = op["kind"]
        method = (
            SUBMIT_METHODS.get(kind)
            or STEP_METHODS.get(kind)
            or ("messages.batchUndo" if kind == "undo" else None)
        )
        available = session.capabilities.get("methods", [])
        if not method:
            return
        changed = False
        latest = None
        for row in self.journal.items(ident):
            if row["status"] != "unknown":
                continue
            result = None
            if "operations.receipt" in available:
                try:
                    answer = await self.daemon._addon_call(
                        "operations.receipt",
                        {
                            "operationId": ident,
                            "itemId": row["n"],
                            "method": method,
                        },
                        timeout=15,
                    )
                    if answer.get("found") and not answer.get("pending"):
                        candidate = answer.get("result")
                        if isinstance(candidate, dict) and candidate.get("status", "succeeded") in {
                            "succeeded",
                            "failed",
                            "conflict",
                            "skipped",
                        }:
                            result = {**candidate, "status": candidate.get("status", "succeeded")}
                        elif answer.get("error"):
                            error = from_wire(method, answer["error"])
                            if self._error_status(error) == "failed":
                                result = {
                                    "status": "failed",
                                    "code": error.code or "OPERATION_FAILED",
                                }
                except Exception:
                    pass
            if (
                result is None
                and kind == "attachments"
                and "messages.attachmentsReconcile" in available
                and row["task"].get("expectedSha256")
            ):
                try:
                    candidate = await self.daemon._addon_call(
                        "messages.attachmentsReconcile", {"item": row["task"]}, timeout=180
                    )
                    if candidate.get("status") == "succeeded":
                        result = candidate
                except Exception:
                    pass
            if result is not None:
                if result.get("reference") and kind in SUBMIT_METHODS:
                    result["draftRef"] = self.journal.remember(
                        result["reference"], row["task"].get("draftRef")
                    )
                self.journal.item_status(ident, row["n"], result["status"], result)
                latest = result
                changed = True
        if changed:
            if kind in SUBMIT_METHODS:
                self.journal.set_status(ident, latest["status"], latest)
            else:
                self.journal.set_status(ident, self._batch_status(self.journal.items(ident)))

    def status(self, ident: str, cursor=None, limit=50) -> dict:
        op = self.journal.operation(ident)
        rows = self.journal.items(ident)
        start = self._cursor(cursor, ident)
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise UsageError("limit must be 1-500.")
        selected = rows[start : start + limit]
        result = json.loads(op["result"] or "{}")
        return {
            **result,
            "operationId": ident,
            "kind": op["kind"],
            "status": op["status"],
            "createdAt": op["created"],
            "expiresAt": op["created"] + RETENTION,
            "counts": dict(Counter(r["status"] for r in rows)),
            "items": [{"itemId": r["n"], **r["result"], "status": r["status"]} for r in selected],
            "nextCursor": f"{ident}:{start + limit}" if start + limit < len(rows) else None,
            "hasMore": start + limit < len(rows),
            "cancelRequested": bool(op["cancel"]),
        }

    @staticmethod
    def _cursor(cursor, ident: str) -> int:
        if cursor is None:
            return 0
        try:
            prefix, number = cursor.rsplit(":", 1)
            if prefix != ident or int(number) < 0:
                raise ValueError
            return int(number)
        except (ValueError, AttributeError):
            raise UsageError("Invalid operation cursor.", code="INVALID_CURSOR") from None

    def list(self, cursor=None, limit=20) -> dict:
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise UsageError("limit must be 1-100.")
        self.journal.purge()
        # Lexicographic keyset over immutable creation time/id, not a shifting offset.
        before = float("inf")
        before_id = "~"
        if cursor:
            try:
                before_s, before_id = cursor.split(":", 1)
                before = float(before_s)
            except (ValueError, AttributeError):
                raise UsageError("Invalid history cursor.") from None
        rows = self.journal.db.execute(
            "SELECT id,created FROM operations WHERE created<? OR (created=? AND id<?) ORDER BY created DESC,id DESC LIMIT ?",
            (before, before, before_id, limit + 1),
        ).fetchall()
        items = [self.status(r["id"], limit=1) for r in rows[:limit]]
        last = rows[limit - 1] if len(rows) > limit else None
        return {
            "items": items,
            "count": len(items),
            "nextCursor": f"{last['created']}:{last['id']}" if last else None,
        }

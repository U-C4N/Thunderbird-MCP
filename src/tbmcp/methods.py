"""Conservative method effects. Read-only is not necessarily replay-safe."""

READ_METHODS = frozenset(
    [
        "accounts.list",
        "accounts.getDefault",
        "identities.list",
        "x.accounts.list",
        "x.accounts.copiesAndFolders",
        "x.accounts.identities",
        "x.identities.get",
        "x.calendar.listItems",
        "x.calendar.getItem",
        "compose.status",
        "contacts.search",
        "addressbooks.list",
        "mailinglists.list",
        "folders.query",
        "search.global",
        "search.conversation",
        "messages.query",
        "messages.list",
        "messages.read",
        "messages.readMany",
        "messages.raw",
        "messages.listAttachments",
        "messages.followups",
        "folders.list",
        "folders.get",
        "folders.capabilities",
        "folders.getUnified",
        "contacts.query",
        "contacts.list",
        "contacts.get",
        "addressBooks.list",
        "mailingLists.list",
        "tags.list",
        "compose.outbox",
        "compose.draftGet",
        "messages.batchPreview",
        "messages.attachmentsPlan",
        "operations.receipt",
        "messages.attachmentsPrepare",
        "messages.attachmentsReconcile",
        "x.prefs.get",
        "x.prefs.getMany",
        "x.prefs.list",
        "x.prefs.userSet",
        "x.prefs.describe",
        "x.accounts.serverSettings",
        "x.accounts.junkSettings",
        "x.accounts.folderSettings",
        "x.accounts.syncSettings",
        "x.identities.list",
        "x.identities.getFull",
        "x.smtp.list",
        "x.filters.list",
        "x.filters.get",
        "x.calendar.listCalendars",
        "x.calendar.listEvents",
        "x.calendar.getEvent",
        "x.calendar.listTasks",
        "x.junk.getSettings",
        "x.openpgp.listKeys",
        "x.vfolders.list",
        "x.gloda.search",
        "x.gloda.conversation",
        "x.gloda.stats",
        "x.files.stat",
        "x.files.read",
        "x.admin.appInfo",
        "x.admin.diagnostics",
        "x.admin.consoleMessages",
        "x.admin.addons",
        "daemon.status",
        "daemon.events",
        "daemon.waitForThunderbird",
        "daemon.operations.status",
        "daemon.operations.list",
        "daemon.drafts.get",
    ]
)

KEYED_METHODS = frozenset(
    {
        "daemon.operations.apply",
        "daemon.operations.undo",
        "daemon.operations.submit",
        "daemon.operations.cancel",
    }
)


def is_read(method: str) -> bool:
    return method in READ_METHODS


def replay_safe(method: str, params: dict) -> bool:
    if method in KEYED_METHODS:
        return method != "daemon.operations.submit" or bool(params.get("requestKey"))
    if method not in READ_METHODS:
        return False
    # Thunderbird list cursors consume the underlying list. A lost response
    # must not cause a second continueList and skip a page.
    return not (method in {"messages.query", "messages.list"} and params.get("cursor"))

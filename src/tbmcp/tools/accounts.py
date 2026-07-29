"""Accounts, identities and outgoing servers — Thunderbird's account configuration.

Opt-in toolset. Only `account_list` degrades to the official API; everything else
needs the privileged half of the add-on, because the official MailExtension API
exposes account names and identities and nothing about servers.

No tool here reads or writes a credential. Passwords stay in Thunderbird's login
manager, which this bridge never touches — after changing a hostname, port or
username the user may have to re-authenticate in Thunderbird itself.
"""

# NOTE: no `from __future__ import annotations` in toolset modules. Tool signatures
# must evaluate at definition time so `Gate(...)` produces a real
# `Annotated[Consent, Resolve(...)]` rather than a string the SDK has to re-evaluate.

from typing import Any, Literal

from ..errors import TbmcpError, UsageError
from ..safety import (
    DESTRUCTIVE,
    IDEMPOTENT_WRITE,
    MUTATING,
    Gate,
    guard_write,
    require,
)
from ..server import Registrar
from ._common import call, changed, dry_run, page

# The privileged half validates settings too — it is the only layer with real
# privilege — but rejecting an unknown name here saves a round trip, and the error
# lists the alternatives, which is the whole point.

SERVER_SETTINGS: tuple[str, ...] = (
    "hostname",
    "port",
    "username",
    "socketType",
    "authMethod",
    "prettyName",
    "doBiff",
    "biffMinutes",
    "downloadOnBiff",
    "loginAtStartUp",
    "limitOfflineMessageSize",
    "maxMessageSize",
    "emptyTrashOnExit",
    "useIdle",
    "maximumConnectionsNumber",
    "forceSelect",
    "cleanupInboxOnExit",
)

JUNK_SETTINGS: tuple[str, ...] = (
    "level",
    "moveOnSpam",
    "moveTargetMode",
    "actionTargetAccount",
    "actionTargetFolder",
    "purge",
    "purgeInterval",
    "useWhiteList",
    "whiteListAbURI",
    "manualMark",
    "markAsReadOnSpam",
)

FOLDER_SETTINGS: tuple[str, ...] = (
    "fccFolder",
    "doFcc",
    "draftFolder",
    "stationeryFolder",
    "archiveFolder",
    "archiveEnabled",
    "archiveGranularity",
    "archiveKeepFolderStructure",
)

SYNC_SETTINGS: tuple[str, ...] = (
    "offlineDownload",
    "autoSyncOfflineStores",
    "downloadBodiesOnGetNewMail",
    "limitOfflineMessageSize",
    "maxMessageSize",
    "offlineSupportLevel",
    "doBiff",
    "biffMinutes",
    "downloadOnBiff",
    "useIdle",
    "leaveMessagesOnServer",
    "deleteMailLeftOnServer",
    "numDaysToLeaveOnServer",
    "headersOnly",
)

SocketType = Literal["plain", "starttls", "tls"]
AuthMethod = Literal["none", "password-cleartext", "password-encrypted", "gssapi", "ntlm", "oauth2"]


def _setting(value: str, allowed: tuple[str, ...], *, field: str = "setting") -> str:
    """Resolve a setting name, forgiving the casing a model is likely to invent.

    The wire names are the XPCOM property names, which are camelCase; models reach
    for `login_at_start_up` about half the time.
    """
    wanted = str(value or "").replace("_", "").replace("-", "").lower()
    for name in allowed:
        if name.lower() == wanted:
            return name
    raise UsageError(
        f"{field} must be one of: {', '.join(allowed)}; got {value!r}. "
        "The matching get tool shows the current values."
    )


def _rows(result: Any, *keys: str) -> list[dict[str, Any]]:
    """Pull the list out of a bridge reply without pinning one key name."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in keys:
            value = result.get(key)
            if isinstance(value, list):
                return value
    return []


def _changes(**fields: Any) -> dict[str, Any]:
    """Only what the caller actually supplied — omitted fields must stay untouched.

    An empty string is a real value (it clears a field), so `None` is the only
    "leave alone" marker.
    """
    return {name: value for name, value in fields.items() if value is not None}


def _applied(target: str, result: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    """Write envelope for the privileged setters, which all report previous/current."""
    payload = result or {}
    out = changed(target, before=payload.get("previous"), after=payload.get("current"), **extra)
    for name in ("restartRequired", "note", "warnings"):
        value = payload.get(name)
        if value:
            out[name] = value
    return out


def _account_row(account: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """The official view of one account, keyed the way the privileged half keys it.

    `MailAccount.id` and `nsIMsgAccount.key` are the same string ("account1"), which
    is what makes the merge below a plain dictionary update.
    """
    key = account.get("id") or account.get("key")
    return key, {
        "key": key,
        "name": account.get("name"),
        "type": account.get("type"),
        "identities": [
            {
                "key": identity.get("id") or identity.get("key"),
                "email": identity.get("email"),
                "name": identity.get("name"),
            }
            for identity in account.get("identities") or []
        ],
    }


def register(reg: Registrar) -> None:
    # ------------------------------------------------------------------- accounts

    @reg.read_tool(title="List accounts")
    async def account_list(include_settings: bool = True) -> dict[str, Any]:
        """List the mail accounts and how each one is configured.

        The official API supplies names, types and identities; the privileged half
        adds host, port, security and check-for-mail settings. The `key` of each
        account (`account1`) is what every other tool in this toolset takes. No
        password is read.
        """
        official = await call(
            # Folder trees belong to folder_list; asking for them here would dwarf
            # the settings the caller came for.
            "accounts.list",
            {"includeFolders": False},
            timeout=45.0,
        )
        rows: dict[str | None, dict[str, Any]] = {}
        for account in _rows(official, "accounts", "items"):
            key, row = _account_row(account)
            rows[key] = row

        note: str | None = None
        if include_settings:
            try:
                settings = await call("x.accounts.list", timeout=45.0)
            except TbmcpError as exc:
                # Half an answer beats none: names and identities are still enough to
                # pick an account key and try again once the add-on is fixed.
                note = (
                    f"Server settings are unavailable ({exc}), so this is the official "
                    "view only. Run `tbmcp doctor` to check the privileged half of the "
                    "add-on."
                )
            else:
                for extra in _rows(settings, "accounts", "items"):
                    key = extra.get("key") or extra.get("id")
                    row = rows.setdefault(key, {"key": key})
                    for name, value in extra.items():
                        if name not in ("key", "id"):
                            # setdefault, not update: the official name and type are
                            # the ones the user sees in Thunderbird's UI.
                            row.setdefault(name, value)

        return page(list(rows.values()), **({"note": note} if note else {}))

    @reg.read_tool(title="Get incoming server settings")
    async def account_get_server(account_key: str) -> dict[str, Any]:
        """Read one account's incoming server settings.

        Host, port, connection security, authentication method and the
        check-for-new-mail settings. Passwords are never read; the login manager is
        out of reach of this bridge.
        """
        result = await call("x.accounts.serverSettings", {"accountKey": account_key}, timeout=30.0)
        return {"accountKey": account_key, **(result or {})}

    @reg.write_tool(title="Change an incoming server setting", annotations=IDEMPOTENT_WRITE)
    async def account_set_server(
        account_key: str,
        setting: str,
        value: str | int | bool,
        confirm: bool = False,
        consent: Gate("change an incoming mail server setting") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Change one incoming server setting. Getting the connection wrong stops mail.

        `setting` is one of: `hostname`, `port`, `username`, `socketType`,
        `authMethod`, `prettyName` (the account's display name), `doBiff`,
        `biffMinutes`, `downloadOnBiff`, `loginAtStartUp`,
        `limitOfflineMessageSize`, `maxMessageSize`, `emptyTrashOnExit`, and for IMAP
        `useIdle`, `maximumConnectionsNumber`, `forceSelect`, `cleanupInboxOnExit`.

        `socketType` takes `plain`, `starttls` or `tls`; `authMethod` takes `none`,
        `password-cleartext`, `password-encrypted`, `gssapi`, `ntlm` or `oauth2`. The
        account type itself cannot be changed. Passwords are never written: after a
        hostname or username change Thunderbird will ask the user to sign in again.
        """
        guard_write("change server settings")
        name = _setting(setting, SERVER_SETTINGS)
        params = {"accountKey": account_key, "setting": name, "value": value}
        if dry_run_only:
            return dry_run("x.accounts.setServerSetting", params)
        require(consent, "change this server setting")
        result = await call("x.accounts.setServerSetting", params, timeout=60.0)
        return _applied(
            f"account {account_key} server.{name}", result, accountKey=account_key, setting=name
        )

    @reg.read_tool(title="Get junk mail settings")
    async def account_get_junk(account_key: str) -> dict[str, Any]:
        """Read one account's junk-mail handling: level, whitelist, move and purge rules."""
        result = await call("x.accounts.junkSettings", {"accountKey": account_key}, timeout=30.0)
        return {"accountKey": account_key, **(result or {})}

    @reg.write_tool(title="Change a junk mail setting", annotations=IDEMPOTENT_WRITE)
    async def account_set_junk(
        account_key: str,
        setting: str,
        value: str | int | bool,
        confirm: bool = False,
        consent: Gate("change an account's junk mail settings") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Change one junk-mail setting for an account.

        `setting` is one of: `level`, `moveOnSpam`, `moveTargetMode`,
        `actionTargetAccount`, `actionTargetFolder`, `purge`, `purgeInterval`,
        `useWhiteList`, `whiteListAbURI`, `manualMark`, `markAsReadOnSpam`.
        Folder settings take a folder URI, which `account_get_junk` reports.
        """
        guard_write("change junk settings")
        name = _setting(setting, JUNK_SETTINGS)
        params = {"accountKey": account_key, "setting": name, "value": value}
        if dry_run_only:
            return dry_run("x.accounts.setJunkSetting", params)
        require(consent, "change these junk settings")
        result = await call("x.accounts.setJunkSetting", params, timeout=60.0)
        return _applied(
            f"account {account_key} junk.{name}", result, accountKey=account_key, setting=name
        )

    @reg.read_tool(title="Get copies and folders settings")
    async def account_get_folders(
        account_key: str, identity_key: str | None = None
    ) -> dict[str, Any]:
        """Read where an identity files sent mail, drafts, templates and archives.

        These live on the identity, not the server, so an account with two identities
        can file its mail in two places. Omit `identity_key` for the account's default
        identity.
        """
        result = await call(
            "x.accounts.copiesAndFolders",
            {"accountKey": account_key, "identityKey": identity_key},
            timeout=30.0,
        )
        return {"accountKey": account_key, "identityKey": identity_key, **(result or {})}

    @reg.write_tool(title="Change a copies and folders setting", annotations=IDEMPOTENT_WRITE)
    async def account_set_folders(
        account_key: str,
        setting: str,
        value: str | int | bool,
        identity_key: str | None = None,
        confirm: bool = False,
        consent: Gate("change where an account files sent mail and drafts") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Change where an identity files sent mail, drafts, templates or archives.

        `setting` is one of: `fccFolder` (sent), `doFcc`, `draftFolder`,
        `stationeryFolder` (templates), `archiveFolder`, `archiveEnabled`,
        `archiveGranularity`, `archiveKeepFolderStructure`. Folder settings take a
        folder URI as reported by `account_get_folders`, not a folder id.
        """
        guard_write("change copies and folders settings")
        name = _setting(setting, FOLDER_SETTINGS)
        params = {
            "accountKey": account_key,
            "identityKey": identity_key,
            "setting": name,
            "value": value,
        }
        if dry_run_only:
            return dry_run("x.accounts.setCopiesAndFolders", params)
        require(consent, "change these folder settings")
        result = await call("x.accounts.setCopiesAndFolders", params, timeout=60.0)
        return _applied(
            f"account {account_key} folders.{name}",
            result,
            accountKey=account_key,
            identityKey=identity_key,
            setting=name,
        )

    @reg.read_tool(title="Get synchronisation settings")
    async def account_get_sync(account_key: str) -> dict[str, Any]:
        """Read an account's offline and synchronisation settings."""
        result = await call("x.accounts.syncSettings", {"accountKey": account_key}, timeout=30.0)
        return {"accountKey": account_key, **(result or {})}

    @reg.write_tool(title="Change a synchronisation setting", annotations=IDEMPOTENT_WRITE)
    async def account_set_sync(
        account_key: str,
        setting: str,
        value: str | int | bool,
        confirm: bool = False,
        consent: Gate("change an account's synchronisation settings") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Change one offline or synchronisation setting for an account.

        `setting` is one of: `offlineDownload`, `autoSyncOfflineStores`,
        `downloadBodiesOnGetNewMail`, `limitOfflineMessageSize`, `maxMessageSize`,
        `offlineSupportLevel`, `doBiff`, `biffMinutes`, `downloadOnBiff`, `useIdle`,
        and for POP3 `leaveMessagesOnServer`, `deleteMailLeftOnServer`,
        `numDaysToLeaveOnServer`, `headersOnly`.

        Turning offline storage on does not download anything by itself — use
        `folder_sync_offline` for that.
        """
        guard_write("change synchronisation settings")
        name = _setting(setting, SYNC_SETTINGS)
        params = {"accountKey": account_key, "setting": name, "value": value}
        if dry_run_only:
            return dry_run("x.accounts.setSyncSetting", params)
        require(consent, "change these synchronisation settings")
        result = await call("x.accounts.setSyncSetting", params, timeout=60.0)
        return _applied(
            f"account {account_key} sync.{name}", result, accountKey=account_key, setting=name
        )

    # ----------------------------------------------------------------- identities

    @reg.read_tool(title="List identities")
    async def identity_list(account_key: str | None = None) -> dict[str, Any]:
        """List the sending identities, across every account or just one.

        An identity is a from-address with its own signature, outgoing server and
        filing folders. Its `key` is what `identity_get` and `identity_set` take.
        """
        result = await call("x.accounts.identities", {"accountKey": account_key}, timeout=30.0)
        return page(_rows(result, "identities", "items"), accountKey=account_key)

    @reg.read_tool(title="Get an identity")
    async def identity_get(identity_key: str) -> dict[str, Any]:
        """Read one identity in full: addresses, signature, outgoing server, filing folders.

        Credentials are not part of this: the outgoing server is reported by key, and
        its password stays in the login manager.
        """
        result = await call("x.identities.get", {"identityKey": identity_key}, timeout=30.0)
        return {"identityKey": identity_key, **(result or {})}

    @reg.write_tool(title="Update an identity", annotations=IDEMPOTENT_WRITE)
    async def identity_set(
        identity_key: str,
        full_name: str | None = None,
        email: str | None = None,
        reply_to: str | None = None,
        organization: str | None = None,
        compose_html: bool | None = None,
        attach_vcard: bool | None = None,
        smtp_server_key: str | None = None,
        do_bcc: bool | None = None,
        bcc_list: str | None = None,
        catch_all: bool | None = None,
        confirm: bool = False,
        consent: Gate("change one of the user's sending identities") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Change an identity's addresses and composition defaults.

        Only the fields you pass are touched; pass an empty string to clear one.
        `email` is the from-address the recipient sees, so changing it can break
        replies and any filter that matches on it. `smtp_server_key` comes from
        `smtp_list`. Signatures go through `identity_set_signature`, and the sent or
        drafts folders through `account_set_folders`. Passwords are never written.
        """
        guard_write("change identities")
        fields = _changes(
            fullName=full_name,
            email=email,
            replyTo=reply_to,
            organization=organization,
            composeHtml=compose_html,
            attachVCard=attach_vcard,
            smtpServerKey=smtp_server_key,
            doBcc=do_bcc,
            doBccList=bcc_list,
            catchAll=catch_all,
        )
        if not fields:
            raise UsageError(
                "Nothing to change — pass at least one of full_name, email, reply_to, "
                "organization, compose_html, attach_vcard, smtp_server_key, do_bcc, "
                "bcc_list or catch_all."
            )
        params = {"identityKey": identity_key, **fields}
        if dry_run_only:
            return dry_run("x.identities.set", params)
        require(consent, "change this identity")
        result = await call("x.identities.set", params, timeout=60.0)
        return _applied(f"identity {identity_key}", result, identityKey=identity_key)

    @reg.write_tool(title="Set an identity's signature", annotations=IDEMPOTENT_WRITE)
    async def identity_set_signature(
        identity_key: str,
        signature: str | None = None,
        is_html: bool = False,
        file_path: str | None = None,
        attach: bool = True,
        below_quote: bool = True,
        confirm: bool = False,
        consent: Gate("replace an identity's signature") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Replace an identity's signature text, or point it at a file.

        Pass `signature` for inline text (set `is_html` when it contains markup), or
        `file_path` for a signature file on this machine — the two are mutually
        exclusive in Thunderbird. `attach=false` keeps the text but stops appending
        it; `below_quote` controls whether the signature sits under a quoted reply.
        """
        guard_write("change signatures")
        if signature is not None and file_path:
            raise UsageError(
                "Pass either signature (inline text) or file_path, not both — "
                "Thunderbird stores one or the other."
            )
        if signature is None and not file_path and attach:
            raise UsageError(
                "Nothing to set — pass signature, or file_path, or attach=false to "
                "stop appending the existing signature."
            )
        params = {
            "identityKey": identity_key,
            "signature": signature,
            "isHtml": is_html,
            "filePath": file_path,
            "attach": attach,
            "belowQuote": below_quote,
        }
        if dry_run_only:
            return dry_run("x.identities.setSignature", params)
        require(consent, "replace this signature")
        result = await call("x.identities.setSignature", params, timeout=60.0)
        return _applied(f"identity {identity_key} signature", result, identityKey=identity_key)

    # ------------------------------------------------------------ outgoing servers

    @reg.read_tool(title="List outgoing servers")
    async def smtp_list() -> dict[str, Any]:
        """List the SMTP servers, and which one is the default.

        Each server's `key` is what the other `smtp_*` tools take. Passwords are
        never read — only whether Thunderbird has one stored.
        """
        result = await call("x.smtp.list", timeout=30.0)
        default_key = None
        if isinstance(result, dict):
            default_key = result.get("defaultServerKey") or result.get("defaultKey")
        return page(_rows(result, "servers", "items"), defaultServerKey=default_key)

    @reg.write_tool(title="Add an outgoing server", annotations=MUTATING)
    async def smtp_create(
        hostname: str,
        port: int | None = None,
        username: str | None = None,
        socket_type: SocketType = "starttls",
        auth_method: AuthMethod = "password-cleartext",
        description: str | None = None,
        make_default: bool = False,
        confirm: bool = False,
        consent: Gate("add an outgoing mail server") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Add an SMTP server. Nothing sends through it until an identity points at it.

        Omit `port` to take the default for the chosen security (587 for STARTTLS,
        465 for TLS). No password is stored: Thunderbird prompts the user the first
        time the server is used, and this bridge never writes to the login manager.
        """
        guard_write("add outgoing servers")
        params = {
            "hostname": hostname,
            "port": port,
            "username": username,
            "socketType": socket_type,
            "authMethod": auth_method,
            "description": description,
            "makeDefault": make_default,
        }
        if dry_run_only:
            return dry_run("x.smtp.create", params)
        require(consent, "add this outgoing server")
        result = await call("x.smtp.create", params, timeout=60.0)
        created = result.get("server") if isinstance(result, dict) else None
        return changed(
            "smtp server",
            before=None,
            after=created or result,
            note="Point an identity at it with identity_set(smtp_server_key=...).",
        )

    @reg.write_tool(title="Update an outgoing server", annotations=IDEMPOTENT_WRITE)
    async def smtp_update(
        server_key: str,
        hostname: str | None = None,
        port: int | None = None,
        username: str | None = None,
        socket_type: SocketType | None = None,
        auth_method: AuthMethod | None = None,
        description: str | None = None,
        confirm: bool = False,
        consent: Gate("change an outgoing mail server") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Change an existing SMTP server. Only the fields you pass are touched.

        Get `server_key` from `smtp_list`. Changing the hostname or username usually
        invalidates the stored password, and the user will be asked for it on the next
        send — this tool neither reads nor writes it.
        """
        guard_write("change outgoing servers")
        fields = _changes(
            hostname=hostname,
            port=port,
            username=username,
            socketType=socket_type,
            authMethod=auth_method,
            description=description,
        )
        if not fields:
            raise UsageError(
                "Nothing to change — pass at least one of hostname, port, username, "
                "socket_type, auth_method or description."
            )
        params = {"serverKey": server_key, **fields}
        if dry_run_only:
            return dry_run("x.smtp.update", params)
        require(consent, "change this outgoing server")
        result = await call("x.smtp.update", params, timeout=60.0)
        return _applied(f"smtp server {server_key}", result, serverKey=server_key)

    @reg.write_tool(title="Delete an outgoing server", annotations=DESTRUCTIVE)
    async def smtp_delete(
        server_key: str,
        confirm: bool = False,
        consent: Gate("delete an outgoing mail server") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Remove an SMTP server. Identities using it will be left unable to send.

        Thunderbird has no undo for this, and the settings are not recoverable from
        the UI, so the reply repeats them — check `smtp_list` for identities pointing
        at this key first.
        """
        guard_write("delete outgoing servers")
        params = {"serverKey": server_key}
        if dry_run_only:
            return dry_run("x.smtp.delete", params)
        require(consent, "delete this outgoing server")
        result = await call("x.smtp.delete", params, timeout=60.0)
        payload = result if isinstance(result, dict) else {}
        return changed(
            f"smtp server {server_key}",
            before=payload.get("previous") or payload.get("server"),
            after=None,
            serverKey=server_key,
            recoverable=False,
            affectedIdentities=payload.get("affectedIdentities") or [],
        )

    @reg.write_tool(title="Set the default outgoing server", annotations=IDEMPOTENT_WRITE)
    async def smtp_set_default(
        server_key: str,
        confirm: bool = False,
        consent: Gate("change the default outgoing mail server") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Make one SMTP server the default for identities that have none of their own."""
        guard_write("change the default outgoing server")
        params = {"serverKey": server_key}
        if dry_run_only:
            return dry_run("x.smtp.setDefault", params)
        require(consent, "change the default outgoing server")
        result = await call("x.smtp.setDefault", params, timeout=60.0)
        return _applied("default smtp server", result, serverKey=server_key)

"""Address books, contacts and mailing lists.

Thunderbird 153 stores a contact as a vCard, and writing the card is the only
supported way to change one. That is an awkward thing to hand a model, so the tools
here take ordinary fields (`display_name`, `emails`, `phones`, …), let the add-on
fold them into the card that is already there, and return both a flat view and the
raw card. `vcard` is still accepted for anything the named fields do not cover.
"""

# NOTE: no `from __future__ import annotations` in toolset modules — see mail.py.

from typing import Any

from ..errors import UsageError
from ..safety import (
    DESTRUCTIVE,
    IDEMPOTENT_WRITE,
    MUTATING,
    Gate,
    guard_write,
    require,
)
from ..server import Registrar
from ._common import call, changed, clamp, dry_run, page

#: A contact photo is base64 in the payload. Past this it is pure cost — no client
#: renders a megabyte data URL usefully — so the metadata goes out without it.
PHOTO_INLINE_LIMIT = 200_000


def _fields(**values: Any) -> dict[str, Any]:
    """Keep only the fields the caller actually supplied.

    `None` means "leave this alone"; `""` and `[]` are kept, because they are how a
    caller clears a field. That distinction is the whole reason this helper exists.
    """
    return {key: value for key, value in values.items() if value is not None}


def _card_or_fields(vcard: str | None, fields: dict[str, Any], *, verb: str) -> dict[str, Any]:
    """Validate the two ways of describing a contact, which are mutually exclusive."""
    if vcard and fields:
        raise UsageError(
            "Pass either vcard or the individual fields, not both — a vCard replaces "
            "every property, so the fields would be silently dropped."
        )
    if not vcard and not fields:
        raise UsageError(
            f"Nothing to {verb} — give at least display_name or emails, or a complete vcard string."
        )
    return {"vCard": vcard, "fields": fields}


def register(reg: Registrar) -> None:
    # ------------------------------------------------------------------- contacts

    @reg.read_tool(title="Find a contact")
    async def contact_search(
        query: str,
        address_book_id: str | None = None,
        include_remote: bool = True,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Look someone up in the address book.

        This is the tool to reach for when the user says "what is X's email" — it
        matches names, addresses, phone numbers, nicknames and organisations across
        every address book, and `primaryEmail` on each result is the address
        Thunderbird itself would use. Set `include_remote=false` to skip LDAP and
        CardDAV books, which can be slow.
        """
        if not query.strip():
            raise UsageError("query is empty — pass a name, an address or part of one.")
        result = await call(
            "contacts.search",
            {
                "query": query,
                "addressBookId": address_book_id,
                "includeRemote": include_remote,
                "limit": clamp(limit, default=25, minimum=1, maximum=200, field="limit"),
            },
            timeout=60.0,
        )
        return page(
            result.get("contacts", []),
            total=result.get("matched"),
            # Worth reporting: a hit found only by the fallback scan means Thunderbird's
            # own quick search would not have found it, so the address book UI will not either.
            fallbackScan=result.get("fallbackScan"),
        )

    @reg.read_tool(title="List contacts")
    async def contact_list(
        address_book_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List contacts, across every address book unless one is named.

        For finding a particular person use `contact_search` instead. Continue with
        `cursor=nextCursor`; a cursor is only valid while the set of address books
        stays as it was.
        """
        result = await call(
            "contacts.list",
            {
                "addressBookId": address_book_id,
                "limit": clamp(limit, default=50, minimum=1, maximum=500, field="limit"),
                "cursor": cursor,
            },
            timeout=60.0,
        )
        return page(
            result.get("contacts", []),
            cursor=result.get("cursor"),
            addressBookId=address_book_id,
        )

    @reg.read_tool(title="Read a contact")
    async def contact_get(contact_id: str, include_photo: bool = False) -> dict[str, Any]:
        """Read one contact in full, including its raw vCard.

        `include_photo` adds the picture as a data URL, which is large — leave it off
        unless the picture is the point.
        """
        result = await call(
            "contacts.get", {"id": contact_id, "includePhoto": include_photo}, timeout=30.0
        )
        contact = dict(result.get("contact") or {})
        photo = contact.pop("photo", None)
        if photo:
            payload = photo.get("base64") or ""
            summary = {"contentType": photo.get("contentType"), "bytes": photo.get("bytes")}
            if len(payload) > PHOTO_INLINE_LIMIT:
                summary["omitted"] = (
                    "The photo is too large to inline. It is still attached to the "
                    "contact in Thunderbird."
                )
            else:
                media = photo.get("contentType") or "image/jpeg"
                summary["dataUrl"] = f"data:{media};base64,{payload}"
            contact["photo"] = summary
        return contact

    @reg.write_tool(title="Create a contact", annotations=MUTATING)
    async def contact_create(
        address_book_id: str,
        display_name: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        emails: list[str] | None = None,
        phones: list[str] | None = None,
        organisation: str | None = None,
        job_title: str | None = None,
        nickname: str | None = None,
        birthday: str | None = None,
        notes: str | None = None,
        vcard: str | None = None,
        confirm: bool = False,
        consent: Gate("add a contact to the address book") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Add a contact to an address book.

        The first address in `emails` becomes the preferred one. `birthday` is
        `YYYY-MM-DD`. For postal addresses or any property not named here, pass a
        complete `vcard` instead of the fields. Address book ids come from
        `addressbook_list`.
        """
        guard_write("create contacts")
        fields = _fields(
            displayName=display_name,
            firstName=first_name,
            lastName=last_name,
            emails=emails,
            phones=phones,
            organisation=organisation,
            jobTitle=job_title,
            nickname=nickname,
            birthday=birthday,
            notes=notes,
        )
        payload = _card_or_fields(vcard, fields, verb="create")
        require(consent, "create this contact")
        result = await call(
            "contacts.create", {"addressBookId": address_book_id, **payload}, timeout=60.0
        )
        return changed("contacts", before=None, after=result.get("contact"))

    @reg.write_tool(title="Update a contact", annotations=IDEMPOTENT_WRITE)
    async def contact_update(
        contact_id: str,
        display_name: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        emails: list[str] | None = None,
        phones: list[str] | None = None,
        organisation: str | None = None,
        job_title: str | None = None,
        nickname: str | None = None,
        birthday: str | None = None,
        notes: str | None = None,
        vcard: str | None = None,
        confirm: bool = False,
        consent: Gate("change this contact's details") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Change fields on an existing contact.

        Fields you omit are left alone; `""` or an empty list clears one. Passing
        `emails` or `phones` replaces the whole set, so include the addresses you
        want to keep. A `vcard` replaces the entire contact.
        """
        guard_write("change contacts")
        fields = _fields(
            displayName=display_name,
            firstName=first_name,
            lastName=last_name,
            emails=emails,
            phones=phones,
            organisation=organisation,
            jobTitle=job_title,
            nickname=nickname,
            birthday=birthday,
            notes=notes,
        )
        payload = _card_or_fields(vcard, fields, verb="change")
        require(consent, "change this contact")
        result = await call("contacts.update", {"id": contact_id, **payload}, timeout=60.0)
        return changed("contacts", before=result.get("previous"), after=result.get("current"))

    @reg.write_tool(title="Delete a contact", annotations=DESTRUCTIVE)
    async def contact_delete(
        contact_id: str,
        confirm: bool = False,
        consent: Gate("delete this contact") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Delete a contact. There is no Trash for contacts, so this cannot be undone.

        The contact also leaves every mailing list it was on. The returned `previous`
        holds its vCard, which is enough to recreate it with `contact_create`.
        """
        guard_write("delete contacts")
        if dry_run_only:
            return dry_run("contacts.delete", {"id": contact_id})
        require(consent, "delete this contact")
        result = await call("contacts.delete", {"id": contact_id}, timeout=30.0)
        return changed(
            "contacts",
            before=result.get("previous"),
            after=None,
            recoverable=False,
            note="Recreate it by passing the vCard in `previous` to contact_create.",
        )

    # -------------------------------------------------------------- address books

    @reg.read_tool(title="List address books")
    async def addressbook_list(include_counts: bool = True) -> dict[str, Any]:
        """List the address books, with how many contacts and lists each holds.

        `readOnly` books reject writes; `remote` ones are LDAP or CardDAV. Turn
        `include_counts` off if a remote book makes this slow.
        """
        result = await call("addressbooks.list", {"includeCounts": include_counts}, timeout=60.0)
        return page(result.get("books", []))

    @reg.write_tool(title="Create an address book", annotations=MUTATING)
    async def addressbook_create(
        name: str,
        confirm: bool = False,
        consent: Gate("create a new address book") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Create an empty local address book."""
        guard_write("create address books")
        if not name.strip():
            raise UsageError("name is required — give the address book a title.")
        require(consent, "create this address book")
        result = await call("addressbooks.create", {"name": name}, timeout=30.0)
        return changed("addressbooks", before=None, after=result.get("book"))

    @reg.write_tool(title="Delete an address book", annotations=DESTRUCTIVE)
    async def addressbook_delete(
        address_book_id: str,
        confirm: bool = False,
        consent: Gate("delete this address book and its contacts") = None,  # type: ignore[valid-type]
        dry_run_only: bool = False,
    ) -> dict[str, Any]:
        """Delete an address book together with all its contacts and mailing lists.

        Irreversible and usually not what the user meant — deleting one contact is
        `contact_delete`. Run with `dry_run_only=true` first to see the size of it.
        """
        guard_write("delete address books")
        if dry_run_only:
            return dry_run("addressbooks.delete", {"id": address_book_id})
        require(consent, "delete this address book")
        result = await call("addressbooks.delete", {"id": address_book_id}, timeout=60.0)
        return changed("addressbooks", before=result.get("previous"), after=None, recoverable=False)

    # -------------------------------------------------------------- mailing lists

    @reg.read_tool(title="List mailing lists")
    async def mailinglist_list(
        address_book_id: str | None = None,
        include_members: bool = False,
    ) -> dict[str, Any]:
        """List address book mailing lists, with member counts.

        These are Thunderbird's own distribution lists, not mailing lists you
        subscribe to. `include_members` returns each roster in full.
        """
        result = await call(
            "mailinglists.list",
            {"addressBookId": address_book_id, "includeMembers": include_members},
            timeout=60.0,
        )
        return page(result.get("lists", []), addressBookId=address_book_id)

    @reg.write_tool(title="Create a mailing list", annotations=MUTATING)
    async def mailinglist_create(
        address_book_id: str,
        name: str,
        nickname: str | None = None,
        description: str | None = None,
        confirm: bool = False,
        consent: Gate("create a mailing list") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Create an empty mailing list in an address book.

        `nickname` is what the user types in a compose window to address the whole
        list, so it is worth setting.
        """
        guard_write("create mailing lists")
        if not name.strip():
            raise UsageError("name is required — give the mailing list a title.")
        require(consent, "create this mailing list")
        result = await call(
            "mailinglists.create",
            {
                "addressBookId": address_book_id,
                "name": name,
                "nickName": nickname,
                "description": description,
            },
            timeout=30.0,
        )
        return changed("mailinglists", before=None, after=result.get("list"))

    @reg.write_tool(title="Add someone to a mailing list", annotations=IDEMPOTENT_WRITE)
    async def mailinglist_add_member(
        mailing_list_id: str,
        contact_id: str,
        confirm: bool = False,
        consent: Gate("add a contact to a mailing list") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Add an existing contact to a mailing list.

        If the contact lives in a different address book it is copied into the list's
        one; the result says so via `copiedToAddressBook`.
        """
        guard_write("change mailing list membership")
        require(consent, "add this contact to the mailing list")
        result = await call(
            "mailinglists.addMember",
            {"id": mailing_list_id, "contactId": contact_id},
            timeout=30.0,
        )
        return {
            "added": True,
            "copiedToAddressBook": result.get("copiedToAddressBook", False),
            "list": result.get("list"),
            "contact": result.get("contact"),
        }

    @reg.write_tool(title="Remove someone from a mailing list", annotations=IDEMPOTENT_WRITE)
    async def mailinglist_remove_member(
        mailing_list_id: str,
        contact_id: str,
        confirm: bool = False,
        consent: Gate("remove a contact from a mailing list") = None,  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        """Take a contact off a mailing list. The contact itself is left alone."""
        guard_write("change mailing list membership")
        require(consent, "remove this contact from the mailing list")
        result = await call(
            "mailinglists.removeMember",
            {"id": mailing_list_id, "contactId": contact_id},
            timeout=30.0,
        )
        return {
            "removed": True,
            "list": result.get("list"),
            "contact": result.get("contact"),
        }

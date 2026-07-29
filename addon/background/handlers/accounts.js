/* Account and identity handlers — the official API half of the accounts domain.
 *
 * What lives here is the *map*: which accounts exist, which identities they own, and
 * which root folder each one hangs off. That is what every other tool needs before it
 * can address a folder or pick a From address, and the official API answers it
 * cheaply and without privilege.
 *
 * Server configuration — hosts, ports, socket types, junk, sync, copies-and-folders —
 * is deliberately absent. A WebExtension MailAccount carries none of it, and the
 * privileged `x.accounts.*` module already owns that ground (it also has to read
 * `mail.server.<key>.hostname` by hand, because `incomingServer.hostName` reads back
 * null on 153). The two halves are merged on the Python side, keyed on the account
 * id: on this build the WebExtension account id *is* the nsIMsgAccount key, which is
 * what makes that merge possible. `accountKey` is emitted as the same string so the
 * merge does not depend on that staying true.
 */

{
  function accountsApi() {
    if (!browser.accounts) {
      throw tbxError.unsupported(
        "browser.accounts is missing — the add-on does not hold the accountsRead " +
          "permission. Reinstall with `tbmcp install-addon`."
      );
    }
    return browser.accounts;
  }

  function identitiesApi() {
    if (!browser.identities) {
      throw tbxError.unsupported(
        "browser.identities is missing — the add-on does not hold the " +
          "accountsIdentities permission. Reinstall with `tbmcp install-addon`."
      );
    }
    return browser.identities;
  }

  /** MailIdentity → the compact row every identity tool reports. */
  function identity(item) {
    if (!item) {
      return null;
    }
    return {
      id: item.id,
      email: item.email,
      name: item.name,
      label: item.label || null,
    };
  }

  /** MailAccount → summary. Folders are not summarised here; folder_list does that. */
  function account(item, defaultAccount) {
    if (!item) {
      return null;
    }
    return {
      id: item.id,
      accountKey: item.id,
      name: item.name,
      type: item.type,
      isDefault: Boolean(defaultAccount) && item.id === defaultAccount,
      identities: (item.identities || []).map(identity),
      rootFolderId: item.rootFolder ? item.rootFolder.id : null,
    };
  }

  /* `false` keeps the folder tree out of every reply. A large IMAP account otherwise
   * enumerates thousands of folders that nobody asked for, and the reply outgrows the
   * frame ceiling for no gain. */
  const NO_FOLDERS = false;

  /** Null rather than throwing: a profile with no accounts at all is legal. */
  async function defaultAccountId(api) {
    try {
      const found = await api.getDefault(NO_FOLDERS);
      return found ? found.id : null;
    } catch (ex) {
      tbxLog.debug("accounts.getDefault failed:", ex.message || ex);
      return null;
    }
  }

  async function defaultIdentityId(api, accountId) {
    try {
      const found = await api.getDefault(accountId);
      return found ? found.id : null;
    } catch (ex) {
      tbxLog.debug(`identities.getDefault(${accountId}) failed:`, ex.message || ex);
      return null;
    }
  }

  // ------------------------------------------------------------------------ accounts

  tbxRegistry.define("accounts.list", async () => {
    const api = accountsApi();
    const accounts = await api.list(NO_FOLDERS);
    const preferred = await defaultAccountId(api);
    return {
      accounts: (accounts || []).map((item) => account(item, preferred)),
      defaultAccountId: preferred,
    };
  });

  tbxRegistry.define("accounts.get", async (params) => {
    const api = accountsApi();
    const accountId = tbxUtil.need(params, "accountId", "string");
    let found = null;
    try {
      found = await api.get(accountId, NO_FOLDERS);
    } catch (ex) {
      // An unrecognised id returns null on some builds and throws on others; either
      // way the caller wants the list of ids that would have worked.
      tbxLog.debug(`accounts.get(${accountId}) threw:`, ex.message || ex);
    }
    if (!found) {
      const known = ((await api.list(NO_FOLDERS)) || []).map((a) => a.id);
      throw tbxError.usage(
        `no account with id ${accountId}. Known ids: ${known.join(", ") || "(none)"}`
      );
    }
    return { account: account(found, await defaultAccountId(api)) };
  });

  tbxRegistry.define("accounts.getDefault", async () => {
    const api = accountsApi();
    const found = await api.getDefault(NO_FOLDERS);
    if (!found) {
      throw tbxError.thunderbird(
        "this profile has no default account — add a mail account in Thunderbird first"
      );
    }
    return { account: account(found, found.id) };
  });

  // ---------------------------------------------------------------------- identities

  tbxRegistry.define("identities.list", async (params) => {
    const api = identitiesApi();
    const accountId = params.accountId ? String(params.accountId) : null;
    const items = accountId ? await api.list(accountId) : await api.list();
    /* One getDefault per account, cached. Profiles have a handful of accounts, so the
     * flag is worth far more than the round trips cost. */
    const defaults = new Map();
    const identities = [];
    for (const item of items || []) {
      const owner = item.accountId || accountId;
      if (owner && !defaults.has(owner)) {
        defaults.set(owner, await defaultIdentityId(api, owner));
      }
      identities.push(
        Object.assign(identity(item), {
          accountId: owner || null,
          isDefault: Boolean(owner) && defaults.get(owner) === item.id,
        })
      );
    }
    return { identities, accountId };
  });

  tbxRegistry.define("identities.getDefault", async (params) => {
    const api = identitiesApi();
    let accountId = params.accountId ? String(params.accountId) : null;
    if (!accountId) {
      // "the default identity" with no account named means the default account's.
      accountId = await defaultAccountId(accountsApi());
      if (!accountId) {
        throw tbxError.usage(
          "accountId is required — this profile has no default account to fall back to"
        );
      }
    }
    const found = await api.getDefault(accountId);
    return { accountId, identity: identity(found) };
  });

  tbxRegistry.define("identities.setDefault", async (params) => {
    const api = identitiesApi();
    const accountId = tbxUtil.need(params, "accountId", "string");
    const identityId = tbxUtil.need(params, "identityId", "string");
    if (typeof api.setDefault !== "function") {
      throw tbxError.unsupported(
        "this Thunderbird cannot change the default identity from an add-on " +
          "(browser.identities.setDefault is missing)"
      );
    }
    const existing = (await api.list(accountId)) || [];
    const target = existing.find((i) => i.id === identityId);
    if (!target) {
      throw tbxError.usage(
        `account ${accountId} has no identity ${identityId}. Its identities: ${
          existing.map((i) => `${i.id} (${i.email})`).join(", ") || "(none)"
        }`
      );
    }
    const previousId = await defaultIdentityId(api, accountId);
    await api.setDefault(accountId, identityId);
    const current = await api.getDefault(accountId).catch(() => target);
    return {
      accountId,
      previous: identity(existing.find((i) => i.id === previousId)),
      current: identity(current) || identity(target),
    };
  });
}

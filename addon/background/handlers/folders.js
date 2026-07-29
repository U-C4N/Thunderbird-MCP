/* Folder handlers — the official browser.folders / browser.accounts surface.
 *
 * `folders.query` carries most of the traffic, because a model needs a folder id
 * before it can do anything else. All the shaping lives here: one compact record
 * per folder, MailFolderInfo counts folded in, flat or nested on request.
 *
 * Three things learned against 153:
 *   - queryInfo.name compares a bare string for equality, so a substring search has
 *     to travel as a RegularExpressionType.
 *   - MailFolder.type is gone in favour of specialUse; we keep a single-word `type`
 *     because that is what callers actually branch on.
 *   - there is no emptyTrash/emptyJunk in the official API at all. See the bottom.
 */

{
  const DEFAULT_DEPTH = 1;
  const MAX_DEPTH = 20;
  const MAX_FOLDERS = 500;
  const INFO_CONCURRENCY = 8;
  const PURGE_CHUNK = 200;

  const UNIFIED_TYPES = ["inbox", "drafts", "sent", "trash", "templates", "archives", "junk"];

  /* Predicates that mean "search the whole tree". Anything else (accountId, isRoot)
   * is still just browsing, so the depth cap stays in force for it. */
  const SEARCH_KEYS = [
    "name",
    "path",
    "specialUse",
    "folderId",
    "capabilities",
    "isVirtual",
    "isTag",
    "isUnified",
    "isFavorite",
    "hasMessages",
    "hasUnreadMessages",
    "hasNewMessages",
    "hasSubFolders",
  ];

  function escapeRegExp(text) {
    return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  /** Depth relative to the account root: "/" is 0, "/Inbox" is 1. */
  function depthOf(folder) {
    return String(folder.path || "/")
      .split("/")
      .filter(Boolean).length;
  }

  /** queryInfo for browser.folders.query, straight from the daemon's params. */
  function buildQuery(input) {
    const query = {};
    for (const [key, value] of Object.entries(input || {})) {
      if (value === null || value === undefined || value === "") {
        continue;
      }
      query[key] = value;
    }
    if (typeof query.name === "string") {
      query.name = { regexp: escapeRegExp(query.name), flags: "i" };
    }
    return query;
  }

  /** The compact folder record the Python layer hands to the model. */
  function summary(folder, info) {
    const out = {
      id: folder.id,
      name: folder.name,
      path: folder.path,
      accountId: folder.accountId,
      type: (folder.specialUse && folder.specialUse[0]) || folder.type || null,
      specialUse: folder.specialUse || [],
      depth: depthOf(folder),
    };
    // Flags are emitted only when true: a folder listing is mostly false booleans.
    for (const flag of ["isRoot", "isVirtual", "isTag", "isUnified", "isFavorite"]) {
      if (folder[flag]) {
        out[flag] = true;
      }
    }
    if (info) {
      out.totalMessageCount = info.totalMessageCount;
      out.unreadMessageCount = info.unreadMessageCount;
      out.newMessageCount = info.newMessageCount;
      if (info.favorite) {
        out.isFavorite = true;
      }
      if (info.lastUsed) {
        // lastUsed crosses the API boundary as a Date on 153 and as a timestamp on
        // older builds; both survive this.
        const stamp = new Date(info.lastUsed);
        if (!Number.isNaN(stamp.getTime())) {
          out.lastUsed = stamp.toISOString();
        }
      }
      if (info.quota && info.quota.length) {
        out.quota = info.quota;
      }
    }
    return out;
  }

  /** A MailFolder plus whatever subFolders it already carries, with no extra calls. */
  function shallow(folder) {
    const node = summary(folder, null);
    if (folder.subFolders && folder.subFolders.length) {
      node.children = folder.subFolders.map(shallow);
    }
    return node;
  }

  function flatten(folders, out) {
    for (const folder of folders || []) {
      out.push(folder);
      flatten(folder.subFolders, out);
    }
    return out;
  }

  async function withInfo(folders) {
    const results = await tbxUtil.mapLimited(folders, INFO_CONCURRENCY, (folder) =>
      // Root folders hold no mail, so getFolderInfo on them is a wasted hop.
      folder.isRoot ? null : browser.folders.getFolderInfo(folder.id)
    );
    return folders.map((folder, index) => {
      const entry = results[index];
      return summary(folder, entry && entry.ok ? entry.value : null);
    });
  }

  /** Account roots plus `depth` levels below them, breadth first. */
  async function walkTree(query, depth) {
    const rootQuery = { isRoot: true };
    if (query.accountId) {
      rootQuery.accountId = query.accountId;
    }
    let level = (await browser.folders.query(rootQuery)) || [];
    if (!level.length) {
      // Cheap insurance: if a build ever stops returning roots from query, the
      // account list still has them.
      const accounts = (await browser.accounts.list(false)) || [];
      level = accounts
        .filter((a) => !query.accountId || a.id === query.accountId)
        .map((a) => a.rootFolder)
        .filter(Boolean);
    }
    const collected = [...level];
    for (let step = 0; step < depth && level.length && collected.length < MAX_FOLDERS; step += 1) {
      const batches = await tbxUtil.mapLimited(level, INFO_CONCURRENCY, (folder) =>
        browser.folders.getSubFolders(folder.id, false)
      );
      const next = [];
      for (const children of tbxUtil.partition(batches).values) {
        next.push(...(children || []));
      }
      collected.push(...next);
      level = next;
    }
    return collected;
  }

  function byAccountThenPath(a, b) {
    if (a.accountId !== b.accountId) {
      return a.accountId < b.accountId ? -1 : 1;
    }
    if (a.path === b.path) {
      return 0;
    }
    return a.path < b.path ? -1 : 1;
  }

  /** Nest a flat list by path. A folder whose parent is missing from the set stays
   *  at the top level, so a filtered query still returns something navigable. */
  function nest(items) {
    const byKey = new Map(items.map((item) => [`${item.accountId}${item.path}`, item]));
    const roots = [];
    for (const item of items) {
      const path = item.path || "/";
      const cut = path.lastIndexOf("/");
      const parent = path === "/" ? null : byKey.get(`${item.accountId}${cut > 0 ? path.slice(0, cut) : "/"}`);
      if (parent && parent !== item) {
        parent.children = parent.children || [];
        parent.children.push(item);
      } else {
        roots.push(item);
      }
    }
    return roots;
  }

  /** The account's folder for a specialUse ("trash", "junk", …). */
  async function specialFolder(accountId, use, required) {
    const matches = (await browser.folders.query({ accountId, specialUse: [use] })) || [];
    if (matches.length) {
      return matches[0];
    }
    if (!required) {
      return null;
    }
    throw tbxError.usage(
      `account ${accountId} has no ${use} folder configured — check Account Settings ` +
        "> Copies & Folders, or pass folderId explicitly"
    );
  }

  /** Folder ids on 153, MailFolder objects on 128. Try ids (cheaper) and fall back
   *  once, rather than branching on a version number. */
  async function callWithFolders(name, ids, tail) {
    try {
      return await browser.folders[name](...ids, ...tail);
    } catch (ex) {
      const message = String(ex.message || ex);
      if (!/MailFolder|type error|Expected|does not match/i.test(message)) {
        throw ex;
      }
      const objects = await Promise.all(ids.map((id) => browser.folders.get(id)));
      return browser.folders[name](...objects, ...tail);
    }
  }

  // --------------------------------------------------------------------- reading

  tbxRegistry.define("folders.query", async (params) => {
    const query = buildQuery(params.query);
    const limit = tbxUtil.limit(params.limit, 200, MAX_FOLDERS);
    const searching = SEARCH_KEYS.some((key) => key in query);
    const depth = Number.isInteger(params.depth)
      ? Math.min(Math.max(params.depth, 0), MAX_DEPTH)
      : null;

    let folders;
    if (searching) {
      folders = (await browser.folders.query(query)) || [];
      if (depth !== null) {
        folders = folders.filter((folder) => depthOf(folder) <= depth);
      }
    } else {
      folders = await walkTree(query, depth === null ? DEFAULT_DEPTH : depth);
    }

    // Sort before truncating, so dropping the tail is predictable rather than
    // whatever order Thunderbird happened to walk the accounts in.
    folders.sort(byAccountThenPath);
    const truncated = folders.length > limit;
    folders = folders.slice(0, limit);

    const items =
      params.includeCounts === false
        ? folders.map((folder) => summary(folder, null))
        : await withInfo(folders);
    return {
      folders: params.tree ? nest(items) : items,
      count: items.length,
      truncated,
      searched: searching,
    };
  });

  tbxRegistry.define("folders.get", async (params) => {
    const folderId = tbxUtil.need(params, "folderId", "string");
    const includeSubFolders = Boolean(params.includeSubFolders);
    const folder = await browser.folders.get(folderId, includeSubFolders);
    const info = folder.isRoot
      ? null
      : await browser.folders.getFolderInfo(folderId).catch(() => null);
    const payload = summary(folder, info);
    if (includeSubFolders && folder.subFolders && folder.subFolders.length) {
      payload.children = folder.subFolders.map(shallow);
    }
    return { folder: payload };
  });

  tbxRegistry.define("folders.capabilities", async (params) => {
    const folderId = tbxUtil.need(params, "folderId", "string");
    if (!browser.folders.getFolderCapabilities) {
      throw tbxError.unsupported(
        "this Thunderbird build has no folders.getFolderCapabilities (needs 128 or newer)"
      );
    }
    const capabilities = await browser.folders.getFolderCapabilities(folderId);
    return { folderId, capabilities: capabilities || {} };
  });

  tbxRegistry.define("folders.getUnified", async (params) => {
    const type = tbxUtil.need(params, "type", "string");
    if (!UNIFIED_TYPES.includes(type)) {
      throw tbxError.usage(`type must be one of ${UNIFIED_TYPES.join(", ")}`);
    }
    if (!browser.folders.getUnifiedFolder) {
      throw tbxError.unsupported(
        "this Thunderbird build has no unified folders (needs 128 or newer)"
      );
    }
    const includeSubFolders = Boolean(params.includeSubFolders);
    let folder;
    try {
      folder = await browser.folders.getUnifiedFolder(type, includeSubFolders);
    } catch (ex) {
      // Thunderbird throws rather than returning null when the unified folder for a
      // type has never been materialised on this profile.
      throw tbxError.unsupported(
        `there is no unified ${type} folder on this profile: ${ex.message || ex}`
      );
    }
    const info = await browser.folders.getFolderInfo(folder.id).catch(() => null);
    const payload = summary(folder, info);
    if (includeSubFolders && folder.subFolders && folder.subFolders.length) {
      payload.children = folder.subFolders.map(shallow);
    }
    return { folder: payload };
  });

  // -------------------------------------------------------------------- mutation

  /** A parent folder id, from parentId or from an account's root folder. */
  async function resolveParent(params) {
    if (params.parentId) {
      return params.parentId;
    }
    const accountId = tbxUtil.need(params, "accountId", "string");
    const account = await browser.accounts.get(accountId, false);
    if (!account || !account.rootFolder) {
      throw tbxError.usage(`no account with id ${accountId}`);
    }
    return account.rootFolder.id;
  }

  tbxRegistry.define("folders.create", async (params) => {
    const name = tbxUtil.need(params, "name", "string");
    const parentId = await resolveParent(params);
    const folder = await callWithFolders("create", [parentId], [name]);
    return { folder: summary(folder, null), parentId };
  });

  tbxRegistry.define("folders.rename", async (params) => {
    const folderId = tbxUtil.need(params, "folderId", "string");
    const newName = tbxUtil.need(params, "newName", "string");
    const before = await browser.folders.get(folderId);
    const folder = await callWithFolders("rename", [folderId], [newName]);
    return { previous: summary(before, null), folder: summary(folder, null) };
  });

  tbxRegistry.define("folders.move", async (params) => {
    const folderId = tbxUtil.need(params, "folderId", "string");
    const destinationId = tbxUtil.need(params, "destinationId", "string");
    const before = await browser.folders.get(folderId);
    const folder = await callWithFolders("move", [folderId, destinationId], []);
    return { previous: summary(before, null), folder: summary(folder, null) };
  });

  tbxRegistry.define("folders.copy", async (params) => {
    const folderId = tbxUtil.need(params, "folderId", "string");
    const destinationId = tbxUtil.need(params, "destinationId", "string");
    const folder = await callWithFolders("copy", [folderId, destinationId], []);
    return { folder: summary(folder, null), sourceFolderId: folderId };
  });

  tbxRegistry.define("folders.delete", async (params) => {
    const folderId = tbxUtil.need(params, "folderId", "string");
    const before = await browser.folders.get(folderId, true);
    const info = before.isRoot
      ? null
      : await browser.folders.getFolderInfo(folderId).catch(() => null);
    // Thunderbird moves a folder to Trash unless it already lives there, so work out
    // which of the two happened instead of leaving the caller to guess.
    const trash = await specialFolder(before.accountId, "trash", false).catch(() => null);
    const permanent = Boolean(
      trash && (before.path === trash.path || String(before.path).startsWith(`${trash.path}/`))
    );
    await browser.folders.delete(folderId);
    return {
      deleted: summary(before, info),
      subFolderCount: flatten(before.subFolders, []).length,
      permanent,
    };
  });

  tbxRegistry.define("folders.markAsRead", async (params) => {
    const folderId = tbxUtil.need(params, "folderId", "string");
    const before = await browser.folders.getFolderInfo(folderId).catch(() => null);
    const targets = [folderId];
    if (params.includeSubFolders) {
      const children = await browser.folders.getSubFolders(folderId, true);
      for (const folder of flatten(children, [])) {
        targets.push(folder.id);
      }
    }
    const results = await tbxUtil.mapLimited(targets, INFO_CONCURRENCY, (id) =>
      browser.folders.markAsRead(id)
    );
    const { values, failures } = tbxUtil.partition(results);
    const after = await browser.folders.getFolderInfo(folderId).catch(() => null);
    return {
      folderId,
      folders: values.length,
      failures,
      previousUnread: before ? before.unreadMessageCount : null,
      unread: after ? after.unreadMessageCount : null,
    };
  });

  tbxRegistry.define("folders.update", async (params) => {
    const folderId = tbxUtil.need(params, "folderId", "string");
    if (params.isFavorite === undefined || params.isFavorite === null) {
      throw tbxError.usage(
        "isFavorite is required — it is the only folder property this API can update"
      );
    }
    const before = await browser.folders.get(folderId);
    await browser.folders.update(folderId, { isFavorite: Boolean(params.isFavorite) });
    const after = await browser.folders.get(folderId);
    return { previous: summary(before, null), folder: summary(after, null) };
  });

  // ----------------------------------------------------------------- emptying out

  /* There is no folders.emptyTrash or folders.emptyJunk in the official API on 153,
   * so these do what the UI does: delete every message permanently, folder by folder,
   * and (for Trash) drop the emptied subfolders afterwards.
   *
   * Ids are collected before the first delete. Deleting invalidates the MessageList
   * we would otherwise still be paging through, and the symptom is a silent half-empty
   * folder rather than an error. */
  async function collectIds(folderId) {
    const ids = [];
    let current = await browser.messages.list(folderId);
    while (current) {
      for (const message of current.messages || []) {
        ids.push(message.id);
      }
      if (!current.id) {
        break;
      }
      current = await browser.messages.continueList(current.id);
      if (!current || !(current.messages || []).length) {
        break;
      }
    }
    return ids;
  }

  async function purge(folders, ctx, label) {
    const plan = [];
    for (const folder of folders) {
      for (const id of await collectIds(folder.id)) {
        plan.push(id);
      }
    }
    let deleted = 0;
    for (let offset = 0; offset < plan.length; offset += PURGE_CHUNK) {
      const chunk = plan.slice(offset, offset + PURGE_CHUNK);
      await browser.messages.delete(chunk, { deletePermanently: true });
      deleted += chunk.length;
      if (ctx && ctx.progress) {
        ctx.progress(deleted, plan.length, label);
      }
    }
    return deleted;
  }

  async function emptySpecial(params, ctx, use, removeSubFolders) {
    const folder = params.folderId
      ? await browser.folders.get(params.folderId)
      : await specialFolder(tbxUtil.need(params, "accountId", "string"), use, true);
    const children = (await browser.folders.getSubFolders(folder.id, true)) || [];
    const descendants = flatten(children, []);
    const deleted = await purge([folder, ...descendants], ctx, `emptying ${folder.name}`);

    let foldersRemoved = 0;
    if (removeSubFolders) {
      for (const child of children) {
        try {
          await browser.folders.delete(child.id);
          foldersRemoved += 1;
        } catch (ex) {
          // A folder the server refuses to drop is not worth failing the whole
          // operation over — its messages are already gone.
          tbxLog.warn(`could not remove ${child.path}: ${ex.message || ex}`);
        }
      }
    }
    return {
      folderId: folder.id,
      path: folder.path,
      accountId: folder.accountId,
      deleted,
      foldersRemoved,
      subFoldersEmptied: descendants.length,
    };
  }

  tbxRegistry.define("folders.emptyTrash", async (params, ctx) =>
    emptySpecial(params, ctx, "trash", params.removeSubFolders !== false)
  );

  // Junk keeps its structure: unlike Trash, its subfolders are usually filter
  // targets the user set up on purpose.
  tbxRegistry.define("folders.emptyJunk", async (params, ctx) =>
    emptySpecial(params, ctx, "junk", Boolean(params.removeSubFolders))
  );
}

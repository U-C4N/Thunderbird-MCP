/* Conservative message references shared by drafts and reversible operations.
 * Only identifiers and a digest leave this helper; subjects and addresses are
 * inputs to the digest, never fields in the persisted reference. */
var tbxReferences = (() => {
  const MAX_CANDIDATES = 2000;

  function fail(code, message) {
    return tbxError.blocked(message, [], { code });
  }

  function canonical(value) {
    if (Array.isArray(value)) {
      return value.map(canonical);
    }
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
    }
    return value;
  }

  async function digest(value) {
    const bytes = new TextEncoder().encode(JSON.stringify(canonical(value)));
    const hash = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  async function fingerprint(message) {
    const date = new Date(message.date).getTime();
    return digest({
      headerMessageId: String(message.headerMessageId || ""),
      date: Number.isFinite(date) ? date : null,
      subject: String(message.subject || ""),
      author: String(message.author || ""),
      recipients: message.recipients || [],
      ccList: message.ccList || [],
      bccList: message.bccList || [],
      size: message.size ?? null,
    });
  }

  function epoch() {
    return typeof tbxRuntimeEpoch === "string" ? tbxRuntimeEpoch : null;
  }

  async function folderOf(message) {
    const value = message.folder;
    if (!value || !value.id) {
      throw fail("REFERENCE_NO_FOLDER", "The message has no concrete folder.");
    }
    const folder = value.accountId && value.path !== undefined
      ? value
      : await browser.folders.get(value.id);
    if (!folder.accountId || folder.path === undefined) {
      throw fail("REFERENCE_NO_ACCOUNT", "The message's account cannot be identified.");
    }
    return folder;
  }

  async function candidates(reference) {
    let page = await browser.messages.query({
      folderId: reference.folderId,
      includeSubFolders: false,
      headerMessageId: reference.headerMessageId,
    });
    const found = [];
    let examined = 0;
    let pages = 0;
    let cursor = null;
    try {
      while (page) {
        if (!Array.isArray(page.messages)) {
          throw fail("REFERENCE_INVALID_LIST", "Thunderbird returned an invalid message list.");
        }
        examined += page.messages.length;
        pages += 1;
        cursor = page.id || null;
        if (examined > MAX_CANDIDATES || pages > MAX_CANDIDATES) {
          throw fail("REFERENCE_TOO_MANY", "The message reference cannot be resolved unambiguously.");
        }
        for (const message of page.messages) {
          if (message.folder && message.folder.id === reference.folderId &&
              String(message.headerMessageId || "") === reference.headerMessageId) {
            found.push(message);
          }
        }
        if (!cursor) {
          break;
        }
        page = await browser.messages.continueList(cursor);
      }
      return found;
    } finally {
      if (cursor && browser.messages.abortList) {
        await browser.messages.abortList(cursor).catch(() => {});
      }
    }
  }

  async function matching(reference) {
    const matches = [];
    for (const message of await candidates(reference)) {
      if (await fingerprint(message) === reference.fingerprint) {
        matches.push(message);
      }
    }
    return matches;
  }

  async function captureHeader(message) {
    if (!message.headerMessageId) {
      throw fail("REFERENCE_NO_MESSAGE_ID", "This message has no RFC Message-ID; a persistent reference cannot be created.");
    }
    const folder = await folderOf(message);
    const reference = {
      accountId: folder.accountId,
      folderId: folder.id,
      folderPath: folder.path,
      headerMessageId: String(message.headerMessageId),
      fingerprint: await fingerprint(message),
      runtimeEpoch: epoch(),
      messageId: message.id,
    };
    const matches = await matching(reference);
    if (matches.length !== 1 || matches[0].id !== message.id) {
      throw fail("REFERENCE_AMBIGUOUS", "This message cannot be uniquely identified in its folder.");
    }
    return reference;
  }

  async function capture(messageId) {
    return captureHeader(await browser.messages.get(messageId));
  }

  async function resolve(reference) {
    if (!reference || typeof reference !== "object" || !reference.accountId ||
        !reference.folderId || !reference.headerMessageId || !reference.fingerprint ||
        typeof reference.folderPath !== "string") {
      throw fail("REFERENCE_INVALID", "The message reference is invalid.");
    }
    let folder;
    try {
      folder = await browser.folders.get(reference.folderId);
    } catch (error) {
      throw fail("REFERENCE_FOLDER_MISSING", "The original folder is no longer available.");
    }
    if (folder.accountId !== reference.accountId || folder.path !== reference.folderPath) {
      throw fail("REFERENCE_FOLDER_CHANGED", "The original folder was changed; create a new preview.");
    }
    const matches = await matching(reference);
    if (matches.length !== 1) {
      throw fail(matches.length ? "REFERENCE_AMBIGUOUS" : "REFERENCE_NOT_FOUND",
        matches.length ? "More than one message matches this reference." : "The message changed or is no longer in its expected folder.");
    }
    return matches[0];
  }

  return { capture, captureHeader, resolve, fingerprint, digest };
})();

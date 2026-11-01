/* Batch moves use the native online path. TB128's WebExtension move hardcodes
 * allowUndo=true, which acknowledges pseudo-offline headers before the server
 * move and can later send a fake UID during a second move. Our durable journal
 * owns undo; Thunderbird's optimistic UI undo queue is deliberately not used. */
{
  TBX_MODULE_NAMES.push("batch");

  function blocked(code, message) {
    return H.blocked(message, [], { code });
  }

  function fingerprint(message) {
    if (typeof TextEncoder === "undefined") {
      Cu.importGlobalProperties(["TextEncoder"]);
    }
    const date = new Date(message.date).getTime();
    // Same canonical fields/order as background/references.js, synchronously
    // hashed so the final native header check cannot yield before dispatch.
    const value = {
      author: String(message.author || ""), bccList: message.bccList || [],
      ccList: message.ccList || [], date: Number.isFinite(date) ? date : null,
      headerMessageId: String(message.headerMessageId || ""), recipients: message.recipients || [],
      size: message.size ?? null, subject: String(message.subject || ""),
    };
    const bytes = new TextEncoder().encode(JSON.stringify(value));
    const hash = Cc["@mozilla.org/security/hash;1"].createInstance(Ci.nsICryptoHash);
    hash.init(hash.SHA256);
    hash.update(bytes, bytes.length);
    return Array.from(hash.finish(false), (c) => c.charCodeAt(0).toString(16).padStart(2, "0")).join("");
  }

  TBX_MODULES["batch.move"] = async (params, extension) => {
    if (!extension?.messageManager || !extension?.folderManager) {
      throw Object.assign(H.unsupported("Native batch moves are unavailable."), { code: "UNSUPPORTED_CAPABILITY" });
    }
    const id = H.need(params, "messageId");
    const reference = H.need(params, "reference");
    const destination = H.need(params, "destination");
    if (!Number.isInteger(id) || !reference.headerMessageId || !reference.fingerprint ||
        !reference.folderId || !reference.accountId || typeof reference.folderPath !== "string" ||
        !destination.id || !destination.accountId || typeof destination.path !== "string") {
      throw H.usage("A complete saved source and destination reference is required.");
    }
    const { getFolder } = ChromeUtils.importESModule("resource:///modules/ExtensionAccounts.sys.mjs");
    const from = getFolder(reference.folderId).folder;
    const to = getFolder(destination.id).folder;
    const fromInfo = extension.folderManager.convert(from);
    const toInfo = extension.folderManager.convert(to);
    if (fromInfo.accountId !== reference.accountId || fromInfo.path !== reference.folderPath ||
        toInfo.accountId !== destination.accountId || toInfo.path !== destination.path ||
        fromInfo.accountId !== toInfo.accountId || from.URI === to.URI) {
      throw blocked("REFERENCE_FOLDER_CHANGED", "The planned source or destination changed.");
    }
    if (from.isServer || to.isServer || (from.flags & Ci.nsMsgFolderFlags.Virtual) ||
        (to.flags & Ci.nsMsgFolderFlags.Virtual) || !from.canDeleteMessages || !to.canFileMessages) {
      throw blocked("REFERENCE_FOLDER_UNSUPPORTED", "The move requires writable concrete folders in one account.");
    }
    if (Services.io.offline && (from.server.type === "imap" || to.server.type === "imap")) {
      throw blocked("REFERENCE_OFFLINE", "Connect Thunderbird before a reversible IMAP move.");
    }
    const tracked = extension.messageManager.get(id);
    if (!tracked || tracked.folder?.URI !== from.URI || !from.msgDatabase.containsKey(tracked.messageKey)) {
      throw blocked("REFERENCE_NOT_FOUND", "The message is no longer in its expected source folder.");
    }
    const header = from.msgDatabase.getMsgHdrForKey(tracked.messageKey);
    if (!header || header.getUint32Property("pseudoHdr") ||
        (header.flags & (Ci.nsMsgMessageFlags.IMAPDeleted | Ci.nsMsgMessageFlags.Expunged))) {
      throw blocked("REFERENCE_PENDING", "Wait for Thunderbird to finish the prior move, then read the message again.");
    }
    const current = extension.messageManager.convert(header);
    if (String(current.headerMessageId || "") !== reference.headerMessageId ||
        fingerprint(current) !== reference.fingerprint) {
      throw blocked("REFERENCE_CHANGED", "The message changed since it was reviewed.");
    }
    const services = needMod("MailServices");
    const imapDestination = to.server.type === "imap"
      ? to.QueryInterface(Ci.nsIMsgImapMailFolder) : null;
    if (!services.mfn?.addListener || !services.mfn?.removeListener ||
        (imapDestination && typeof imapDestination.updateFolderWithListener !== "function")) {
      throw Object.assign(H.unsupported("Native move completion tracking is unavailable."), { code: "UNSUPPORTED_CAPABILITY" });
    }
    if (params.checkOnly === true) {
      return { supported: true };
    }
    const sourceKey = header.messageKey;
    const sourceMessageId = String(header.messageId || "");
    let notify;
    const notification = new Promise((resolve) => { notify = resolve; });
    let targetSeen = false;
    let targetAdded;
    const targetReady = new Promise((resolve) => { targetAdded = resolve; });
    function observeTarget(candidate) {
      try {
        if (candidate?.folder?.URI === to.URI && candidate.messageId === sourceMessageId &&
            !candidate.getUint32Property("pseudoHdr") &&
            fingerprint(extension.messageManager.convert(candidate)) === reference.fingerprint) {
          targetSeen = true;
          targetAdded();
        }
      } catch (_) { /* A partially loaded header cannot prove the destination. */ }
    }
    const listener = {
      msgAdded: observeTarget,
      msgsMoveCopyCompleted(isMove, originals, target, destinations) {
        if (target?.URI === to.URI && Array.from(originals || []).some((original) =>
          original.folder?.URI === from.URI && original.messageKey === sourceKey &&
          (!original.messageId || original.messageId === sourceMessageId))) {
          for (const candidate of destinations || []) observeTarget(candidate);
          notify({ isMove });
        }
      },
    };
    // TB128 suppresses WebExtension onMoved when native destination headers are
    // empty. The native event still identifies the source and destination. Bind
    // before copy, then load authoritative destination headers before mapping it.
    services.mfn.addListener(listener, services.mfn.msgsMoveCopyCompleted | services.mfn.msgAdded);
    try {
      await H.withTimeout(new Promise((resolve, reject) => {
        services.copy.copyMessages(from, [header], to, true, {
          onStartCopy() {}, onProgress() {}, setMessageKey() {}, getMessageId() { return null; },
          onStopCopy(status) {
            if (Components.isSuccessCode(status)) {
              resolve();
            } else {
              reject(Object.assign(new Error("The native move did not complete successfully."),
                { tbxKind: "thunderbird", code: "NATIVE_MOVE_FAILED" }));
            }
          },
        }, null, false /* Our journal owns undo; never create pseudo-offline headers. */);
      }), 120000, "native batch move");
      const observed = await H.withTimeout(notification, 15000, "native move notification");
      if (!observed.isMove) {
        throw Object.assign(new Error("Thunderbird copied instead of moving the message."),
          { tbxKind: "thunderbird", code: "NATIVE_UNEXPECTED_COPY" });
      }
      if (imapDestination && !targetSeen) {
        // Modern Thunderbird may already be updating this folder for its own
        // onMoved implementation. It does not attach a second URL listener to
        // an in-progress update. The prebound msgAdded listener independently
        // observes the real matching destination header in that case.
        const refreshed = new Promise((resolve, reject) => {
          imapDestination.updateFolderWithListener(null, {
            OnStartRunningUrl() {},
            OnStopRunningUrl(_url, status) {
              if (Components.isSuccessCode(status)) resolve();
              else reject(Object.assign(new Error("Destination headers could not be refreshed."),
                { tbxKind: "thunderbird", code: "NATIVE_REFRESH_FAILED" }));
            },
          });
        });
        await H.withTimeout(Promise.race([refreshed, targetReady]), 30000, "move destination refresh");
      }
      return { completed: true, nativeMoveObserved: {
        messageId: id, sourceFolderId: reference.folderId, destinationFolderId: destination.id,
      } };
    } finally {
      services.mfn.removeListener(listener);
    }
  };
}

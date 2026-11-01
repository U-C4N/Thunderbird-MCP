/* Native draft opening. Never substitute EditAsNew, and never adopt a user's window. */
function tbxDraftEditorSupported() {
  // Mozilla bug 1925747: the OpenPGP startup listener could miss the editor
  // entirely. Certifying a partially initialized composer as ready is unsafe.
  const version = Services.appinfo.version;
  const major = Number.parseInt(version, 10);
  if (major === 128) {
    return Services.vc.compare(version, "128.4.3") >= 0;
  }
  return major > 133 || (major === 133 && Services.vc.compare(version, "133.0b3") >= 0);
}

{
  TBX_MODULE_NAMES.push("drafts");
  const owned = new Map();
  const opening = new Set();
  const pending = new Set();
  let stopped = false;
  const editorRestriction = "Saved draft editing and sending require the native composer startup fix " +
    "in Thunderbird 128.4.3 ESR or 133.0b3 and newer. Read this draft here or open it in Thunderbird.";
  TBX_SHUTDOWN.push(() => {
    stopped = true;
    for (const reject of pending) {
      reject(error("DRAFT_INTERRUPTED", "The add-on stopped while opening the draft."));
    }
    for (const entry of owned.values()) {
      entry.release(false);
    }
    owned.clear();
  });
  // Native editor APIs emit trusted beforeinput events too; blocking them silently
  // prevents programmatic body changes. Block physical user input instead.
  const INPUT_EVENTS = ["keydown", "mousedown", "paste", "drop"];

  function error(code, message) {
    return Object.assign(H.blocked(message), { code });
  }

  function headerFor(params, extension) {
    if (!extension?.messageManager || !extension?.tabManager) {
      throw Object.assign(H.unsupported("Native draft editing is unavailable in this build."), {
        code: "UNSUPPORTED_CAPABILITY",
      });
    }
    const id = H.need(params, "messageId");
    if (!Number.isInteger(id)) {
      throw H.usage("messageId must be an integer");
    }
    const header = extension.messageManager.get(id);
    const folder = header?.folder;
    if (!folder || !(folder.flags & Ci.nsMsgFolderFlags.Drafts) ||
        (header.flags & (Ci.nsMsgMessageFlags.Queued | Ci.nsMsgMessageFlags.Template))) {
      throw error("NOT_A_DRAFT", "Only a saved message in a Drafts folder can be edited.");
    }
    return header;
  }

  function baseUri(uri) {
    const value = String(uri || "").split("?")[0];
    // TB128's saved draftId normalises the authority while the source folder URI
    // can retain mixed case. Gecko canonicalisation keeps message/folder path
    // case intact and avoids mistaking the same draft for a different editor.
    return value ? Services.io.newURI(value).spec : "";
  }

  function windowsFor(uri) {
    return [...Services.wm.getEnumerator("")].filter((win) => {
      if (win.closed || !win.gMsgCompose) {
        return false;
      }
      const current = win.gMsgCompose.compFields?.draftId;
      return current && baseUri(current) === baseUri(uri);
    });
  }

  function guardInput(win) {
    const prevent = (event) => {
      if (event.isTrusted) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    };
    for (const name of INPUT_EVENTS) {
      win.addEventListener(name, prevent, true);
    }
    const autoSaveInterval = win.gAutoSaveInterval || 0;
    if (win.gAutoSaveTimeout) {
      win.clearTimeout(win.gAutoSaveTimeout);
      win.gAutoSaveTimeout = null;
    }
    win.gAutoSaveInterval = 0;
    return (restoreAutoSave = true) => {
      if (win.closed) {
        return;
      }
      for (const name of INPUT_EVENTS) {
        win.removeEventListener(name, prevent, true);
      }
      win.gAutoSaveInterval = restoreAutoSave ? autoSaveInterval : 0;
      if (restoreAutoSave && autoSaveInterval && typeof win.AutoSave === "function") {
        win.gAutoSaveTimeout = win.setTimeout(() => win.AutoSave(), autoSaveInterval);
      }
    };
  }

  TBX_MODULES["drafts.inspect"] = async (params, extension) => {
    const header = headerFor(params, extension);
    const uri = header.folder.getUriForMsg(header);
    const editorSupported = tbxDraftEditorSupported();
    return { isDraft: true, openInComposer: windowsFor(uri).length > 0,
      editorSupported, editorRestriction: editorSupported ? null : editorRestriction };
  };

  TBX_MODULES["drafts.open"] = async (params, extension) => {
    if (!tbxDraftEditorSupported()) {
      throw Object.assign(H.unsupported(editorRestriction), { code: "UNSUPPORTED_CAPABILITY" });
    }
    if (stopped) {
      throw error("DRAFT_INTERRUPTED", "The add-on is shutting down.");
    }
    const header = headerFor(params, extension);
    const uri = header.folder.getUriForMsg(header);
    const key = baseUri(uri);
    if (opening.has(key) || windowsFor(uri).length) {
      throw error("DRAFT_BUSY", "This draft is open in Thunderbird. Save and close it, then read it again.");
    }
    const identity = H.identity(H.need(params, "identityId"));
    opening.add(key);
    let timer;
    let observer;
    let rejectPending;
    const listened = new Map();
    const before = new Set(Services.wm.getEnumerator(""));
    try {
      const win = await new Promise((resolve, reject) => {
        rejectPending = reject;
        pending.add(reject);
        observer = {
          observe(subject, topic) {
            if (topic !== "domwindowopened" || before.has(subject)) {
              return;
            }
            const candidate = subject;
            const ready = () => {
              const draft = candidate.gMsgCompose?.compFields?.draftId;
              if (candidate.gComposeType !== Ci.nsIMsgCompType.Draft ||
                  baseUri(draft || candidate.gMsgCompose?.originalMsgURI) !== key) {
                return;
              }
              resolve(candidate);
            };
            listened.set(candidate, ready);
            candidate.addEventListener("compose-editor-ready", ready);
          },
        };
        Services.ww.registerNotification(observer);
        timer = setTimeout(() => reject(error(
          "DRAFT_OPEN_TIMEOUT", "The draft editor did not become ready. Check Thunderbird before retrying."
        )), 15000);
        try {
          needMod("MailServices").compose.OpenComposeWindow(
            null, header, uri, Ci.nsIMsgCompType.Draft,
            Ci.nsIMsgCompFormat.Default, identity, null, null
          );
        } catch (ex) {
          reject(ex);
        }
      });
      if (stopped) {
        throw error("DRAFT_INTERRUPTED", "The add-on stopped while opening the draft.");
      }
      const tab = extension.tabManager.convert(win);
      if (!Number.isInteger(tab?.id)) {
        throw H.unsupported("Thunderbird could not identify the native draft tab.");
      }
      const release = guardInput(win);
      owned.set(tab.id, { win, release, operationId: params.operationId });
      return { tabId: tab.id, windowId: tab.windowId };
    } finally {
      opening.delete(key);
      pending.delete(rejectPending);
      if (timer) {
        clearTimeout(timer);
      }
      if (observer) {
        Services.ww.unregisterNotification(observer);
      }
      for (const [win, listener] of listened) {
        win.removeEventListener("compose-editor-ready", listener);
      }
    }
  };

  TBX_MODULES["drafts.state"] = async (params) => {
    const entry = owned.get(params.tabId);
    if (!entry || entry.operationId !== params.operationId || entry.win.closed) {
      throw error("DRAFT_NOT_OWNED", "The operation does not own this compose window.");
    }
    const controller = entry.win.defaultController;
    return {
      canSendNow: Boolean(controller.isCommandEnabled("cmd_sendNow")),
      canSendLater: Boolean(controller.isCommandEnabled("cmd_sendLater")),
    };
  };

  TBX_MODULES["drafts.finish"] = async (params) => {
    const entry = owned.get(params.tabId);
    if (!entry || entry.operationId !== params.operationId) {
      throw error("DRAFT_NOT_OWNED", "The operation does not own this compose window.");
    }
    owned.delete(params.tabId);
    if (entry.win.closed) {
      return { closed: true };
    }
    // Failed/uncertain operations leave an accessible recovery window. Resuming
    // auto-save could persist partially applied changes or recreate a sent draft.
    entry.release(params.close === true);
    if (params.close === true) {
      // Only callers that know save/send completed request close=true.
      entry.win.SetContentAndBodyAsUnmodified();
      entry.win.close();
    }
    return { closed: Boolean(entry.win.closed) };
  };
}

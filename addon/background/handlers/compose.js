/* Compose handlers — the only code in this add-on that puts mail on the wire.
 *
 * There are two delivery paths and the difference is not cosmetic:
 *
 *   headless — `messages.sendMessage` / `messages.saveMessage` build and deliver a
 *     message with no compose window at all. Cheapest and least disruptive, but the
 *     send half needs the `messages.send` permission, which Thunderbird 153 declares
 *     as *optional-only* (it cannot be granted from `permissions` in the manifest).
 *     So treat it as a capability discovered at runtime, never as a version check.
 *
 *   compose window — `begin{New,Reply,Forward}` then `compose.sendMessage(tabId)`.
 *     This is the only path that can quote an original or emit In-Reply-To and
 *     References: `NewMessageDetails` has no `relatedMessageId`, and `customHeaders`
 *     rejects everything outside `X-*`. Every reply and forward therefore goes
 *     through a window, briefly, even when the headless API is available.
 *
 * When a send fails we save the composition into Drafts before closing the window.
 * A stray draft is a far smaller problem than discarding a body the caller has
 * already paid for, and the user can finish it by hand.
 */

{
  const DEFAULT_LIMIT = 25;
  const SEND_MODE = { send: "sendNow", later: "sendLater" };
  const SAVE_MODE = { draft: "draft", template: "template" };
  const REPLY_TYPES = ["replyToSender", "replyToAll", "replyToList"];
  const FORWARD_TYPES = ["forwardInline", "forwardAsAttachment"];

  /** Thunderbird's CustomHeader type only accepts `X-*` (but not `X-Mozilla-*`) and
   *  the one explicitly allowed name. Checking here gives the model a fixable error
   *  instead of an opaque ExtensionError. */
  const HEADER_NAME = /^(?:X-(?!Mozilla-)[A-Za-z0-9-]+|MSIP_Labels)$/i;

  /** `beginReply` resolves once the window exists, which is not always once the
   *  quote has been inserted into the editor. Poll briefly rather than race it. */
  const QUOTE_WAIT_MS = 2500;
  const QUOTE_POLL_MS = 100;

  function pick(value, allowed, fallback, field) {
    if (value === undefined || value === null || value === "") {
      return fallback;
    }
    if (!allowed.includes(value)) {
      throw tbxError.usage(`${field} must be one of ${allowed.join(", ")}; got ${value}`);
    }
    return value;
  }

  function modeOf(params) {
    const value = params.mode || "draft";
    if (!SEND_MODE[value] && !SAVE_MODE[value]) {
      throw tbxError.usage(`mode must be one of draft, send, later, template; got ${value}`);
    }
    return value;
  }

  function isSave(mode) {
    return Boolean(SAVE_MODE[mode]);
  }

  function canHeadless(mode) {
    const api = browser.messages || {};
    return isSave(mode) ? Boolean(api.saveMessage) : Boolean(api.sendMessage);
  }

  // ------------------------------------------------------------------- details

  /** Recipients may arrive as one string or an array; `undefined` means "leave
   *  whatever Thunderbird already put there", which matters for replies. */
  function recipients(value) {
    if (value === undefined || value === null) {
      return undefined;
    }
    const items = (Array.isArray(value) ? value : [value]).filter(
      (entry) => entry !== null && entry !== undefined && entry !== ""
    );
    return items.length ? items : undefined;
  }

  function customHeaders(value) {
    if (!value) {
      return undefined;
    }
    const entries = Array.isArray(value)
      ? value.map((header) => [header.name, header.value])
      : Object.entries(value);
    if (!entries.length) {
      return undefined;
    }
    return entries.map(([name, headerValue]) => {
      if (!HEADER_NAME.test(String(name))) {
        throw tbxError.usage(
          `custom header ${name} is not allowed — Thunderbird only accepts X-* headers ` +
            "(and not X-Mozilla-*)"
        );
      }
      return {
        name: String(name),
        value: headerValue === undefined || headerValue === null ? "" : String(headerValue),
      };
    });
  }

  function fileFromBase64(name, base64, contentType) {
    const type = contentType || "application/octet-stream";
    const filename = name || "attachment";
    return {
      name: filename,
      file: new File([tbxBase64.toUint8Array(base64)], filename, { type }),
    };
  }

  /** Attachments named by path are read by the privileged half, so a multi-megabyte
   *  file never has to travel through a JSON frame on the bridge. */
  async function fileFromPath(path, name) {
    if (!browser.tbx) {
      throw tbxError.unsupported(
        "attaching a file by path needs the privileged half of the add-on, which did " +
          "not load — reinstall with `tbmcp install-addon`, or pass filename plus base64"
      );
    }
    let read;
    try {
      read = await browser.tbx.invoke("files.read", { path });
    } catch (ex) {
      throw tbxError.usage(`could not read attachment ${path}: ${ex.message || ex}`);
    }
    return fileFromBase64(name || read.name, read.base64, read.contentType);
  }

  async function attachmentsFrom(value) {
    if (!Array.isArray(value) || !value.length) {
      return undefined;
    }
    const out = [];
    for (const entry of value) {
      if (typeof entry === "string") {
        out.push(await fileFromPath(entry, null));
      } else if (entry && entry.base64) {
        out.push(fileFromBase64(entry.filename, entry.base64, entry.contentType));
      } else if (entry && entry.path) {
        out.push(await fileFromPath(entry.path, entry.filename));
      } else {
        throw tbxError.usage(
          "each attachment needs a path, or a filename plus base64 content"
        );
      }
    }
    return out;
  }

  /** Our wire params as a ComposeDetails. Absent keys are left absent on purpose:
   *  `setComposeDetails` treats anything present as a user edit. */
  async function detailsFrom(params) {
    const details = {};
    if (params.identityId) {
      details.identityId = params.identityId;
    }
    for (const field of ["to", "cc", "bcc", "replyTo"]) {
      const value = recipients(params[field]);
      if (value) {
        details[field] = value;
      }
    }
    if (params.subject !== undefined && params.subject !== null) {
      details.subject = String(params.subject);
    }
    if (params.body !== undefined && params.body !== null) {
      // isPlainText decides which of the two body fields Thunderbird reads, so both
      // always travel together.
      if (params.isHtml) {
        details.body = String(params.body);
        details.isPlainText = false;
      } else {
        details.plainTextBody = String(params.body);
        details.isPlainText = true;
      }
    }
    if (params.priority) {
      details.priority = params.priority;
    }
    if (params.returnReceipt !== undefined && params.returnReceipt !== null) {
      details.returnReceipt = Boolean(params.returnReceipt);
    }
    if (params.deliveryFormat) {
      details.deliveryFormat = params.deliveryFormat;
    }
    const headers = customHeaders(params.customHeaders);
    if (headers) {
      details.customHeaders = headers;
    }
    const files = await attachmentsFrom(params.attachments);
    if (files) {
      details.attachments = files;
    }
    return details;
  }

  // ---------------------------------------------------------------- quoted body

  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;" };

  function htmlFromText(text) {
    return `<p>${String(text)
      .replace(/[&<>]/g, (character) => ESCAPES[character])
      .replace(/\r?\n/g, "<br>")}</p>`;
  }

  function textFromHtml(html) {
    return String(html)
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<\/p\s*>/gi, "\n\n")
      .replace(/<[^>]+>/g, "")
      .replace(/&nbsp;/g, " ")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&amp;/g, "&")
      .trim();
  }

  /** Insert a snippet just inside `<body>`. Thunderbird hands back a whole HTML
   *  document, so concatenating in front of it would produce content before <html>
   *  and lose the reply's styling. */
  function intoBody(document_, snippet) {
    const open = /<body[^>]*>/i.exec(document_ || "");
    if (!open) {
      return snippet + (document_ || "");
    }
    const at = open.index + open[0].length;
    return document_.slice(0, at) + snippet + document_.slice(at);
  }

  function hasBody(details) {
    const text = details.isPlainText ? details.plainTextBody : details.body;
    return Boolean(text && textFromHtml(text));
  }

  /** Read the composer once Thunderbird has finished filling it in. */
  async function settled(tabId) {
    const deadline = Date.now() + QUOTE_WAIT_MS;
    let current = await browser.compose.getComposeDetails(tabId);
    while (!hasBody(current) && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, QUOTE_POLL_MS));
      current = await browser.compose.getComposeDetails(tabId);
    }
    return current;
  }

  /** Put the caller's text above whatever Thunderbird quoted — top-posting, which is
   *  what every Windows mail client does. `quoteOriginal: false` drops the quote.
   *  The composer's own format wins: an HTML identity gets HTML either way. */
  function bodyPatch(current, params) {
    if (params.body === undefined || params.body === null) {
      return {};
    }
    const keepQuote = params.quoteOriginal !== false;
    if (current.isPlainText) {
      const quoted = keepQuote ? current.plainTextBody || "" : "";
      const text = params.isHtml ? textFromHtml(params.body) : String(params.body);
      return { isPlainText: true, plainTextBody: quoted ? `${text}\n\n${quoted}` : text };
    }
    const snippet = params.isHtml ? String(params.body) : htmlFromText(params.body);
    return { isPlainText: false, body: keepQuote ? intoBody(current.body, snippet) : snippet };
  }

  // -------------------------------------------------------------------- outcome

  function summary(message) {
    if (!message) {
      return null;
    }
    return {
      id: message.id,
      headerMessageId: message.headerMessageId,
      subject: message.subject,
      author: message.author,
      recipients: message.recipients || [],
      date: message.date ? new Date(message.date).toISOString() : null,
      size: message.size,
      folderId: message.folder ? message.folder.id : undefined,
      folderPath: message.folder ? message.folder.path : undefined,
    };
  }

  /** Normalise what Thunderbird reports about the copies it created. For a save that
   *  is the draft itself; for a send it is the fcc copy in Sent, which is the only
   *  handle anyone gets on a message that has already left. */
  function outcome(result, mode, transport) {
    const copies = (((result || {}).messages) || []).map(summary);
    const first = copies[0] || {};
    return {
      requestedMode: mode,
      mode: (result || {}).mode || null,
      headerMessageId: (result || {}).headerMessageId || null,
      messageId: first.id === undefined ? null : first.id,
      folderId: first.folderId || null,
      folderPath: first.folderPath || null,
      copies,
      transport,
    };
  }

  // -------------------------------------------------------------- window plumbing

  /** Close a compose window without tripping the "save changes?" modal — a modal
   *  would block the bridge behind a dialog nobody is watching for. */
  async function closeTab(tabId) {
    try {
      await browser.compose.setComposeDetails(tabId, { isModified: false });
    } catch (ex) {
      tbxLog.debug("could not clear the modified flag before closing:", ex.message || ex);
    }
    try {
      await browser.tabs.remove(tabId);
    } catch (ex) {
      // Thunderbird closes the window itself after a successful send.
      tbxLog.debug(`compose tab ${tabId} was already gone`);
    }
  }

  async function rescueDraft(tabId) {
    try {
      const saved = await browser.compose.saveMessage(tabId, { mode: "draft" });
      const first = ((saved || {}).messages || [])[0];
      return first ? first.id : null;
    } catch (ex) {
      tbxLog.warn("could not rescue the failed composition into Drafts:", ex.message || ex);
      return null;
    }
  }

  async function deliverTab(tabId, mode) {
    let result;
    try {
      result = isSave(mode)
        ? await browser.compose.saveMessage(tabId, { mode: SAVE_MODE[mode] })
        : await browser.compose.sendMessage(tabId, { mode: SEND_MODE[mode] });
    } catch (ex) {
      const rescued = isSave(mode) ? null : await rescueDraft(tabId);
      await closeTab(tabId);
      throw tbxError.thunderbird(
        `${isSave(mode) ? "saving" : "sending"} failed: ${ex.message || ex}` +
          (rescued
            ? ` — the composed message was kept as draft ${rescued}, so nothing was lost`
            : "")
      );
    }
    await closeTab(tabId);
    return outcome(result, mode, "composeWindow");
  }

  /** Open a composer of the requested kind and merge the caller's body into whatever
   *  Thunderbird generated. `kind` is "new", "reply" or "forward". */
  async function openComposer(kind, relatedId, params) {
    const details = await detailsFrom(params);
    if (kind !== "new") {
      // Thunderbird writes the quoted or forwarded body itself; ours is merged in
      // afterwards, so it must not be part of the opening details or it would win.
      delete details.body;
      delete details.plainTextBody;
      delete details.isPlainText;
    }
    let tab;
    if (kind === "reply") {
      tab = await browser.compose.beginReply(
        relatedId,
        pick(params.replyType, REPLY_TYPES, "replyToSender", "replyType"),
        details
      );
    } else if (kind === "forward") {
      tab = await browser.compose.beginForward(
        relatedId,
        pick(params.forwardType, FORWARD_TYPES, "forwardInline", "forwardType"),
        details
      );
    } else {
      tab = await browser.compose.beginNew(details);
    }
    if (!tab || tab.id === undefined || tab.id === null) {
      throw tbxError.thunderbird("Thunderbird did not return a compose tab");
    }
    if (kind !== "new") {
      const patch = bodyPatch(await settled(tab.id), params);
      if (Object.keys(patch).length) {
        await browser.compose.setComposeDetails(tab.id, patch);
      }
    }
    return tab;
  }

  /** The shared route for a fresh message: headless when this build allows it, a
   *  window otherwise, and always a window when threading is requested. */
  async function dispatch(params, mode) {
    if (params.replyToMessageId) {
      const tab = await openComposer("reply", params.replyToMessageId, params);
      return deliverTab(tab.id, mode);
    }
    if (canHeadless(mode)) {
      const details = await detailsFrom(params);
      const result = isSave(mode)
        ? await browser.messages.saveMessage(details, { mode: SAVE_MODE[mode] })
        : await browser.messages.sendMessage(details, { mode: SEND_MODE[mode] });
      return outcome(result, mode, "headless");
    }
    const tab = await openComposer("new", null, params);
    return deliverTab(tab.id, mode);
  }

  // ------------------------------------------------------------------- handlers

  tbxRegistry.define("compose.send", async (params) => dispatch(params, modeOf(params)));

  tbxRegistry.define("compose.save", async (params) =>
    dispatch(params, params.mode === "template" ? "template" : "draft")
  );

  tbxRegistry.define("compose.reply", async (params) => {
    const messageId = tbxUtil.need(params, "messageId", "int");
    const tab = await openComposer("reply", messageId, params);
    return deliverTab(tab.id, modeOf(params));
  });

  tbxRegistry.define("compose.forward", async (params) => {
    const messageId = tbxUtil.need(params, "messageId", "int");
    const tab = await openComposer("forward", messageId, params);
    return deliverTab(tab.id, modeOf(params));
  });

  tbxRegistry.define("compose.open", async (params) => {
    let tab;
    if (params.forwardMessageId) {
      tab = await openComposer("forward", params.forwardMessageId, params);
    } else if (params.replyToMessageId) {
      tab = await openComposer("reply", params.replyToMessageId, params);
    } else {
      tab = await openComposer("new", null, params);
    }
    const state = await browser.compose.getComposeState(tab.id).catch(() => ({}));
    return {
      tabId: tab.id,
      windowId: tab.windowId === undefined ? null : tab.windowId,
      canSendNow: state.canSendNow === undefined ? null : state.canSendNow,
      canSendLater: state.canSendLater === undefined ? null : state.canSendLater,
    };
  });

  // --------------------------------------------------------------------- status

  /** Every Outbox this profile can see. `folders.getUnifiedFolder` has no "outbox"
   *  type on 153, so the unified view — where it exists — only turns up in a query,
   *  and unified folders are skipped unless asked for explicitly. */
  async function outboxes() {
    if (!browser.folders || !browser.folders.query) {
      return { unified: [], real: [] };
    }
    const unified = await browser.folders
      .query({ specialUse: ["outbox"], isUnified: true })
      .catch(() => []);
    const real = await browser.folders.query({ specialUse: ["outbox"] }).catch(() => []);
    return { unified: unified || [], real: real || [] };
  }

  async function folderRef(folder) {
    const info = await browser.folders.getFolderInfo(folder.id).catch(() => ({}));
    return {
      id: folder.id,
      name: folder.name,
      path: folder.path,
      accountId: folder.accountId,
      isUnified: Boolean(folder.isUnified),
      queued: Number.isInteger(info.totalMessageCount) ? info.totalMessageCount : null,
    };
  }

  tbxRegistry.define("compose.status", async (params) => {
    const limit = tbxUtil.limit(params.limit, DEFAULT_LIMIT, 200);
    const found = await outboxes();
    const real = [];
    const unified = [];
    for (const folder of found.real) {
      real.push(await folderRef(folder));
    }
    for (const folder of found.unified) {
      unified.push(await folderRef(folder));
    }

    // Count the per-account folders where there are any: the unified view lists the
    // same messages again, and a doubled queue length reads as a bug.
    const counted = real.length ? real : unified;
    const queued = counted.reduce((total, folder) => total + (folder.queued || 0), 0);

    // Read the queue itself through the unified Outbox when the profile has one — one
    // list instead of one per account, and it is what the user sees in the folder pane.
    const messages = [];
    for (const folder of unified.length ? unified : real) {
      if (messages.length >= limit) {
        break;
      }
      const list = await browser.messages.list(folder.id).catch(() => null);
      for (const message of (list && list.messages) || []) {
        if (messages.length >= limit) {
          break;
        }
        messages.push(summary(message));
      }
    }

    return {
      folders: real.concat(unified),
      messages,
      queued,
      note: real.length || unified.length
        ? null
        : "This profile has no Outbox, so nothing is queued — Thunderbird creates one " +
          "the first time a message is held back for later sending.",
    };
  });
}

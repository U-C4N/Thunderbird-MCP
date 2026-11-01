/* Saved-draft snapshots and guarded native edits. Content never enters the operation journal. */
{
  const MAX_BODY = 200000;
  const MAX_RAW = 64 * 1024 * 1024;
  const active = new Set();
  const PROTECTED_TYPES = new Set([
    "multipart/encrypted", "multipart/signed", "application/pkcs7-mime",
    "application/x-pkcs7-mime", "application/pgp-encrypted",
  ]);

  function blocked(code, message) {
    return tbxError.blocked(message, null, { code });
  }

  async function native(method, params) {
    if (!browser.tbx) {
      throw tbxError.unsupported("Native draft editing needs the privileged add-on capability.", {
        code: "UNSUPPORTED_CAPABILITY",
      });
    }
    try {
      return await browser.tbx.invoke(method, params);
    } catch (ex) {
      throw tbxError.fromWire(ex) || ex;
    }
  }

  function headersOf(part) {
    return Object.fromEntries(Object.entries(part?.headers || {}).map(([key, value]) => [
      key.toLowerCase(), Array.isArray(value) ? value : [value],
    ]));
  }

  function collect(part, found, nested = false, root = true) {
    if (!part) {
      return;
    }
    const type = String(part.contentType || "").split(";")[0].toLowerCase();
    const headers = headersOf(part);
    if (PROTECTED_TYPES.has(type)) {
      found.protected = true;
    }
    const cid = (headers["content-id"] || [])[0];
    if (cid) {
      found.inlineParts.add(part.partName);
    }
    // Do not mistake a text attachment or an attached message's body for the draft.
    const disposition = String((headers["content-disposition"] || [])[0] || "");
    const attached = nested || (!root && type === "message/rfc822") || /^attachment/i.test(disposition);
    if (!attached && !part.name && (type === "text/html" || type === "text/plain") &&
        typeof part.body === "string") {
      found.bodies.push({ body: part.body, isHtml: type === "text/html" });
    }
    for (const child of part.parts || []) {
      collect(child, found, attached, false);
    }
  }

  async function digest(bytes) {
    const hash = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(hash), (value) => value.toString(16).padStart(2, "0")).join("");
  }

  async function revisionFor(messageId) {
    const raw = await browser.messages.getRaw(messageId, { decrypt: false, data_format: "BinaryString" });
    if (typeof raw !== "string" || raw.length > MAX_RAW) {
      throw blocked("DRAFT_TOO_LARGE", "This draft is too large to review safely; use Thunderbird.");
    }
    const split = /\r?\n\r?\n/.exec(raw);
    if (!split) {
      throw blocked("DRAFT_UNSUPPORTED_MIME", "The saved draft has no complete MIME headers.");
    }
    // Message flags may change between reads without changing any reviewed content.
    const header = raw.slice(0, split.index).replace(/\r\n/g, "\n").split(/\n(?![ \t])/)
      .filter((line) => !/^x-mozilla-(?:status2?|keys):/i.test(line)).join("\n");
    const stable = header + "\n\n" + raw.slice(split.index + split[0].length);
    const bytes = Uint8Array.from(stable, (character) => character.charCodeAt(0) & 255);
    return "sha256:" + await digest(bytes);
  }

  async function snapshot(params) {
    let message = params.reference
      ? await tbxReferences.resolve(params.reference)
      : await browser.messages.get(tbxUtil.need(params, "messageId", "int"));
    const info = await native("drafts.inspect", { messageId: message.id });
    const result = {
      messageId: message.id, folderId: message.folder?.id, subject: message.subject || "",
      author: message.author || "", to: message.recipients || [], cc: message.ccList || [],
      bcc: message.bccList || [], openInComposer: Boolean(info.openInComposer),
      editable: false, sendable: false, revision: null,
    };
    if (info.editorSupported === false) {
      result.editorRestriction = info.editorRestriction;
    }
    if (message.size > MAX_RAW) {
      return { ...result, reason: "message_too_large", note: "Edit this draft in Thunderbird." };
    }
    // The displayed MIME and its revision must describe the same saved bytes.
    // Refresh metadata inside this bracket as the first lookup may have raced a
    // save. Never pair an older body or recipient list with a newer revision.
    let revisionBefore;
    try {
      revisionBefore = await revisionFor(message.id);
    } catch (ex) {
      if (ex.code === "DRAFT_TOO_LARGE" || ex.code === "DRAFT_UNSUPPORTED_MIME") {
        return { ...result, reason: "unsupported_body", note: ex.message };
      }
      throw ex;
    }
    message = await browser.messages.get(message.id);
    Object.assign(result, {
      messageId: message.id, folderId: message.folder?.id, subject: message.subject || "",
      author: message.author || "", to: message.recipients || [], cc: message.ccList || [],
      bcc: message.bccList || [],
    });
    // Decoding is the default on every supported release. Explicit decoding
    // switches were introduced in TB133 and reject the entire read on TB128.
    const full = await browser.messages.getFull(message.id, { decrypt: false });
    const headers = headersOf(full);
    const found = { bodies: [], inlineParts: new Set(), protected: false };
    collect(full, found);
    result.identityId = (headers["x-identity-key"] || [])[0] || null;
    if (found.protected || (full.decryptionStatus && full.decryptionStatus !== "none")) {
      return { ...result, reason: "protected_draft", note: "Open protected drafts in Thunderbird." };
    }
    const body = found.bodies.find((item) => item.isHtml) || found.bodies[0];
    if (!body) {
      return { ...result, reason: "unsupported_body", note: "The MIME body cannot be edited safely." };
    }
    result.bodyIsHtml = body.isHtml;
    result.hasInlineContent = found.inlineParts.size > 0 || /\bcid:/i.test(body.body);
    if ([...body.body].length > MAX_BODY) {
      return {
        ...result, reason: "body_too_large", bodyTruncated: true,
        bodyPreview: [...body.body].slice(0, 4000).join(""),
        note: "The preview is incomplete. Edit and send this draft in Thunderbird.",
      };
    }
    result.body = body.body;
    const attachments = await browser.messages.listAttachments(message.id);
    result.attachments = (attachments || []).map((attachment) => ({
      attachmentRef: "part:" + attachment.partName, partName: attachment.partName,
      name: attachment.name, size: attachment.size, contentType: attachment.contentType,
      inline: found.inlineParts.has(attachment.partName),
    }));
    try {
      result.reference = await tbxReferences.capture(message.id);
    } catch (ex) {
      return { ...result, reason: "unresolvable_reference", note: tbxError.readable(ex) };
    }
    try {
      result.revision = await revisionFor(message.id);
      if (result.revision !== revisionBefore) {
        throw blocked("DRAFT_CONFLICT", "The draft changed while it was being read. Read it again before reviewing or editing it.");
      }
    } catch (ex) {
      if (ex.code === "DRAFT_TOO_LARGE" || ex.code === "DRAFT_UNSUPPORTED_MIME") {
        delete result.body;
        return { ...result, reason: "unsupported_body", note: ex.message };
      }
      throw ex;
    }
    if (info.editorSupported === false) {
      return { ...result, reason: "unsupported_draft_editor", note: result.editorRestriction };
    }
    result.editable = !result.openInComposer && Boolean(result.identityId);
    result.sendable = result.editable;
    if (result.openInComposer) {
      result.reason = "draft_busy";
    } else if (!result.identityId) {
      result.reason = "identity_unresolved";
    }
    return result;
  }

  function ensureEditable(before, expected) {
    if (before.editorRestriction) {
      throw tbxError.unsupported(before.editorRestriction, { code: "UNSUPPORTED_CAPABILITY" });
    }
    if (before.openInComposer) {
      throw blocked("DRAFT_BUSY", "Save and close the draft in Thunderbird, then read it again.");
    }
    if (!before.editable || !before.revision) {
      throw blocked("DRAFT_NOT_EDITABLE", "This draft cannot be edited automatically: " + before.reason);
    }
    if (before.revision !== expected) {
      throw blocked("DRAFT_CONFLICT", "The saved draft changed. Read it again before applying changes.");
    }
  }

  function checkProtection(details) {
    const protection = details.selectedEncryptionTechnology;
    if (protection && (protection.encryptBody || protection.encryptSubject || protection.signMessage)) {
      throw blocked("PROTECTED_DRAFT_UNSUPPORTED", "Send or edit this protected draft in Thunderbird.");
    }
  }

  async function mailboxKeys(values) {
    const strings = (Array.isArray(values) ? values : [values]).filter(Boolean);
    if (strings.some((value) => typeof value !== "string")) {
      throw blocked("DRAFT_CONFLICT", "The editor has unresolved recipient entries.");
    }
    if (!browser.messengerUtilities?.parseMailboxString) {
      return strings.map((value) => value.trim()).sort();
    }
    const parsed = await browser.messengerUtilities.parseMailboxString(strings.join(", "));
    return parsed.map((value) => value.email || value.name || "").sort();
  }

  function plainBody(value) {
    // Thunderbird's plain-text serializer appends a final line ending.
    return String(value || "").replace(/\r\n/g, "\n").replace(/\n$/, "");
  }

  async function verifyDetails(details, expected, checkBody = true) {
    if (details.subject !== expected.subject) {
      throw blocked("DRAFT_CONFLICT", "The native editor did not preserve the reviewed subject.");
    }
    for (const field of ["to", "cc", "bcc"]) {
      if (JSON.stringify(await mailboxKeys(details[field])) !==
          JSON.stringify(await mailboxKeys(expected[field]))) {
        throw blocked("DRAFT_CONFLICT", "The native editor changed the reviewed recipients.");
      }
    }
    if (checkBody && !expected.bodyIsHtml && plainBody(details.plainTextBody) !== plainBody(expected.body)) {
      throw blocked("DRAFT_CONFLICT", "The native editor did not preserve the reviewed body.");
    }
  }

  function changesFor(params, before) {
    const changes = params.changes;
    const allowed = new Set(["subject", "body", "to", "cc", "bcc", "addAttachments", "removeAttachmentRefs"]);
    if (!changes || typeof changes !== "object" || Array.isArray(changes) || !Object.keys(changes).length) {
      throw tbxError.usage("changes must name at least one draft field");
    }
    if (Object.keys(changes).some((name) => !allowed.has(name))) {
      throw tbxError.usage("Unsupported draft field; sender identity and body format cannot change.");
    }
    for (const field of ["subject", "body"]) {
      if (field in changes && typeof changes[field] !== "string") {
        throw tbxError.usage(`${field} must be a string`);
      }
    }
    if ("body" in changes) {
      if (before.hasInlineContent) {
        throw blocked("DRAFT_UNSUPPORTED_MIME", "Edit the body of drafts with inline images in Thunderbird.");
      }
      if ([...changes.body].length > MAX_BODY) {
        throw blocked("DRAFT_TOO_LARGE", "The replacement body exceeds 200000 characters.");
      }
      if (before.bodyIsHtml && /\bcid:/i.test(changes.body)) {
        throw blocked("DRAFT_UNSUPPORTED_MIME", "Adding inline CID images is not supported.");
      }
    }
    for (const field of ["to", "cc", "bcc", "addAttachments", "removeAttachmentRefs"]) {
      if (field in changes && !Array.isArray(changes[field])) {
        throw tbxError.usage(`${field} must be an array`);
      }
    }
    return changes;
  }

  async function removalsFor(tabId, messageId, references, before) {
    const requested = [...new Set(references || [])];
    const originals = requested.map((ref) => {
      const attachment = before.attachments.find((item) => item.attachmentRef === ref);
      if (!attachment || attachment.inline) {
        throw blocked("DRAFT_ATTACHMENT_CONFLICT", "Unknown or inline attachment reference: " + ref);
      }
      return attachment;
    });
    if (!originals.length) {
      return [];
    }
    const live = await browser.compose.listAttachments(tabId);
    const removed = new Set();
    const matches = [];
    for (const original of originals) {
      const file = await browser.messages.getAttachmentFile(messageId, original.partName);
      const expected = await digest(await file.arrayBuffer());
      const candidates = [];
      for (const candidate of live) {
        if (removed.has(candidate.id) || candidate.name !== original.name || candidate.size !== original.size) {
          continue;
        }
        const candidateFile = await browser.compose.getAttachmentFile(candidate.id);
        if (await digest(await candidateFile.arrayBuffer()) === expected) {
          candidates.push(candidate.id);
        }
      }
      if (candidates.length !== 1) {
        throw blocked("DRAFT_ATTACHMENT_CONFLICT", "The attachment cannot be uniquely matched in the editor.");
      }
      removed.add(candidates[0]);
      matches.push(candidates[0]);
    }
    return matches;
  }

  async function mutate(params, send) {
    const expected = tbxUtil.need(params, "expectedRevision", "string");
    const operationId = tbxUtil.need(params, "operationId", "string");
    const before = await snapshot(params);
    ensureEditable(before, expected);
    if (send && (!before.sendable || !["send", "later"].includes(params.mode))) {
      throw blocked("DRAFT_NOT_SENDABLE", "A saved sender identity and send/later mode are required.");
    }
    const key = before.reference.fingerprint + ":" + before.reference.folderId;
    if (active.has(key)) {
      throw blocked("DRAFT_BUSY", "Another operation is editing this draft.");
    }
    active.add(key);
    let tabId;
    let success = false;
    let dispatching = false;
    try {
      const changes = send ? {} : changesFor(params, before);
      // Read all new files before touching the saved draft or opening a composer.
      const additions = [];
      for (const entry of changes.addAttachments || []) {
        if (!entry || typeof entry.path !== "string") {
          throw tbxError.usage("addAttachments entries require a path");
        }
        additions.push(await tbxCompose.fileFromPath(entry.path, entry.filename));
      }
      const opened = await native("drafts.open", { messageId: before.messageId, identityId: before.identityId, operationId });
      tabId = opened.tabId;
      const details = await browser.compose.getComposeDetails(tabId);
      checkProtection(details);
      if (details.type !== "draft" || (before.identityId && details.identityId !== before.identityId)) {
        throw blocked("DRAFT_CONFLICT", "Thunderbird opened a different draft identity or compose type.");
      }
      if (details.isPlainText === before.bodyIsHtml) {
        throw blocked("DRAFT_CONFLICT", "Thunderbird changed the draft's body format.");
      }
      await verifyDetails(details, before);
      const removeIds = await removalsFor(
        tabId, before.messageId, changes.removeAttachmentRefs, before
      );
      // Opening and reading attachments took time: detect an external save before writing.
      if (await revisionFor(before.messageId) !== expected) {
        throw blocked("DRAFT_CONFLICT", "The saved draft changed while its editor was opening.");
      }
      if (send) {
        let state = await browser.compose.getComposeState(tabId);
        // Some Thunderbird builds return an empty state object. Check the same
        // native command controller, never infer permission to send from absence.
        if (typeof state?.canSendNow !== "boolean" || typeof state?.canSendLater !== "boolean") {
          state = await native("drafts.state", { tabId, operationId });
        }
        if (!(params.mode === "send" ? state.canSendNow : state.canSendLater)) {
          throw blocked("DRAFT_NOT_SENDABLE", "Thunderbird reports this draft cannot be sent yet.");
        }
      } else {
        const patch = {};
        for (const field of ["subject", "to", "cc", "bcc"]) {
          if (field in changes) {
            patch[field] = changes[field];
          }
        }
        if ("body" in changes) {
          patch[before.bodyIsHtml ? "body" : "plainTextBody"] = changes.body;
          patch.isPlainText = !before.bodyIsHtml;
        }
        if (Object.keys(patch).length) {
          await browser.compose.setComposeDetails(tabId, patch);
        }
        for (const attachmentId of removeIds) {
          await browser.compose.removeAttachment(tabId, attachmentId);
        }
        for (const attachment of additions) {
          await browser.compose.addAttachment(tabId, attachment);
        }
        // A successful API call can still leave an editor field unchanged (for
        // example an input listener prevented insertion). Never save that as success.
        await verifyDetails(await browser.compose.getComposeDetails(tabId), { ...before, ...changes });
      }
      dispatching = true;
      let delivered;
      try {
        delivered = send
          ? await browser.compose.sendMessage(tabId, { mode: params.mode === "send" ? "sendNow" : "sendLater" })
          : await browser.compose.saveMessage(tabId, { mode: "draft" });
      } catch (ex) {
        throw tbxError.thunderbird(
          "The operation outcome is unknown. Inspect its status before doing anything else. " +
            tbxError.readable(ex), { code: "OUTCOME_UNKNOWN" }
        );
      }
      success = true;
      const mode = send ? params.mode : "draft";
      const receipt = {
        ...tbxCompose.outcome(delivered, mode, "nativeDraft"),
        operationId, status: "succeeded", sent: send && mode === "send",
        queued: send && mode === "later", saved: !send,
      };
      if (!send) {
        const messageId = receipt.messageId;
        if (messageId === null || messageId === undefined) {
          throw tbxError.thunderbird("The draft was saved but its new reference is unavailable.", {
            code: "OUTCOME_UNKNOWN",
          });
        }
        receipt.reference = await tbxReferences.capture(messageId);
        receipt.revision = await revisionFor(messageId);
      }
      return receipt;
    } catch (ex) {
      let failure = ex;
      if (dispatching && ex.code !== "OUTCOME_UNKNOWN") {
        failure = tbxError.thunderbird("The operation may have completed; check its status. " +
          tbxError.readable(ex), { code: "OUTCOME_UNKNOWN" });
      }
      if (tabId !== undefined && !success) {
        Object.assign(failure, { recoveryWindowOpen: true, tabId });
        failure.message += " A recovery editor remains open with automatic saving disabled; " +
          "review it in Thunderbird before explicitly saving or closing it.";
      }
      throw failure;
    } finally {
      if (tabId !== undefined) {
        try {
          await native("drafts.finish", { tabId, operationId, close: success });
        } catch (ex) {
          tbxLog.warn("Could not release the owned draft editor:", tbxError.readable(ex));
        }
      }
      active.delete(key);
    }
  }

  tbxRegistry.define("compose.draftGet", snapshot);
  tbxRegistry.define("compose.draftUpdate", (params) => mutate(params, false));
  tbxRegistry.define("compose.draftSend", (params) => mutate(params, true));
}

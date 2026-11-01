/* Attachment plans contain metadata only. A read-only prepare step computes the
 * hash the daemon persists before the single-file write is dispatched. */
{
  const FILE_LIMIT = 25 * 1024 * 1024, TOTAL_LIMIT = 250 * 1024 * 1024;
  const problem = (status, code, reason) => ({ status, code, reason });
  const mime = (value) => String(value || "application/octet-stream").split(";")[0].trim().toLowerCase();
  async function invoke(method, params) {
    if (!browser.tbx) throw tbxError.unsupported("Saving attachments requires the local-file module.");
    try { return await browser.tbx.invoke(method, params); }
    catch (ex) { throw tbxError.fromWire(ex) || ex; }
  }
  function metadata(attachment) {
    return { partName: attachment.partName, originalName: attachment.name || "attachment",
      contentType: mime(attachment.contentType), expectedSize: attachment.size };
  }
  async function source(item) {
    if (!item || !item.reference || !item.partName || !item.directory || !item.filename || !item.path) {
      throw tbxError.usage("The frozen attachment task is invalid.");
    }
    const message = await tbxReferences.resolve(item.reference);
    const attachments = await browser.messages.listAttachments(message.id);
    const found = (attachments || []).find((value) => value.partName === item.partName);
    if (!found || (found.name || "attachment") !== item.originalName || mime(found.contentType) !== item.contentType || found.size !== item.expectedSize) {
      throw tbxError.blocked("The attachment changed since the preview.", [], { code: "ATTACHMENT_CHANGED" });
    }
    const file = await browser.messages.getAttachmentFile(message.id, item.partName);
    if (!file || !Number.isFinite(file.size) || file.size > FILE_LIMIT || file.size !== item.expectedSize) {
      throw tbxError.blocked("The attachment size changed or exceeds 25 MiB.", [], { code: "ATTACHMENT_SIZE" });
    }
    const bytes = await file.arrayBuffer();
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
    return { bytes, sha256, size: file.size };
  }
  tbxRegistry.define("messages.attachmentsPlan", async (params) => {
    const ids = [...new Set(tbxUtil.need(params, "messageIds", "array"))];
    if (!ids.length || ids.length > 100 || ids.some((id) => !Number.isInteger(id) || id < 0)) throw tbxError.usage("Select 1-100 message ids.");
    const directory = tbxUtil.need(params, "directory", "string");
    const contains = String(params.filenameContains || "").normalize("NFC").toLowerCase();
    if (params.contentTypes && !Array.isArray(params.contentTypes)) throw tbxError.usage("contentTypes must be an array.");
    const types = params.contentTypes ? new Set(params.contentTypes.map(mime)) : null;
    if (types && (!types.size || [...types].some((value) => !/^[a-z0-9!#$&^_.+-]+\/[a-z0-9!#$&^_.+-]+$/i.test(value)))) throw tbxError.usage("contentTypes must contain exact MIME types.");
    const selected = [], invalid = [], preview = [];
    let totalBytes = 0;
    // Validate the directory even when none of the messages has a matching file.
    await invoke("files.prepareNames", { directory, names: [] });
    for (const messageId of ids) {
      try {
        const message = await browser.messages.get(messageId);
        const reference = await tbxReferences.captureHeader(message);
        for (const attachment of await browser.messages.listAttachments(messageId)) {
          const info = metadata(attachment);
          if (contains && !info.originalName.normalize("NFC").toLowerCase().includes(contains)) continue;
          if (types && !types.has(info.contentType)) continue;
          if (!Number.isInteger(info.expectedSize) || info.expectedSize < 0 || info.expectedSize > FILE_LIMIT) {
            invalid.push({ messageId, partName: info.partName, code: "ATTACHMENT_SIZE", reason: "Size unavailable or over 25 MiB." });
            continue;
          }
          totalBytes += info.expectedSize;
          if (selected.length >= 200 || totalBytes > TOTAL_LIMIT) {
            invalid.push({ messageId, code: "EXPORT_LIMIT", reason: "Selection exceeds 200 attachments or 250 MiB; narrow it." });
            break;
          }
          selected.push({ reference, ...info });
          preview.push({ messageId, subject: message.subject, originalName: info.originalName, partName: info.partName, bytes: info.expectedSize, contentType: info.contentType });
        }
      } catch (ex) { invalid.push({ messageId, code: ex.code || "SOURCE_UNAVAILABLE", reason: "Message or attachments are unavailable or ambiguous." }); }
      if (totalBytes > TOTAL_LIMIT || invalid.some((entry) => entry.code === "EXPORT_LIMIT")) break;
    }
    const allocation = await invoke("files.prepareNames", { directory, names: selected.map((item) => item.originalName) });
    const items = selected.map((item, index) => ({ ...item, directory: allocation.directory,
      filename: allocation.files[index].name, path: allocation.files[index].path }));
    for (let i = 0; i < preview.length; i++) Object.assign(preview[i], { name: items[i].filename, path: items[i].path });
    return { items, preview, invalid, counts: { messages: ids.length, attachments: items.length, bytes: items.reduce((sum, item) => sum + item.expectedSize, 0), invalid: invalid.length } };
  });
  tbxRegistry.define("messages.attachmentsPrepare", async (params) => {
    const item = tbxUtil.need(params, "item");
    const value = await source(item);
    return { item: { ...item, expectedSha256: value.sha256, expectedBytes: value.size } };
  });
  async function reconcile(item) {
    if (!item || !/^[a-f0-9]{64}$/.test(item.expectedSha256 || "")) return problem("unknown", "HASH_UNAVAILABLE", "No durable pre-write hash is available.");
    const current = await invoke("files.hash", { path: item.path });
    if (!current.exists) return problem("unknown", "FILE_ABSENT", "The destination is absent; no write is replayed automatically.");
    if (current.sha256 !== item.expectedSha256 || current.bytes !== item.expectedBytes) return problem("conflict", "DESTINATION_CHANGED", "The destination differs from the reviewed attachment.");
    return { status: "succeeded", saved: true, reference: item.reference, path: item.path, name: item.filename,
      bytes: current.bytes, sha256: current.sha256, reason: "Existing file matches the durable pre-write hash." };
  }
  tbxRegistry.define("messages.attachmentsReconcile", (params) => reconcile(params.item));
  tbxRegistry.define("messages.attachmentsSave", async (params) => {
    const item = tbxUtil.need(params, "item");
    if (!/^[a-f0-9]{64}$/.test(item.expectedSha256 || "")) throw tbxError.usage("Prepare and persist the attachment hash before saving.");
    const existing = await invoke("files.stat", { path: item.path });
    if (existing.exists) return problem("conflict", "DESTINATION_EXISTS", "The frozen destination now exists; no existing file is reused or overwritten by a new save.");
    let value;
    try { value = await source(item); }
    catch (ex) { return problem("conflict", ex.code || "SOURCE_CHANGED", "The source no longer matches the attachment preview."); }
    if (value.sha256 !== item.expectedSha256 || value.size !== item.expectedBytes) return problem("conflict", "ATTACHMENT_CHANGED", "Attachment bytes changed after preparation.");
    try {
      const written = await invoke("files.write", { directory: item.directory, filename: item.filename,
        base64: tbxBase64.fromArrayBuffer(value.bytes), overwrite: false, hash: true, expectedSha256: item.expectedSha256 });
      return { status: "succeeded", saved: true, reference: item.reference, ...written };
    } catch (ex) {
      if (ex.tbxKind === "blocked") return problem("conflict", ex.code || "DESTINATION_CONFLICT", "The destination changed; no existing file was overwritten.");
      throw ex;
    }
  });
}

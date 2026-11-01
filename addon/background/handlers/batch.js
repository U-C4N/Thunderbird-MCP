/* Frozen previews and guarded, one-message steps. The daemon owns the durable
 * journal and queue; operations.js owns per-runtime request receipts. */
{
  const MAX_MESSAGES = 500;
  const MOVE_EVENT_TIMEOUT_MS = 15000;

  function problem(status, code, message) {
    return { status, code, reason: message };
  }

  function referenceProblem(error) {
    return problem("conflict", error.code || "REFERENCE_UNAVAILABLE",
      "The message or folder changed since the preview; create a new preview.");
  }

  function changesOf(value) {
    const changes = value || {};
    if (!changes || typeof changes !== "object" || Array.isArray(changes)) {
      throw tbxError.usage("changes must be an object.");
    }
    for (const key of Object.keys(changes)) {
      if (!["read", "flagged", "addTags", "removeTags"].includes(key)) {
        throw tbxError.usage(`Unsupported batch change: ${key}`);
      }
    }
    for (const key of ["read", "flagged"]) {
      if (key in changes && typeof changes[key] !== "boolean") {
        throw tbxError.usage(`${key} must be a boolean.`);
      }
    }
    for (const key of ["addTags", "removeTags"]) {
      if (key in changes && (!Array.isArray(changes[key]) ||
          changes[key].some((tag) => typeof tag !== "string" || !tag.trim()))) {
        throw tbxError.usage(`${key} must contain non-empty tag keys.`);
      }
    }
    const add = [...new Set(changes.addTags || [])];
    const remove = [...new Set(changes.removeTags || [])];
    if (add.some((tag) => remove.includes(tag))) {
      throw tbxError.usage("A tag cannot be both added and removed.");
    }
    if (!("read" in changes) && !("flagged" in changes) && !add.length && !remove.length) {
      throw tbxError.usage("Pass at least one read, flagged or tag change.");
    }
    return { ...changes, addTags: add, removeTags: remove };
  }

  async function checkTags(changes) {
    if (!changes.addTags.length && !changes.removeTags.length) {
      return;
    }
    const api = browser.messages;
    const tags = api.tags && typeof api.tags.list === "function"
      ? await api.tags.list()
      : await api.listTags();
    const keys = new Set(tags.map((tag) => tag.key));
    if (changes.addTags.some((tag) => !keys.has(tag))) {
      throw tbxError.usage("A tag to add does not exist; list or create the tag first.");
    }
  }

  function markState(message, template) {
    const state = {};
    for (const key of ["read", "flagged"]) {
      if (key in template) {
        state[key] = Boolean(message[key]);
      }
    }
    if (template.tags) {
      const tags = new Set(message.tags || []);
      state.tags = Object.fromEntries(Object.keys(template.tags).sort().map((tag) => [tag, tags.has(tag)]));
    }
    return state;
  }

  function sameState(left, right) {
    for (const key of ["read", "flagged"]) {
      if (key in right && left[key] !== right[key]) {
        return false;
      }
    }
    return Object.entries(right.tags || {}).every(([tag, present]) => left.tags && left.tags[tag] === present);
  }

  function desiredState(changes) {
    const state = {};
    for (const key of ["read", "flagged"]) {
      if (key in changes) {
        state[key] = changes[key];
      }
    }
    if (changes.addTags.length || changes.removeTags.length) {
      state.tags = Object.fromEntries([
        ...changes.addTags.map((tag) => [tag, true]),
        ...changes.removeTags.map((tag) => [tag, false]),
      ]);
    }
    return state;
  }

  async function concreteFolder(id) {
    const folder = await browser.folders.get(id);
    if (!folder || !folder.accountId || folder.isRoot || folder.isVirtual ||
        folder.isUnified || folder.isTag || typeof folder.path !== "string") {
      throw tbxError.blocked("Batch moves require a concrete mail folder.", [], { code: "FOLDER_UNSUPPORTED" });
    }
    return { id: folder.id, accountId: folder.accountId, path: folder.path };
  }

  async function verifyMove(source, destination) {
    const from = await concreteFolder(source.folderId || source.id);
    const to = await concreteFolder(destination.id);
    if (from.accountId !== source.accountId || from.path !== (source.folderPath ?? source.path) ||
        to.accountId !== destination.accountId || to.path !== destination.path) {
      throw tbxError.blocked("A planned folder changed.", [], { code: "FOLDER_CHANGED" });
    }
    if (from.accountId !== to.accountId) {
      throw tbxError.blocked("Reversible batch moves stay within one account.", [], { code: "CROSS_ACCOUNT_MOVE" });
    }
    if (from.id === to.id) {
      throw tbxError.blocked("The message is already in the destination folder.", [], { code: "SAME_FOLDER" });
    }
    const sourceCaps = await browser.folders.getFolderCapabilities(from.id);
    const destinationCaps = await browser.folders.getFolderCapabilities(to.id);
    if (sourceCaps.canDeleteMessages !== true || destinationCaps.canAddMessages !== true) {
      throw tbxError.blocked("The source must allow removal and the destination must allow messages.", [], { code: "FOLDER_CAPABILITY" });
    }
    return { from, to };
  }

  tbxRegistry.define("messages.batchPreview", async (params) => {
    const input = tbxUtil.need(params, "messageIds", "array");
    if (!input.length || input.some((id) => !Number.isInteger(id) || id < 0)) {
      throw tbxError.usage("messageIds must be non-empty, non-negative integer identifiers.");
    }
    const ids = [...new Set(input)];
    if (ids.length > MAX_MESSAGES) {
      throw tbxError.usage("A batch preview accepts at most 500 unique messages.");
    }
    if (!["mark", "move"].includes(params.action)) {
      throw tbxError.usage("action must be mark or move.");
    }
    if (params.action === "mark" && params.destinationFolderId) {
      throw tbxError.usage("A mark preview cannot have a destination folder.");
    }
    if (params.action === "move" && params.changes && Object.keys(params.changes).length) {
      throw tbxError.usage("A move preview cannot also change flags or tags.");
    }
    const changes = params.action === "mark" ? changesOf(params.changes) : null;
    if (changes) {
      await checkTags(changes);
    }
    const destination = params.action === "move"
      ? await concreteFolder(tbxUtil.need(params, "destinationFolderId", "string"))
      : null;
    if (destination && (!browser.tbx || typeof browser.tbx.invoke !== "function")) {
      throw tbxError.blocked("Native batch moves are unavailable.", [], { code: "UNSUPPORTED_CAPABILITY" });
    }
    const items = [];
    const preview = [];
    const invalid = [];
    for (const id of ids) {
      try {
        const message = await browser.messages.get(id);
        const reference = await tbxReferences.captureHeader(message);
        const item = { action: params.action, reference };
        if (changes) {
          item.desired = desiredState(changes);
          item.previous = markState(message, item.desired);
        } else {
          await verifyMove(reference, destination);
          await tbxRegistry.invoke("x.batch.move", { messageId: id, reference, destination, checkOnly: true });
          item.destination = destination;
        }
        items.push(item);
        if (preview.length < 10) {
          preview.push({ messageId: id, subject: message.subject || "", author: message.author || "",
            folderId: reference.folderId, action: params.action,
            ...(destination ? { destinationFolderId: destination.id } : {}) });
        }
      } catch (error) {
        invalid.push({ messageId: id, code: error.code || "PREVIEW_UNAVAILABLE",
          message: "This message cannot be safely included; refresh its reference or folder capabilities." });
      }
    }
    return { items, preview, invalid, counts: { requested: ids.length, eligible: items.length, invalid: invalid.length } };
  });

  async function mark(reference, expected, desired) {
    let message;
    try {
      message = await tbxReferences.resolve(reference);
    } catch (error) {
      return referenceProblem(error);
    }
    if (!sameState(markState(message, expected), expected)) {
      return problem("conflict", "STATE_CHANGED", "A requested field changed since the preview.");
    }
    if (sameState(markState(message, desired), desired)) {
      return { status: "succeeded", changed: false, reference, previous: expected, current: desired };
    }
    const patch = {};
    for (const key of ["read", "flagged"]) {
      if (key in desired) {
        patch[key] = desired[key];
      }
    }
    if (desired.tags) {
      const tags = new Set(message.tags || []);
      for (const [tag, present] of Object.entries(desired.tags)) {
        if (present) {
          tags.add(tag);
        } else {
          tags.delete(tag);
        }
      }
      patch.tags = [...tags];
    }
    try {
      await browser.messages.update(message.id, patch);
      const after = await tbxReferences.resolve(reference);
      if (!sameState(markState(after, desired), desired)) {
        return problem("unknown", "STATE_NOT_CONFIRMED", "Thunderbird did not confirm the requested values.");
      }
      return { status: "succeeded", changed: true,
        reference: { ...reference, messageId: after.id, runtimeEpoch: tbxRuntimeEpoch },
        previous: expected, current: desired };
    } catch (error) {
      return problem("unknown", "UPDATE_UNCERTAIN", "The update was dispatched but its final result could not be confirmed.");
    }
  }

  function observeMove(messageId) {
    let settle;
    const promise = new Promise((resolve) => { settle = resolve; });
    const unsubscribers = [];
    for (const name of ["messages.onMoved", "messages.onCopied"]) {
      unsubscribers.push(tbxEvents.subscribe(name, (event) => {
        if ((event.originals || []).some((message) => message.id === messageId)) {
          settle({ ...event, name });
        }
      }));
    }
    let timer = null;
    return {
      wait() {
        timer = setTimeout(() => settle(null), MOVE_EVENT_TIMEOUT_MS);
        return promise;
      },
      close() {
        if (timer !== null) {
          clearTimeout(timer);
        }
        unsubscribers.forEach((unsubscribe) => unsubscribe());
      },
    };
  }

  async function move(reference, destination) {
    let message;
    try {
      message = await tbxReferences.resolve(reference);
      await verifyMove(reference, destination);
    } catch (error) {
      return referenceProblem(error);
    }
    if (typeof tbxEvents === "undefined" || typeof tbxEvents.subscribe !== "function") {
      return problem("failed", "EVENTS_UNAVAILABLE", "Move completion tracking is unavailable.");
    }
    const observed = observeMove(message.id);
    let nativeCompleted = false;
    try {
      const native = await tbxRegistry.invoke("x.batch.move", { messageId: message.id, reference, destination });
      nativeCompleted = true;
      let after;
      const proof = native?.nativeMoveObserved;
      if (native?.completed === true && proof?.messageId === message.id &&
          proof.sourceFolderId === reference.folderId && proof.destinationFolderId === destination.id) {
        // A prebound native event correlated the exact source with the target,
        // and the native helper awaited authoritative IMAP destination headers.
        // Resolve all matching headers, rejecting missing or duplicate identities.
        const mapped = await tbxReferences.resolve({ ...reference, accountId: destination.accountId,
          folderId: destination.id, folderPath: destination.path });
        after = await tbxReferences.captureHeader(mapped);
      } else {
        const event = await observed.wait();
        if (!event || event.complete !== true) {
          return problem("unknown", "MOVE_NOT_CONFIRMED", "The move was dispatched but its completion event is incomplete or missing.");
        }
        if (event.name === "messages.onCopied") {
          return problem("unknown", "UNEXPECTED_COPY", "Thunderbird copied instead of moving the message; inspect the source and destination.");
        }
        const matches = [];
        for (const candidate of event.moved || []) {
          if (candidate.folder && candidate.folder.id === destination.id &&
              await tbxReferences.fingerprint(candidate) === reference.fingerprint) {
            matches.push(candidate);
          }
        }
        if (matches.length !== 1) {
          return problem("unknown", "MOVE_MAPPING_AMBIGUOUS", "The moved message could not be uniquely matched to the source.");
        }
        after = await tbxReferences.capture(matches[0].id);
      }
      try {
        await tbxReferences.resolve(reference);
        return problem("unknown", "SOURCE_STILL_PRESENT", "The source message is still present after the move.");
      } catch (error) {
        if (error.code !== "REFERENCE_NOT_FOUND") {
          return problem("unknown", "SOURCE_NOT_CONFIRMED", "The source folder could not be verified after the move.");
        }
      }
      return { status: "succeeded", changed: true, reference: after,
        previousReference: reference, destination };
    } catch (error) {
      if (!nativeCompleted && (String(error.code || "").startsWith("REFERENCE_") || error.code === "UNSUPPORTED_CAPABILITY")) {
        return referenceProblem(error);
      }
      if (error.code === "NATIVE_UNEXPECTED_COPY") {
        return problem("unknown", "UNEXPECTED_COPY", "Thunderbird reported a copy instead of the requested move.");
      }
      if (["NATIVE_TIMEOUT", "NATIVE_REFRESH_FAILED", "NATIVE_MOVE_FAILED", "REFERENCE_AMBIGUOUS", "REFERENCE_NOT_FOUND"].includes(error.code)) {
        return problem("unknown", error.code, "The dispatched move could not be verified. Inspect the source and destination before another write.");
      }
      return problem("unknown", "MOVE_UNCERTAIN", "The move was dispatched but its final result could not be confirmed.");
    } finally {
      observed.close();
    }
  }

  tbxRegistry.define("messages.batchStep", async (params) => {
    const item = params.item;
    if (!item || !item.reference) {
      throw tbxError.usage("A saved batch item is required.");
    }
    if (item.action === "mark" && item.previous && item.desired) {
      return mark(item.reference, item.previous, item.desired);
    }
    if (item.action === "move" && item.destination) {
      return move(item.reference, item.destination);
    }
    throw tbxError.usage("Invalid batch item action or state.");
  });

  tbxRegistry.define("messages.batchUndo", async (params) => {
    const item = params.item;
    const result = params.previousResult;
    if (!item || !result || result.status !== "succeeded" || !result.reference) {
      return problem("failed", "NOT_UNDOABLE", "Only a confirmed successful item can be undone.");
    }
    if (result.changed === false) {
      return { status: "succeeded", changed: false, reference: result.reference };
    }
    if (item.action === "mark" && result.current && result.previous) {
      return mark(result.reference, result.current, result.previous);
    }
    if (item.action === "move" && item.reference) {
      return move(result.reference, { id: item.reference.folderId,
        accountId: item.reference.accountId, path: item.reference.folderPath });
    }
    return problem("failed", "NOT_UNDOABLE", "The successful item has no supported undo data.");
  });
}

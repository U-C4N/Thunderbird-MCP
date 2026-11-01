/* Normalize Thunderbird's paged event lists once. Internal subscribers receive
 * complete headers for operation correlation; public notifications stay small. */
var tbxEvents = (() => {
  let started = false;
  const subscribers = new Map();
  const MAX_EVENT_MESSAGES = 100000;
  function subscribe(name, listener) {
    if (!subscribers.has(name)) subscribers.set(name, new Set());
    subscribers.get(name).add(listener);
    return () => subscribers.get(name)?.delete(listener);
  }
  function notify(name, data, publicData) {
    for (const listener of [...(subscribers.get(name) || [])]) {
      try {
        Promise.resolve(listener(data)).catch((ex) => tbxLog.warn("event subscriber:", String(ex)));
      } catch (ex) { tbxLog.warn("event subscriber:", String(ex)); }
    }
    tbxTransport.emit(name, publicData === undefined ? data : publicData);
  }
  async function readList(first) {
    const messages = [];
    let page = first;
    let listId = null;
    const seenEmpty = new Set();
    const deadline = Date.now() + 60000;
    try {
      for (;;) {
        if (!page || !Array.isArray(page.messages)) throw new Error("invalid MessageList");
        messages.push(...page.messages);
        listId = page.id || null;
        if (!listId) return { messages, complete: true };
        if (messages.length >= MAX_EVENT_MESSAGES || Date.now() >= deadline) throw new Error("event list limit reached");
        if (!page.messages.length && seenEmpty.has(listId)) throw new Error("event list made no progress");
        if (!page.messages.length) seenEmpty.add(listId);
        let timer;
        try {
          page = await Promise.race([
            browser.messages.continueList(listId),
            new Promise((_, reject) => {
              timer = setTimeout(() => reject(new Error("event list timed out")), Math.max(1, deadline - Date.now()));
            }),
          ]);
        } finally { clearTimeout(timer); }
      }
    } catch (ex) {
      if (listId) { try { await browser.messages.abortList(listId); } catch (_) { /* expired */ } }
      return { messages, complete: false, error: String(ex.message || ex) };
    }
  }
  function folderRef(folder) {
    return folder ? { id: folder.id, name: folder.name, path: folder.path, accountId: folder.accountId } : null;
  }
  function listen(event, callback) {
    if (event?.addListener) event.addListener((...args) => Promise.resolve(callback(...args)).catch(
      (ex) => tbxLog.warn("event normalization:", String(ex))
    ));
  }
  function listStatus(result) {
    return { count: result.messages.length, countExact: result.complete,
      ...(result.complete ? {} : { partialReason: result.error }) };
  }
  return {
    subscribe, readList,
    start() {
      if (started) return;
      started = true;
      const messages = browser.messages || {};
      listen(messages.onNewMailReceived, async (folder, list) => {
        const result = await readList(list);
        notify("messages.onNewMailReceived", { folder, ...result }, {
          folder: folderRef(folder), ...listStatus(result),
          preview: result.messages.slice(0, 5).map((m) => ({ id: m.id, subject: m.subject, author: m.author })),
        });
      });
      listen(messages.onUpdated, (message, properties, oldProperties) => {
        notify("messages.onUpdated", { message, properties, oldProperties, complete: true }, { id: message.id, properties });
      });
      for (const [eventName, target] of [["onMoved", "moved"], ["onCopied", "copied"]]) {
        listen(messages[eventName], async (originalList, destinationList) => {
          const [originals, destination] = await Promise.all([readList(originalList), readList(destinationList)]);
          const complete = originals.complete && destination.complete;
          const error = originals.error || destination.error;
          notify(`messages.${eventName}`, { originals: originals.messages, [target]: destination.messages, complete,
            ...(error ? { error } : {}) }, {
            count: destination.messages.length, countExact: complete,
            fromIds: originals.messages.slice(0, 20).map((m) => m.id),
            toIds: destination.messages.slice(0, 20).map((m) => m.id),
            ...(error ? { partialReason: error } : {}),
          });
        });
      }
      listen(messages.onDeleted, async (list) => {
        const result = await readList(list);
        notify("messages.onDeleted", result, { ...listStatus(result), ids: result.messages.slice(0, 20).map((m) => m.id) });
      });
      for (const [namespace, names] of [
        ["folders", ["onCreated", "onRenamed", "onMoved", "onDeleted"]],
        ["accounts", ["onCreated", "onDeleted", "onUpdated"]],
        ["identities", ["onCreated", "onDeleted", "onUpdated"]],
      ]) for (const name of names) listen(browser[namespace]?.[name], (...args) => notify(`${namespace}.${name}`, {
        args: args.map((arg) => arg && typeof arg === "object" && "id" in arg ? { id: arg.id, name: arg.name } : arg),
      }));
      tbxLog.debug("event listeners attached");
    },
  };
})();

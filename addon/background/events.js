/* Thunderbird notifications, forwarded to the daemon's ring buffer.
 *
 * Nothing is pushed at an MCP client uninvited — the daemon buffers these and a
 * tool drains them on request. Payloads stay small (ids and counts, never bodies)
 * because a busy mailbox would otherwise flood the buffer.
 */

var tbxEvents = (() => {
  let started = false;

  function emit(name, data) {
    tbxTransport.emit(name, data);
  }

  function folderRef(folder) {
    if (!folder) {
      return null;
    }
    return { id: folder.id, name: folder.name, path: folder.path, accountId: folder.accountId };
  }

  return {
    start() {
      if (started) {
        return;
      }
      started = true;

      if (browser.messages && browser.messages.onNewMailReceived) {
        browser.messages.onNewMailReceived.addListener((folder, messages) => {
          emit("messages.onNewMailReceived", {
            folder: folderRef(folder),
            count: messages && messages.messages ? messages.messages.length : 0,
            // A couple of subjects make the event actionable without a follow-up read.
            preview: (messages && messages.messages ? messages.messages : [])
              .slice(0, 5)
              .map((m) => ({ id: m.id, subject: m.subject, author: m.author })),
          });
        });
      }

      if (browser.messages && browser.messages.onUpdated) {
        browser.messages.onUpdated.addListener((message, properties) => {
          emit("messages.onUpdated", { id: message.id, properties });
        });
      }

      if (browser.messages && browser.messages.onMoved) {
        browser.messages.onMoved.addListener((originals, moved) => {
          emit("messages.onMoved", {
            count: (moved && moved.length) || 0,
            fromIds: (originals || []).slice(0, 20).map((m) => m.id),
            toIds: (moved || []).slice(0, 20).map((m) => m.id),
          });
        });
      }

      if (browser.messages && browser.messages.onDeleted) {
        browser.messages.onDeleted.addListener((messages) => {
          emit("messages.onDeleted", {
            count: (messages && messages.length) || 0,
            ids: (messages || []).slice(0, 20).map((m) => m.id),
          });
        });
      }

      for (const [namespace, events] of [
        ["folders", ["onCreated", "onRenamed", "onMoved", "onDeleted"]],
        ["accounts", ["onCreated", "onDeleted", "onUpdated"]],
        ["identities", ["onCreated", "onDeleted", "onUpdated"]],
      ]) {
        const api = browser[namespace];
        if (!api) {
          continue;
        }
        for (const eventName of events) {
          const event = api[eventName];
          if (!event || !event.addListener) {
            continue;
          }
          event.addListener((...args) => {
            emit(`${namespace}.${eventName}`, {
              args: args.map((arg) =>
                arg && typeof arg === "object" && "id" in arg
                  ? { id: arg.id, name: arg.name }
                  : arg
              ),
            });
          });
        }
      }

      tbxLog.debug("event listeners attached");
    },
  };
})();

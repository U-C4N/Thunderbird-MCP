/* Runtime receipt cache. Reconnecting a socket must not repeat a mutation. */
var tbxRuntimeEpoch = crypto.randomUUID();
var tbxOperations = (() => {
  const entries = new Map();
  const MAX_RECEIPTS = 10000;
  function key(method, params) {
    return JSON.stringify([method, params.operationId, params.itemId ?? 0]);
  }
  function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.keys(value).sort().map(k => [k, canonical(value[k])]));
    }
    return value;
  }
  async function hash(value) {
    const data = new TextEncoder().encode(JSON.stringify(canonical(value)));
    return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", data)))
      .map(b => b.toString(16).padStart(2, "0")).join("");
  }
  return {
    async run(method, params, fn) {
      if (!params.operationId || method === "operations.receipt") return fn();
      const id = key(method, params);
      const fingerprint = await hash(params);
      const old = entries.get(id);
      if (old) {
        if (old.hash !== fingerprint) throw tbxError.usage("Operation key reused for different content", {code:"REQUEST_KEY_REUSED"});
        return old.promise;
      }
      if (entries.size >= MAX_RECEIPTS) throw tbxError.blocked("Receipt cache is full; restart Thunderbird after current operations finish", [], {code:"RECEIPT_CAPACITY"});
      const entry = {hash:fingerprint, pending:true};
      entries.set(id, entry);
      entry.promise = Promise.resolve().then(fn).then(result => {
        entry.pending = false;
        entry.result = result;
        return result;
      }, error => {
        entry.pending = false;
        entry.error = tbxError.serialize(error);
        throw error;
      });
      return entry.promise;
    },
    receipt(params) {
      const entry = entries.get(key(params.method, params));
      return entry ? {found:true,pending:entry.pending,result:entry.result,error:entry.error,runtimeEpoch:tbxRuntimeEpoch}
        : {found:false,pending:false,runtimeEpoch:tbxRuntimeEpoch};
    },
  };
})();

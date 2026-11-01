/* Bounded, immutable read snapshots shared by search and follow-up tools. */
var tbxSnapshots = (() => {
  const MAX_ITEMS = 1000, MAX_SNAPSHOTS = 16, MAX_BYTES = 32 * 1024 * 1024, TTL = 5 * 60 * 1000;
  const epoch = typeof tbxRuntimeEpoch === "string" ? tbxRuntimeEpoch : `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
  const snapshots = new Map();
  let serial = 0, bytes = 0;
  const clone = (value) => JSON.parse(JSON.stringify(value));
  function remove(id) {
    const old = snapshots.get(id);
    if (old) { bytes -= old.bytes; snapshots.delete(id); }
  }
  function sweep() {
    for (const [id, value] of snapshots) if (Date.now() >= value.expires) remove(id);
  }
  function expired() {
    return tbxError.usage("This result cursor expired or belongs to a previous Thunderbird session; repeat the original search.", { code: "CURSOR_EXPIRED" });
  }
  function page(kind, cursor, limit = 25) {
    sweep();
    const parts = String(cursor).split(":");
    if (parts.length !== 4 || parts[0] !== "tbs" || parts[1] !== epoch || !/^\d+$/.test(parts[3])) throw expired();
    const held = snapshots.get(parts[2]);
    const offset = Number(parts[3]);
    if (!held || held.kind !== kind || !Number.isSafeInteger(offset) || offset > held.items.length) throw expired();
    const size = tbxUtil.limit(limit, 25, 1000);
    const items = held.items.slice(offset, offset + size);
    const next = offset + items.length;
    return clone({ ...held.meta, items, count: items.length, snapshotAt: held.createdAt, hasMore: next < held.items.length,
      ...(next < held.items.length ? { nextCursor: `tbs:${epoch}:${parts[2]}:${next}` } : {}) });
  }
  function begin(kind, items, meta, limit = 25, offset = 0) {
    sweep();
    if (items.length > MAX_ITEMS) throw tbxError.usage("A snapshot can hold at most 1000 results.");
    const encoded = JSON.stringify({ items, meta });
    const size = encoded.length * 2;
    if (size > MAX_BYTES) throw tbxError.usage("Search snapshot exceeds the 32 MiB limit; narrow the scope.");
    while (snapshots.size >= MAX_SNAPSHOTS || bytes + size > MAX_BYTES) remove(snapshots.keys().next().value);
    const id = String(++serial);
    snapshots.set(id, { kind, ...JSON.parse(encoded), bytes: size, expires: Date.now() + TTL, createdAt: new Date().toISOString() });
    bytes += size;
    return page(kind, `tbs:${epoch}:${id}:${Math.min(offset, items.length)}`, limit);
  }
  return { begin, page };
})();

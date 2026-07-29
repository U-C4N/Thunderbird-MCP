/* Tag handlers — official API, with the surface resolved per call.
 *
 * TB 153 declares tag management twice: the flat
 * `browser.messages.listTags/createTag/updateTag/deleteTag` and a newer
 * `browser.messages.tags.*` sub-namespace. On the live 153 build the probe found
 * `browser.messages.tags.list` undefined even with `messagesTags` and
 * `messagesTagsList` granted, so neither surface can be assumed present. Every call
 * therefore picks whichever object actually holds a function, preferring the
 * sub-namespace because that is where the API is heading. Do not collapse this to a
 * single surface without re-probing the target build.
 *
 * Tag keys are permanent: they end up as `mailnews.tags.<key>` preferences and are
 * what a message's keyword header stores. Labels and colours are cosmetic and can be
 * changed freely, which is why `tags.upsert` will rename or recolour but never
 * re-key.
 */

{
  /* Offered to a new tag in order, first unused one wins, so tags the model creates
   * stay visually distinguishable without the caller having to think about colour. */
  const PALETTE = [
    "#1E90FF",
    "#2E8B57",
    "#FF8C00",
    "#8A2BE2",
    "#DC143C",
    "#008B8B",
    "#B8860B",
    "#708090",
  ];

  const COLOR = /^#?([0-9a-f]{6})$/i;
  /* What Thunderbird's own schema refuses in a tag key, plus backslash: a key becomes
   * part of a preference name, and a stray one is a nuisance to clean up by hand. */
  const BAD_KEY = /[ ()/{%*<>"\\]/;

  /**
   * Resolve one tag function. `action` is the sub-namespace name, `legacy` the flat
   * one; a build that has only one of them still works.
   */
  function tagFn(action, legacy) {
    const ns = browser.messages && browser.messages.tags;
    if (ns && typeof ns[action] === "function") {
      return ns[action].bind(ns);
    }
    if (browser.messages && typeof browser.messages[legacy] === "function") {
      return browser.messages[legacy].bind(browser.messages);
    }
    throw tbxError.unsupported(
      `this Thunderbird exposes neither browser.messages.tags.${action} nor ` +
        `browser.messages.${legacy}, so tags cannot be managed. Check that the ` +
        "add-on still holds the messagesTags permission."
    );
  }

  function surface() {
    const ns = browser.messages && browser.messages.tags;
    return ns && typeof ns.list === "function" ? "messages.tags" : "messages.listTags";
  }

  /** MessageTag → our row. Thunderbird calls the human part `tag`; we say `label`. */
  function normalise(tag) {
    return {
      key: tag.key,
      label: tag.tag,
      // Lenient on read: a colour written by an older Thunderbird may be anything.
      color: tag.color ? String(tag.color).toUpperCase() : null,
      ordinal: tag.ordinal || null,
    };
  }

  async function listTags() {
    const raw = await tagFn("list", "listTags")();
    return (raw || []).map(normalise);
  }

  async function readBack(key) {
    const wanted = String(key).toLowerCase();
    return (await listTags()).find((t) => String(t.key).toLowerCase() === wanted) || null;
  }

  /** Strict on write: the tag manager ignores a malformed colour silently. */
  function colour(value) {
    const match = COLOR.exec(String(value).trim());
    if (!match) {
      throw tbxError.usage(`color must be #RRGGBB (for example #FF9900); got ${value}`);
    }
    return `#${match[1].toUpperCase()}`;
  }

  function freeColour(existing) {
    const used = new Set(existing.map((t) => t.color).filter(Boolean));
    return PALETTE.find((c) => !used.has(c)) || PALETTE[0];
  }

  function checkKey(key) {
    if (BAD_KEY.test(key)) {
      throw tbxError.usage(
        `tag keys cannot contain spaces or any of ()/{%*<>"\\ — got ${key}. ` +
          "Omit key and a safe one will be derived from the label."
      );
    }
    return key;
  }

  /** Slug a label into a key nothing else is using. */
  function deriveKey(label, taken) {
    let base = String(label)
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40);
    if (!base) {
      // A label with no ASCII alphanumerics (CJK, emoji) leaves nothing to slug, and
      // a non-ASCII key would be awkward to type into a pref later.
      base = "tag";
    }
    if (!taken.has(base)) {
      return base;
    }
    for (let n = 2; n < 1000; n += 1) {
      const candidate = `${base}-${n}`;
      if (!taken.has(candidate)) {
        return candidate;
      }
    }
    return `${base}-${Date.now()}`;
  }

  // --------------------------------------------------------------------------- read

  tbxRegistry.define("tags.list", async () => {
    const tags = await listTags();
    return { tags, api: surface() };
  });

  // ------------------------------------------------------------------------- upsert

  tbxRegistry.define("tags.upsert", async (params) => {
    const existing = await listTags();
    const byKey = new Map(existing.map((t) => [String(t.key).toLowerCase(), t]));
    const label =
      params.label === undefined || params.label === null || params.label === ""
        ? null
        : String(params.label);
    const requested = params.key ? String(params.key).trim() : "";
    const previous = requested ? byKey.get(requested.toLowerCase()) || null : null;

    if (previous) {
      const properties = {};
      if (label !== null && label !== previous.label) {
        properties.tag = label;
      }
      const wanted = params.color ? colour(params.color) : null;
      if (wanted && wanted !== previous.color) {
        properties.color = wanted;
      }
      if (Object.keys(properties).length === 0) {
        // Already in the requested state. Still answer with a diff so the Python
        // layer reports something truthful rather than claiming a change.
        return { previous, current: previous, created: false, unchanged: true };
      }
      await tagFn("update", "updateTag")(previous.key, properties);
      return { previous, current: (await readBack(previous.key)) || previous, created: false };
    }

    if (label === null) {
      throw tbxError.usage(
        requested
          ? `no tag with key ${requested} exists yet, so label is required to create it`
          : "label is required to create a tag"
      );
    }
    // Honouring a caller-supplied key on create is what keeps this idempotent: a
    // retry after a half-failed create lands on the same tag instead of minting
    // "<key>-2" alongside it.
    const key = requested ? checkKey(requested) : deriveKey(label, new Set(byKey.keys()));
    const color = params.color ? colour(params.color) : freeColour(existing);
    await tagFn("create", "createTag")(key, label, color);
    return {
      previous: null,
      current: (await readBack(key)) || { key, label, color, ordinal: null },
      created: true,
    };
  });

  // ------------------------------------------------------------------------- delete

  tbxRegistry.define("tags.delete", async (params) => {
    const key = tbxUtil.need(params, "key", "string");
    const existing = await listTags();
    const wanted = key.trim().toLowerCase();
    const previous = existing.find((t) => String(t.key).toLowerCase() === wanted) || null;
    if (!previous) {
      throw tbxError.usage(
        `no tag with key ${key}. Defined keys: ${
          existing.map((t) => t.key).join(", ") || "(none)"
        }`
      );
    }
    await tagFn("delete", "deleteTag")(previous.key);
    // Messages keep the raw keyword, so this is recoverable by re-creating the key.
    return { previous, deleted: true };
  });
}

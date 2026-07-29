/* Junk mail — the global bayes preferences and the training corpus.
 *
 * MailServices.junk is the nsIJunkMailPlugin (verified on 153: `userHasClassified`
 * read back `false` on the test profile, so the interface is live before any
 * training has happened). Two things about it are easy to get wrong:
 *
 *   - Marking a message as junk and *training* the filter are different
 *     operations. The junkscore property is what the message list paints;
 *     setMessageClassification is what moves the message into the corpus. Doing
 *     only the first teaches Thunderbird nothing, so junk.train does both.
 *   - A score means nothing until the corpus holds *both* classes. The filter
 *     weighs a message's tokens against ham it has seen as well as spam, so a
 *     profile trained on forty junk messages and no good ones will cheerfully call
 *     everything junk. junk.classify says so in its reply instead of letting the
 *     caller trust the number.
 *
 * Addressing messages: the privileged half never sees WebExtension message ids —
 * those are minted per session by the extension's message tracker and mean nothing
 * over here. Pass one of, in order of preference:
 *
 *   { folderUri, headerMessageIds: ["20260701.a1@example.net"] }
 *   { accountKey, folderPath: "INBOX/Receipts", headerMessageIds: [...] }
 *   { messageUris: ["imap-message://user@host/INBOX#4711"] }
 *
 * `mail_search` returns headerMessageId (and the folder) for every hit, which is
 * what the Python tool forwards. Leaving the folder out is allowed but expensive:
 * we then open every message database in the profile hunting for the Message-ID.
 */

TBX_MODULE_NAMES.push("junk");

{
  const IJUNK = Ci.nsIJunkMailPlugin;
  const JUNK = IJUNK ? IJUNK.JUNK : 100;
  const GOOD = IJUNK ? IJUNK.GOOD : 0;
  const UNCLASSIFIED = IJUNK ? IJUNK.UNCLASSIFIED : -1;

  /** The plugin answers through a listener, and an unreachable message on IMAP can
   *  mean it never answers at all. The daemon's default request deadline is 30 s, so
   *  returning early with "re-issue for the rest" beats being killed mid-batch with
   *  nothing to show for the work already done. */
  const LISTENER_MS = 8000;
  const BATCH_BUDGET_MS = 20000;
  const MAX_MESSAGES = 100;
  const FOLDER_SCAN_LIMIT = 400;

  /** The global half of Thunderbird's junk configuration. Per-account settings
   *  (level, move target, whitelist) belong to x.accounts.junkSettings; the
   *  `default*` keys here are only what a *newly created* account inherits.
   *
   *  Every entry is read through H.readPref, so a key that a future build drops
   *  reports `exists: false` instead of breaking the whole call. */
  const SETTINGS = {
    manualMark: {
      pref: "mail.spam.manualMark",
      type: "bool",
      about: "act on a message when the user marks it junk, not just record it",
    },
    manualMarkMode: {
      pref: "mail.spam.manualMarkMode",
      type: "int",
      values: [0, 1],
      about: "0 = move to the account's junk folder, 1 = delete outright",
    },
    markAsReadOnSpam: {
      pref: "mail.spam.markAsReadOnSpam",
      type: "bool",
      about: "mark a message read when the filter classifies it as junk",
    },
    manualMarkAsJunkMarksRead: {
      pref: "mailnews.ui.junk.manualMarkAsJunkMarksRead",
      type: "bool",
      about: "mark a message read when the *user* marks it junk",
    },
    logging: {
      pref: "mail.spam.logging.enabled",
      type: "bool",
      about: "write junk decisions to the junk log, useful when a filter misbehaves",
    },
    junkThreshold: {
      pref: "mail.adaptivefilters.junk_threshold",
      type: "int",
      min: 50,
      max: 100,
      about: "percentage at or above which the adaptive filter calls a message junk",
    },
    maxTokens: {
      pref: "mailnews.bayesian_spam_filter.junk_maxtokens",
      type: "int",
      min: 1000,
      max: 1000000,
      about: "corpus size ceiling; larger learns more and costs more memory",
    },
    engineVersion: {
      pref: "mail.spam.version",
      type: "int",
      readOnly: true,
      about: "Thunderbird's own junk-settings migration version",
    },
    defaultLevel: {
      pref: "mail.server.default.spamLevel",
      type: "int",
      min: 0,
      max: 100,
      about: "junk filtering on (100) or off (0) for accounts created from now on",
    },
    defaultMoveOnSpam: {
      pref: "mail.server.default.moveOnSpam",
      type: "bool",
      about: "new accounts move junk to their junk folder",
    },
    defaultPurge: {
      pref: "mail.server.default.purgeSpam",
      type: "bool",
      about: "new accounts delete old junk automatically",
    },
    defaultPurgeInterval: {
      pref: "mail.server.default.purgeSpamInterval",
      type: "int",
      min: 1,
      max: 3650,
      about: "days of junk a new account keeps before purging",
    },
    defaultUseWhiteList: {
      pref: "mail.server.default.useWhiteList",
      type: "bool",
      about: "new accounts exempt senders in the address book",
    },
  };

  function plugin() {
    const junk = H.junk;
    if (!junk) {
      throw H.unsupported(
        "MailServices.junk is missing, so this build has no bayesian junk filter"
      );
    }
    return junk;
  }

  /* ------------------------------------------------------------------ settings */

  function readSetting(key) {
    const spec = SETTINGS[key];
    const info = H.readPref(spec.pref);
    const out = {
      pref: spec.pref,
      type: spec.type,
      value: info.type === "none" ? null : info.value,
      exists: info.type !== "none",
      userSet: info.set,
      writable: !spec.readOnly,
      about: spec.about,
    };
    if (spec.values) {
      out.allowed = spec.values;
    }
    if (spec.min !== undefined) {
      out.range = [spec.min, spec.max];
    }
    return out;
  }

  function coerce(key, spec, value) {
    if (spec.type === "bool") {
      if (typeof value !== "boolean") {
        throw H.usage(`${key} is a boolean; got ${JSON.stringify(value)}`);
      }
      return value;
    }
    if (!Number.isInteger(value)) {
      throw H.usage(`${key} is an integer; got ${JSON.stringify(value)}`);
    }
    if (spec.values && !spec.values.includes(value)) {
      throw H.usage(`${key} must be one of ${spec.values.join(", ")}; got ${value}`);
    }
    if (spec.min !== undefined && (value < spec.min || value > spec.max)) {
      throw H.usage(`${key} must be between ${spec.min} and ${spec.max}; got ${value}`);
    }
    return value;
  }

  /** The corpus lives in one file in the profile; its size is the only honest
   *  evidence of how much training exists, since the plugin exposes no token count. */
  async function corpusFile() {
    const path = PathUtils.join(
      Services.dirsvc.get("ProfD", Ci.nsIFile).path,
      "training.dat"
    );
    try {
      const info = await IOUtils.stat(path);
      return {
        path,
        exists: true,
        bytes: info.size,
        modified: info.lastModified ? new Date(info.lastModified).toISOString() : null,
      };
    } catch (ex) {
      return { path, exists: false, bytes: 0, modified: null };
    }
  }

  TBX_MODULES["junk.getSettings"] = async () => {
    const junk = plugin();
    const settings = {};
    for (const key of Object.keys(SETTINGS)) {
      settings[key] = readSetting(key);
    }
    return {
      settings,
      training: {
        userHasClassified: H.safeGet(junk, "userHasClassified"),
        corpus: await corpusFile(),
      },
      note:
        "These preferences are global. Per-account junk handling — level, move " +
        "target, whitelist, purge — is x.accounts.junkSettings.",
    };
  };

  TBX_MODULES["junk.setSettings"] = async (params) => {
    const wanted = H.need(params, "settings");
    if (typeof wanted !== "object" || Array.isArray(wanted)) {
      throw H.usage(
        'settings must be an object, e.g. {"manualMark": true, "junkThreshold": 90}'
      );
    }
    const keys = Object.keys(wanted);
    if (!keys.length) {
      throw H.usage(`settings was empty; valid keys are ${Object.keys(SETTINGS).join(", ")}`);
    }

    // Validate every key before writing any of them: a typo in the third key must
    // not leave the first two applied and the caller guessing.
    const planned = [];
    for (const key of keys) {
      const spec = SETTINGS[key];
      if (!spec) {
        throw H.usage(
          `unknown junk setting ${key} (valid: ${Object.keys(SETTINGS).join(", ")})`
        );
      }
      if (spec.readOnly) {
        throw H.usage(`${key} is reported for information only and cannot be written`);
      }
      planned.push({ key, spec, value: coerce(key, spec, wanted[key]) });
    }

    const changes = [];
    const unchanged = [];
    for (const item of planned) {
      const before = readSetting(item.key);
      if (before.exists && before.value === item.value) {
        unchanged.push(item.key);
        continue;
      }
      H.writePref(item.spec.pref, item.spec.type, item.value);
      changes.push({
        key: item.key,
        pref: item.spec.pref,
        created: !before.exists,
        previous: { value: before.value, userSet: before.userSet, existed: before.exists },
        current: { value: item.value },
      });
    }
    if (changes.length) {
      H.flushPrefs();
    }

    const settings = {};
    for (const item of planned) {
      settings[item.key] = readSetting(item.key);
    }
    return { changed: changes, unchanged, settings };
  };

  /* ------------------------------------------------------------ message lookup */

  function asArray(value, name) {
    if (value === undefined || value === null) {
      return [];
    }
    if (!Array.isArray(value)) {
      throw H.usage(`${name} must be an array`);
    }
    return value;
  }

  /** Message-IDs are stored without their angle brackets; callers paste them either
   *  way round. */
  function bareMessageId(value) {
    return String(value).trim().replace(/^<|>$/g, "");
  }

  function folderFromUri(uri) {
    const text = String(uri).trim();
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) {
      throw H.usage(
        `folderUri must be a Thunderbird folder URI such as imap://user@host/INBOX ` +
          `or mailbox://nobody@Local%20Folders/Junk; got ${text}. Pass accountKey ` +
          "plus folderPath if you only have a display path."
      );
    }
    const folder = needMod("MailUtils").getExistingFolder(text);
    if (!folder) {
      throw H.usage(`no folder is subscribed at ${text}`);
    }
    return folder;
  }

  function folderFromPath(accountKey, path) {
    const server = H.account(String(accountKey)).incomingServer;
    if (!server) {
      throw H.usage(`account ${accountKey} has no incoming server, so it holds no mail`);
    }
    let folder = server.rootFolder;
    for (const segment of String(path).split("/").filter(Boolean)) {
      let child = null;
      try {
        child = folder.getChildNamed(segment);
      } catch (ex) {
        child = null;
      }
      if (!child) {
        const known = [...folder.subFolders].map((f) => f.name).join(", ");
        throw H.usage(
          `${folder.URI} has no subfolder named ${segment} (has: ${known || "none"})`
        );
      }
      folder = child;
    }
    return folder;
  }

  function resolveFolder(params) {
    const uri = params.folderUri || params.folderId || params.folder;
    if (uri) {
      return folderFromUri(uri);
    }
    if (params.folderPath) {
      return folderFromPath(H.need(params, "accountKey"), params.folderPath);
    }
    return null;
  }

  /** Every folder that can own a message database. There is no index from
   *  Message-ID to folder, so a caller who omits folderUri pays for this walk. */
  function messageFolders() {
    const out = [];
    for (const account of H.accounts.accounts) {
      let root = null;
      try {
        root = account.incomingServer ? account.incomingServer.rootFolder : null;
      } catch (ex) {
        root = null; // a half-configured account should not abort the search
      }
      if (!root) {
        continue;
      }
      for (const folder of root.descendants) {
        // Virtual folders have no database of their own to search.
        if (folder.flags & Ci.nsMsgFolderFlags.Virtual) {
          continue;
        }
        out.push(folder);
      }
    }
    return out;
  }

  function describe(hdr) {
    const folder = hdr.folder;
    return {
      hdr,
      uri: folder.getUriForMsg(hdr),
      headerMessageId: hdr.messageId,
      subject: hdr.mime2DecodedSubject,
      author: hdr.mime2DecodedAuthor,
      folderUri: folder.URI,
      folderName: folder.localizedName || folder.prettyName || folder.name,
      junkScore: hdr.getStringProperty("junkscore") || null,
      junkScoreOrigin: hdr.getStringProperty("junkscoreorigin") || null,
    };
  }

  function headerFromMessageUri(uri) {
    try {
      const service = needMod("MailServices").messageServiceFromURI(uri);
      return service.messageURIToMsgHdr(uri) || null;
    } catch (ex) {
      return null;
    }
  }

  function headerFromMessageId(folders, id) {
    for (const folder of folders) {
      let hdr = null;
      try {
        // Touching msgDatabase opens it; Thunderbird's own cache decides when to
        // let it go again, which is why the fallback walk is capped.
        hdr = folder.msgDatabase.getMsgHdrForMessageID(id);
      } catch (ex) {
        hdr = null;
      }
      if (hdr) {
        return hdr;
      }
    }
    return null;
  }

  function resolveMessages(params) {
    const uris = asArray(params.messageUris, "messageUris");
    const ids = asArray(params.headerMessageIds, "headerMessageIds");
    if (!uris.length && !ids.length) {
      throw H.usage(
        "pass headerMessageIds (with folderUri, ideally) or messageUris — " +
          "WebExtension message ids do not resolve in the privileged half"
      );
    }
    if (uris.length + ids.length > MAX_MESSAGES) {
      throw H.usage(
        `at most ${MAX_MESSAGES} messages per call; split the batch so one request ` +
          "cannot outlive its deadline"
      );
    }

    const messages = [];
    const unresolved = [];
    for (const uri of uris) {
      const hdr = headerFromMessageUri(String(uri));
      if (hdr) {
        messages.push(describe(hdr));
      } else {
        unresolved.push({ messageUri: uri, reason: "no message at that URI" });
      }
    }

    if (ids.length) {
      const folder = resolveFolder(params);
      const candidates = folder ? [folder] : messageFolders().slice(0, FOLDER_SCAN_LIMIT);
      for (const raw of ids) {
        const id = bareMessageId(raw);
        const hdr = id ? headerFromMessageId(candidates, id) : null;
        if (hdr) {
          messages.push(describe(hdr));
        } else {
          unresolved.push({
            headerMessageId: raw,
            reason: folder
              ? `not found in ${folder.URI}`
              : `not found in the ${candidates.length} folders searched — pass folderUri`,
          });
        }
      }
    }
    return { messages, unresolved };
  }

  function publicShape(message) {
    const { hdr, ...rest } = message; // nsIMsgDBHdr does not belong on the wire
    return rest;
  }

  /* ---------------------------------------------------------------- classifying */

  /** Wrap nsIJunkMailClassificationListener as a promise.
   *
   *  The plugin reports one callback per message, and on some builds a final one
   *  with an empty URI to close the batch; resolving on either survives both.
   *  A deadline that resolves rather than rejects is deliberate — a message the
   *  plugin cannot read (no offline copy, server gone) simply never comes back, and
   *  the results we did collect are still worth returning. */
  function awaitClassifications(uris, run, deadlineMs) {
    return new Promise((resolve, reject) => {
      const scores = new Map();
      let settled = false;
      const finish = () => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          resolve(scores);
        }
      };
      const timer = setTimeout(finish, deadlineMs);
      const listener = {
        QueryInterface: ChromeUtils.generateQI(["nsIJunkMailClassificationListener"]),
        onMessageClassified(uri, classification, junkPercent) {
          if (uri) {
            scores.set(uri, { classification, junkPercent });
          }
          if (!uri || scores.size >= uris.length) {
            finish();
          }
        },
      };
      try {
        run(listener);
      } catch (ex) {
        settled = true;
        clearTimeout(timer);
        reject(ex);
      }
    });
  }

  function label(classification) {
    if (classification === JUNK) {
      return "junk";
    }
    if (classification === GOOD) {
      return "good";
    }
    return "unknown";
  }

  function threshold() {
    return Services.prefs.getIntPref("mail.adaptivefilters.junk_threshold", 90);
  }

  /** What the corpus already believes about this message, so the plugin can retract
   *  the old training instead of double-counting it. Only a user-set score counts:
   *  the filter's own guesses were never training data. */
  function previousClassification(hdr) {
    const score = hdr.getStringProperty("junkscore");
    const origin = hdr.getStringProperty("junkscoreorigin");
    if (!score || origin !== "user") {
      return UNCLASSIFIED;
    }
    return Number(score) >= 50 ? JUNK : GOOD;
  }

  /** The message list reads the junkscore property, not the corpus. Writing it here
   *  is what makes a trained message *look* trained. Moving or deleting it is not
   *  our business — that is mail_move / mail_delete, where the user gets a say. */
  function writeJunkScore(hdr, classification) {
    const score = classification === JUNK ? "100" : "0";
    hdr.setStringProperty("junkscore", score);
    hdr.setStringProperty("junkscoreorigin", "user");
    return score;
  }

  TBX_MODULES["junk.train"] = async (params) => {
    const junk = plugin();
    const wanted = String(H.need(params, "classification")).toLowerCase();
    if (wanted !== "junk" && wanted !== "good") {
      throw H.usage(`classification must be "junk" or "good"; got ${JSON.stringify(params.classification)}`);
    }
    const target = wanted === "junk" ? JUNK : GOOD;
    const resolved = resolveMessages(params);
    if (!resolved.messages.length) {
      throw H.usage(
        "none of those messages could be resolved, so there is nothing to train on"
      );
    }

    const started = Date.now();
    const trained = [];
    const deferred = [];
    for (const message of resolved.messages) {
      if (Date.now() - started > BATCH_BUDGET_MS) {
        deferred.push(publicShape(message));
        continue;
      }
      // One message at a time: the callback identifies its message only by URI, and
      // the plugin tokenises serially anyway, so a parallel burst buys nothing and
      // makes the accounting ambiguous.
      const scores = await awaitClassifications(
        [message.uri],
        (listener) =>
          junk.setMessageClassification(
            message.uri,
            previousClassification(message.hdr),
            target,
            null,
            listener
          ),
        LISTENER_MS
      );
      const written = writeJunkScore(message.hdr, target);
      const reported = scores.get(message.uri);
      trained.push({
        ...publicShape(message),
        junkScore: written,
        junkScoreOrigin: "user",
        confirmed: Boolean(reported),
      });
    }

    return {
      classification: wanted,
      trained: trained.length,
      messages: trained,
      deferred,
      unresolved: resolved.unresolved,
      userHasClassified: H.safeGet(junk, "userHasClassified"),
      corpus: await corpusFile(),
      hint: deferred.length
        ? "the request budget ran out; re-issue with the deferred messages"
        : undefined,
      note:
        "Scoring stays unreliable until the corpus holds a useful number of both " +
        "junk and good messages — train both classes, not just the junk.",
    };
  };

  TBX_MODULES["junk.classify"] = async (params) => {
    const junk = plugin();
    const resolved = resolveMessages(params);
    if (!resolved.messages.length) {
      throw H.usage("none of those messages could be resolved, so there is nothing to score");
    }
    const uris = resolved.messages.map((m) => m.uri);
    // classifyMessages only reads: it reports through the listener and writes no
    // junkscore, which is exactly what makes this handler safe to call speculatively.
    const scores = await awaitClassifications(
      uris,
      (listener) => junk.classifyMessages(uris, null, listener),
      BATCH_BUDGET_MS
    );

    const limit = threshold();
    const results = [];
    const timedOut = [];
    for (const message of resolved.messages) {
      const reported = scores.get(message.uri);
      if (!reported) {
        timedOut.push(publicShape(message));
        continue;
      }
      results.push({
        ...publicShape(message),
        classification: label(reported.classification),
        junkPercent: reported.junkPercent,
        overThreshold: reported.junkPercent >= limit,
      });
    }

    const isTrained = H.safeGet(junk, "userHasClassified");
    return {
      messages: results,
      timedOut,
      unresolved: resolved.unresolved,
      threshold: limit,
      userHasClassified: isTrained,
      note:
        isTrained === true
          ? "Nothing was changed; these are the scores the filter would use."
          : "The filter has never been trained, so these scores carry no information. " +
            "Use junk.train on a sample of both junk and good mail first.",
    };
  };

  TBX_MODULES["junk.resetTraining"] = async (params) => {
    const junk = plugin();
    if (params.confirm !== true) {
      throw H.blocked(
        "resetting the junk corpus discards everything Thunderbird has learned " +
          "about this user's mail and cannot be undone",
        "confirm=true"
      );
    }
    const before = { userHasClassified: H.safeGet(junk, "userHasClassified"), corpus: await corpusFile() };
    junk.resetTrainingData();
    return {
      reset: true,
      previous: before,
      current: {
        userHasClassified: H.safeGet(junk, "userHasClassified"),
        corpus: await corpusFile(),
      },
      note:
        "Existing junkscore marks on messages are untouched; only the corpus is " +
        "gone. Retrain before trusting junk.classify again.",
    };
  };
}

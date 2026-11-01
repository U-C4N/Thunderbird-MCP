/* Local file access for the bridge: read, write, stat.
 *
 * `files.read` exists so compose can attach a file the user names by path, and
 * `files.write` is what saving an attachment lands on. There is deliberately no
 * delete: nothing in this project needs one, and a privileged unlink driven by a
 * language model has a blast radius no confirmation prompt can shrink.
 *
 * core.js keeps its own `writeFile` because that one is declared in
 * experiment/schema.json and the background page calls it directly while saving an
 * attachment. `files.write` is the same operation reached through `invoke`, for the
 * Python layer. Two doors, one behaviour.
 */

TBX_MODULE_NAMES.push("files");

{
  /** ~25 MB. Above this a read is almost certainly a mistake: the bytes travel as
   *  base64 in a JSON frame (a third bigger again) and have to be chunked across the
   *  socket before anything can use them. */
  const MAX_READ_BYTES = 25 * 1024 * 1024;

  /** base64 through ChromeUtils, which core.js documents as injected here; btoa and
   *  atob are not part of what the extension sandbox guarantees. base64url differs
   *  from base64 in exactly two characters, so the swap is lossless. */
  function toBase64(bytes) {
    return ChromeUtils.base64URLEncode(bytes, { pad: true })
      .replace(/-/g, "+")
      .replace(/_/g, "/");
  }

  function fromBase64(text) {
    const url = String(text)
      .replace(/\s+/g, "")
      .replace(/\+/g, "-")
      .replace(/\//g, "_");
    try {
      return new Uint8Array(ChromeUtils.base64URLDecode(url, { padding: "ignore" }));
    } catch (ex) {
      throw H.usage("base64 was not decodable — send standard base64 for the file bytes");
    }
  }

  function mimeFor(name) {
    const dot = String(name).lastIndexOf(".");
    if (dot < 1) {
      return "application/octet-stream";
    }
    try {
      return Cc["@mozilla.org/mime;1"]
        .getService(Ci.nsIMIMEService)
        .getTypeFromExtension(String(name).slice(dot + 1));
    } catch (ex) {
      // Unknown extensions throw NS_ERROR_NOT_AVAILABLE rather than returning null.
      return "application/octet-stream";
    }
  }

  /** IOUtils rejects relative paths with an opaque error; say so in the caller's
   *  terms instead, since the model is the one that chose the path. */
  function absolutePath(params, field) {
    const path = String(H.need(params, field));
    if (!PathUtils.isAbsolute(path)) {
      throw H.usage(`${field} must be an absolute path; got ${path}`);
    }
    return path;
  }

  async function statOrNull(path) {
    try {
      return await IOUtils.stat(path);
    } catch (ex) {
      return null;
    }
  }

  TBX_MODULES["files.read"] = async (params) => {
    const path = absolutePath(params, "path");
    const info = await statOrNull(path);
    if (!info) {
      throw H.usage(`no such file: ${path}`);
    }
    if (info.type === "directory") {
      throw H.usage(`${path} is a directory — name a file inside it`);
    }
    if (info.size > MAX_READ_BYTES) {
      throw H.blocked(
        `${path} is ${info.size} bytes, over the ${MAX_READ_BYTES}-byte ceiling for a ` +
          "file read across the bridge",
        "attach it from Thunderbird's own compose window, or point at a smaller file"
      );
    }
    const bytes = await IOUtils.read(path);
    const name = PathUtils.filename(path);
    return {
      path,
      name,
      bytes: bytes.length,
      contentType: mimeFor(name),
      base64: toBase64(bytes),
    };
  };

  TBX_MODULES["files.stat"] = async (params) => {
    const path = absolutePath(params, "path");
    const info = await statOrNull(path);
    if (!info) {
      // Not an error: "is this there?" is the question stat exists to answer.
      return { path, exists: false };
    }
    const name = PathUtils.filename(path);
    const isFile = info.type === "regular";
    return {
      path,
      exists: true,
      name,
      parent: PathUtils.parent(path),
      isFile,
      isDirectory: info.type === "directory",
      bytes: isFile ? info.size : null,
      modified: info.lastModified ? new Date(info.lastModified).toISOString() : null,
      contentType: isFile ? mimeFor(name) : null,
      readableByBridge: isFile && info.size <= MAX_READ_BYTES,
    };
  };

  function safeFilename(value) {
    let name = String(value || "attachment").normalize("NFC")
      .replace(/[<>:"/\\|?*\x00-\x1f\x7f]/g, "_").replace(/[. ]+$/g, "").trim();
    if (!name || name === "." || name === "..") name = "attachment";
    if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(name)) name = `_${name}`;
    const dot = name.lastIndexOf(".");
    const extension = dot > 0 && name.length - dot <= 20 ? name.slice(dot) : "";
    const stem = extension ? name.slice(0, -extension.length) : name;
    return `${Array.from(stem).slice(0, 180 - extension.length).join("")}${extension}`;
  }

  async function directoryPath(params) {
    const directory = absolutePath(params, "directory");
    const info = await statOrNull(directory);
    if (!info || info.type !== "directory") throw H.usage(`directory does not exist or is not a directory: ${directory}`);
    return directory;
  }

  async function fileHash(path) {
    if (IOUtils.computeHexDigest) return IOUtils.computeHexDigest(path, "sha256");
    const bytes = await IOUtils.read(path, { maxBytes: MAX_READ_BYTES + 1 });
    if (bytes.length > MAX_READ_BYTES) throw H.usage("File exceeds the 25 MiB hashing limit.");
    const hash = Cc["@mozilla.org/security/hash;1"].createInstance(Ci.nsICryptoHash);
    hash.init(hash.SHA256);
    hash.update(bytes, bytes.length);
    return Array.from(hash.finish(false), (c) => c.charCodeAt(0).toString(16).padStart(2, "0")).join("");
  }

  TBX_MODULES["files.prepareNames"] = async (params) => {
    const directory = await directoryPath(params);
    if (!Array.isArray(params.names) || params.names.length > 200) throw H.usage("names must be an array of at most 200 filenames.");
    const reserved = new Set();
    const files = [];
    for (const originalName of params.names) {
      const clean = safeFilename(originalName);
      const dot = clean.lastIndexOf(".");
      const extension = dot > 0 && clean.length - dot <= 20 ? clean.slice(dot) : "";
      const stem = extension ? clean.slice(0, -extension.length) : clean;
      let name = clean, path;
      let suffix = 1;
      for (;;) {
        path = PathUtils.join(directory, name);
        if (!reserved.has(name.toLocaleLowerCase("en-US")) && !(await IOUtils.exists(path))) break;
        if (++suffix > 10000) throw H.usage("Too many colliding attachment filenames; choose another directory.");
        const tail = ` (${suffix})`;
        name = `${Array.from(stem).slice(0, 180 - extension.length - tail.length).join("")}${tail}${extension}`;
      }
      reserved.add(name.toLocaleLowerCase("en-US"));
      files.push({ originalName: String(originalName || ""), name, path });
    }
    return { directory, files };
  };

  H.writeFileAtomic = async (params) => {
    const directory = await directoryPath(params);
    const filename = String(H.need(params, "filename"));
    if (/[\\/\x00]/.test(filename) || filename === "." || filename === ".." || /:/.test(filename)) {
      throw H.usage("filename must be a bare name, with no path separator or stream suffix");
    }
    const path = PathUtils.join(directory, filename);
    if (!params.overwrite && await IOUtils.exists(path)) throw H.blocked(`${path} already exists`, "overwrite=true, or a different filename");
    const bytes = fromBase64(params.base64 ?? "");
    // The unique temporary lives beside its destination, so the final move is
    // atomic. Only this exact internally-created temporary may be removed.
    const temporary = await IOUtils.createUniqueFile(directory, ".tbmcp-", 0o600);
    let created = true;
    try {
      await IOUtils.write(temporary, bytes, { flush: true });
      const sha256 = params.hash ? await fileHash(temporary) : null;
      if (params.expectedSha256 && sha256 !== params.expectedSha256) throw H.blocked("Attachment content changed before writing.", [], { code: "ATTACHMENT_CHANGED" });
      try { await IOUtils.move(temporary, path, { noOverwrite: !params.overwrite }); }
      catch (ex) {
        if (!params.overwrite && await IOUtils.exists(path)) throw H.blocked(`${path} already exists`, "overwrite=true, or a different filename");
        throw ex;
      }
      created = false;
      return { path, name: filename, bytes: bytes.length, ...(sha256 ? { sha256 } : {}) };
    } finally {
      if (created) { try { await IOUtils.remove(temporary, { ignoreAbsent: true }); } catch (_) { /* only our temporary */ } }
    }
  };
  TBX_MODULES["files.write"] = (params) => H.writeFileAtomic(params);
  TBX_MODULES["files.hash"] = async (params) => {
    const path = absolutePath(params, "path");
    const info = await statOrNull(path);
    if (!info) return { path, exists: false };
    if (info.type !== "regular" || info.size > MAX_READ_BYTES) return { path, exists: true, hashAvailable: false };
    return { path, exists: true, bytes: info.size, sha256: await fileHash(path) };
  };
  if (typeof TBX_TEST_HOOKS !== "undefined") TBX_TEST_HOOKS.files = { safeFilename };
}

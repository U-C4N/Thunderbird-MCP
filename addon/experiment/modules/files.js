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

  TBX_MODULES["files.write"] = async (params) => {
    const directory = absolutePath(params, "directory");
    const filename = String(H.need(params, "filename"));
    const base64 = H.need(params, "base64");
    // The model chose this name, so treat a separator as attempted traversal rather
    // than a typo.
    if (/[\\/]/.test(filename) || filename === "." || filename === "..") {
      throw H.usage("filename must be a bare name, with no path separator");
    }
    const info = await statOrNull(directory);
    if (!info) {
      throw H.usage(`directory does not exist: ${directory}`);
    }
    if (info.type !== "directory") {
      throw H.usage(`${directory} is not a directory`);
    }
    const path = PathUtils.join(directory, filename);
    if (!params.overwrite && (await IOUtils.exists(path))) {
      throw H.blocked(`${path} already exists`, "overwrite=true, or a different filename");
    }
    const bytes = fromBase64(base64);
    // Write via a temp file and rename: a half-written attachment on disk is worse
    // than none, and the caller has already been told the write succeeded.
    await IOUtils.write(path, bytes, { tmpPath: `${path}.tmp` });
    return { path, name: filename, bytes: bytes.length };
  };
}

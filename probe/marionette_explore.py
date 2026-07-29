"""Resolve the API shapes the capability probe left ambiguous (calendar item
creation, OpenPGP key listing, virtual folders, server/identity property names)."""

from __future__ import annotations

import json
import sys

from marionette_probe import Marionette

SCRIPT = r"""
const done = arguments[arguments.length - 1];
const attempt = fn => { try { const v = fn(); return v === undefined ? true : v; }
                        catch (e) { return { __error: String(e && e.message || e) }; } };
const imp = url => { try { return ChromeUtils.importESModule(url); } catch (e) { return null; } };

(async () => {
  const out = {};
  const { MailServices } = ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");

  // --- calendar: how do we create an event/task in TB 153? -----------------
  const calMod = imp("resource:///modules/calendar/calUtils.sys.mjs");
  const cal = calMod && calMod.cal;
  out.calendar = {
    calKeys: cal ? Object.keys(cal).sort() : null,
    itemKeys: cal && cal.item ? Object.keys(cal.item).sort() : null,
    createEventType: cal ? typeof cal.createEvent : null,
    calEventModule: attempt(() => Object.keys(imp("resource:///modules/CalEvent.sys.mjs") || {})),
    calTodoModule: attempt(() => Object.keys(imp("resource:///modules/CalTodo.sys.mjs") || {})),
    calendars: attempt(() => cal.manager.getCalendars().map(c => ({
      id: c.id, type: c.type, name: c.name, readOnly: c.readOnly, uri: c.uri && c.uri.spec,
    }))),
    managerKeys: attempt(() => Object.keys(cal.manager).filter(k => typeof cal.manager[k] === "function").sort()),
  };

  // --- OpenPGP: list keys -------------------------------------------------
  out.openpgp = {
    rnpKeys: attempt(() => {
      const { RNP } = imp("chrome://openpgp/content/modules/RNP.sys.mjs");
      return Object.getOwnPropertyNames(RNP).filter(k => typeof RNP[k] === "function").sort();
    }),
    keyRingModule: attempt(() => {
      const m = imp("chrome://openpgp/content/modules/keyRing.sys.mjs");
      return m ? Object.keys(m) : null;
    }),
    keyCount: attempt(() => {
      const m = imp("chrome://openpgp/content/modules/keyRing.sys.mjs");
      const ring = m && (m.EnigmailKeyRing || m.KeyRing);
      return ring && typeof ring.getAllKeys === "function"
        ? (ring.getAllKeys().keyList || []).length : "no getAllKeys";
    }),
  };

  // --- virtual folders (saved searches) ------------------------------------
  out.virtualFolders = attempt(() => {
    const { VirtualFolderHelper } = imp("resource:///modules/VirtualFolderWrapper.sys.mjs");
    return {
      statics: Object.getOwnPropertyNames(VirtualFolderHelper)
        .filter(k => typeof VirtualFolderHelper[k] === "function").sort(),
    };
  });

  // --- incoming server settings surface -----------------------------------
  out.server = attempt(() => {
    const server = MailServices.accounts.accounts[0].incomingServer;
    const scalar = {};
    for (const key of ["type", "hostName", "port", "username", "socketType", "authMethod",
                       "prettyName", "biffMinutes", "doBiff", "downloadOnBiff", "loginAtStartUp",
                       "limitOfflineMessageSize", "maxMessageSize", "emptyTrashOnExit",
                       "canBeDefaultServer", "isSecure", "offlineSupportLevel"]) {
      try { const v = server[key]; scalar[key] = typeof v === "object" ? "<obj>" : v; }
      catch (e) { scalar[key] = { __error: String(e.message || e) }; }
    }
    // IMAP-specific and spam settings live on separate interfaces.
    let imapKeys = null;
    try {
      const imap = server.QueryInterface(Ci.nsIImapIncomingServer);
      imapKeys = { useIdle: imap.useIdle, maximumConnectionsNumber: imap.maximumConnectionsNumber,
                   forceSelect: imap.forceSelect, cleanupInboxOnExit: imap.cleanupInboxOnExit };
    } catch (e) { imapKeys = { __error: String(e.message || e) }; }
    let spam = null;
    try {
      const s = server.spamSettings;
      spam = { level: s.level, moveOnSpam: s.moveOnSpam, moveTargetMode: s.moveTargetMode,
               purge: s.purge, purgeInterval: s.purgeInterval, useWhiteList: s.useWhiteList,
               whiteListAbURI: s.whiteListAbURI, manualMark: s.manualMark };
    } catch (e) { spam = { __error: String(e.message || e) }; }
    return { scalar, imap: imapKeys, spamSettings: spam };
  });

  // --- identity + outgoing server surface ----------------------------------
  out.identity = attempt(() => {
    const id = MailServices.accounts.allIdentities[0];
    const keys = {};
    for (const key of ["key", "fullName", "email", "replyTo", "organization", "composeHtml",
                       "attachSignature", "sigBottom", "htmlSigText", "htmlSigFormat",
                       "attachVCard", "escapedVCard", "smtpServerKey", "doBcc", "doBccList",
                       "draftFolder", "fccFolder", "archiveFolder", "stationeryFolder",
                       "archiveEnabled", "catchAll", "catchAllHint", "signature"]) {
      try { const v = id[key]; keys[key] = typeof v === "object" && v !== null ? "<obj>" : v; }
      catch (e) { keys[key] = { __error: String(e.message || e) }; }
    }
    return keys;
  });
  out.outgoing = attempt(() => {
    const svc = MailServices.outgoingServer;
    return {
      serviceKeys: Object.getOwnPropertyNames(svc).sort().slice(0, 30),
      servers: [...svc.servers].map(s => ({
        key: s.key, type: s.type, description: s.description,
        UID: s.UID, serverURI: s.serverURI,
      })),
    };
  });

  // --- MsgUtils / compose-without-UI module paths --------------------------
  out.moduleProbe = {};
  for (const url of ["resource:///modules/MsgUtils.sys.mjs",
                     "resource:///modules/MimeMessage.sys.mjs",
                     "resource:///modules/MessageSend.sys.mjs",
                     "resource:///modules/MailStringUtils.sys.mjs",
                     "resource:///modules/FilterEditor.sys.mjs",
                     "resource:///modules/MsgIncomingServer.sys.mjs"]) {
    const m = imp(url);
    out.moduleProbe[url] = m ? Object.keys(m) : false;
  }

  // --- tags service surface -------------------------------------------------
  out.tags = attempt(() => MailServices.tags.getAllTags().map(t => ({
    key: t.key, tag: t.tag, color: t.color, ordinal: t.ordinal,
  })));

  done(out);
})().catch(e => done({ fatalError: String(e && e.stack || e) }));
"""


def main() -> int:
    client = Marionette()
    client.send("WebDriver:NewSession", {"capabilities": {}})
    client.send("Marionette:SetContext", {"value": "chrome"})
    result = client.send(
        "WebDriver:ExecuteAsyncScript",
        {"script": SCRIPT, "args": [], "newSandbox": False, "scriptTimeout": 60000},
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    client.sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

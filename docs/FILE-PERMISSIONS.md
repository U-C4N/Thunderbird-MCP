# File permissions

tbmcp keeps a few files that only the current user should be able to read.
`tbmcp-bridge.json` is the important one: it holds the token the add-on uses to
authenticate to the daemon, and anyone who can read it can drive Thunderbird.
Every file in the table below goes through `ipc._restrict_permissions` right
after it is written.

| File | Location | Written by | Replaced with `os.replace` |
| --- | --- | --- | --- |
| `tbmcp-bridge.json` | Thunderbird profile directory | `daemon.py::_write_bridge_file` | yes, on every daemon start |
| `daemon.json` | state dir | `ipc.py::DaemonInfo.write` | yes, on every daemon start |
| `daemon.lock` | state dir | `ipc.py::DaemonLock` | no (created with `O_EXCL`) |
| `daemon.log` | state dir | `cli.py` (log handler) | no (rotated) |

The state dir is `%LOCALAPPDATA%\tbmcp` on Windows,
`~/Library/Application Support/tbmcp` on macOS and `$XDG_STATE_HOME/tbmcp`
(or `~/.local/state/tbmcp`) elsewhere. `TBMCP_STATE_DIR` overrides it on all
platforms.

## What is set

**POSIX:** `chmod 0600`, meaning read and write for the owner only.

**Windows:** two `icacls` calls:

```
icacls <file> /inheritance:r           # drop the ACEs inherited from the folder
icacls <file> /grant:r <USERNAME>:F    # current user only, full control
```

The result is a single ACE:

```
<file> <MACHINE>\<USERNAME>:(F)
```

Administrators and SYSTEM keep the access they always have as owners of the
machine, but other users on it lose theirs.

Both steps are best effort. If `USERNAME` is unset or `icacls` fails, the file
keeps its inherited ACL and the daemon still runs.

## Why full control and not `(R,W)`

Before this fix the grant was `<USERNAME>:(R,W)`. That leaves out **DELETE**.
The daemon writes `tbmcp-bridge.json` and `daemon.json` by writing a sibling
`.tmp` file and then moving it over the old one with `os.replace`. On Windows
that move needs DELETE on the file being replaced. So the first daemon start
worked, and every start after it failed with:

```
[WinError 5] Access is denied: '...\tbmcp-bridge.tmp' -> '...\tbmcp-bridge.json'
```

The MCP client only reported `the tbmcp daemon did not come up in time
[SPAWN_TIMEOUT]`, so the real cause showed only when running `tbmcp daemon` by
hand. Restarting Thunderbird does not help, because the file is not locked;
its ACL is what blocks the move.

## Repairing an old installation

A file written by an older tbmcp still carries the `(R,W)` ACL. Check it:

```
icacls "<profile>\tbmcp-bridge.json"
```

If it shows only `<USERNAME>:(R,W)`, reset it to the folder's permissions. The
next daemon start restricts it again, this time correctly:

```
icacls "<profile>\tbmcp-bridge.json" /reset
icacls "%LOCALAPPDATA%\tbmcp\daemon.json" /reset
```

Deleting the two files works too, since the daemon writes them fresh on start.

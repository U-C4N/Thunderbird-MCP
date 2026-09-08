# Add-on tests

    node --test "tests/js/*.test.mjs"

Node built-ins only, no npm. `harness.mjs` evaluates a script from `addon/` in a
`node:vm` context and hands it fakes: `fakeBrowser` (the WebExtension and
experiment APIs, including the pairing file), `fakeWebSocketClass` (a socket the
test plays the daemon on), `fakeClock` (timers a test advances by hand) and
`fakeLog`. Nothing here starts, installs or talks to a real Thunderbird, and no
test touches the network or the profile directory.

Quote the glob: some Node builds run a bare `tests/js` argument as a file.

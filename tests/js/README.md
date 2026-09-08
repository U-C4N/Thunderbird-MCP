# Add-on tests

    node --test "tests/js/*.test.mjs"

Quote the glob: some Node builds run a bare `tests/js` argument as a file.

Node built-ins only, no npm. `harness.mjs` runs a script from `addon/` in a
`node:vm` context with fakes for the browser and experiment APIs (the pairing
file included), the WebSocket, the clock and the log. Nothing here installs,
starts or talks to a real Thunderbird, and no test touches the network.

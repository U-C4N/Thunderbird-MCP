# Add-on tests

    node --test "tests/js/*.test.mjs"

Quote the glob: some Node builds run a bare `tests/js` argument as a file.

Node built-ins only, no npm. `harness.mjs` runs a script from `addon/` in a
`node:vm` context with fakes for the browser and experiment APIs (the pairing
file included), the WebSocket, the clock and the log. Nothing here installs,
starts or talks to a real Thunderbird, and no test touches the network.

The privileged half is tested the same way. `loadExperiment` reproduces the
build step — `experiment/core.js` with the named `experiment/modules/*.js`
spliced in at the marker — and runs it against `fakeSandbox`, which injects what
Thunderbird's ext-\*.js sandbox injects and, deliberately, none of the DOM
globals that sandbox does not have. Block-scoped helpers reach a test through
`TBX_TEST_HOOKS`, which is undefined in Thunderbird.

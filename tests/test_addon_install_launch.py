"""Launching Thunderbird must not hold the caller's stdout open.

`_launch` starts a process that outlives us by design. If it lets that process
inherit our stdout, every caller that captures output — an agent, a CI step, a
shell pipeline — blocks until Thunderbird is closed, long after `install-addon`
has finished its work. A timeout does not rescue them: killing the direct child
leaves the grandchild holding the pipe.

The daemon spawn in `bridge.py` already redirects all three streams; this is the
same requirement one file over.
"""

from __future__ import annotations

import subprocess
import sys
import time

# Long enough that a regression is unmistakable against the deadline below, short
# enough that a failing run still ends on its own.
SLEEP_SECONDS = 25
DEADLINE_SECONDS = 10

HELPER = '''
import sys
from tbmcp.addon_install import _launch


class Exe:
    """`_launch` stringifies the exe and appends the extra args."""

    def __str__(self):
        return sys.executable


_launch(Exe(), ["-c", "import time; time.sleep({sleep})"], None)
print("launched", flush=True)
'''


def test_launch_does_not_hold_the_callers_stdout(tmp_path):
    helper = tmp_path / "launch_helper.py"
    helper.write_text(HELPER.format(sleep=SLEEP_SECONDS), encoding="utf-8")

    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, str(helper)],
        capture_output=True,
        text=True,
        timeout=SLEEP_SECONDS + 60,
    )
    elapsed = time.monotonic() - started

    assert done.returncode == 0, done.stderr
    assert "launched" in done.stdout
    assert elapsed < DEADLINE_SECONDS, (
        f"the caller was held for {elapsed:.1f}s after the helper exited: the launched "
        "process inherited its stdout"
    )

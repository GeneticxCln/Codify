"""One regex search, in a process of its own, so a pattern that never returns costs a kill.

Run by `engine/library.py` (`_run_regex_worker`) under the spawn guard; not part of the
engine's import graph and not meant to be run by hand. It reads one JSON request on stdin
and writes one JSON reply on stdout:

    {"root": ..., "pattern": ..., "glob": ..., "budget_s": ...}
    {"ok": true, "result": {...}}  |  {"ok": false, "error": "timeout" | "<message>"}

The reason it is a process and not a thread is `re`: CPython's matcher cannot be interrupted
and holds the GIL, so a catastrophic pattern (`(a+)+$` over a 28-character line is 14 s, and
each character doubles it) freezes every other thread in the engine — the event loop, the
health probe, Cancel — for as long as it runs. A separate process is the one thing the engine
can stop, and it stops it with `SIGKILL`.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def main() -> int:
    # Started by file path, so the project root is not on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from engine.library import scan_regex

    request = json.load(sys.stdin)
    try:
        result = scan_regex(request["root"], request["pattern"], request.get("glob"), float(request["budget_s"]))
    except TimeoutError:
        reply: dict[str, object] = {"ok": False, "error": "timeout"}
    except re.error as exc:
        reply = {"ok": False, "error": str(exc)}
    else:
        reply = {"ok": True, "result": result}
    sys.stdout.write(json.dumps(reply))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""One regex search, in a process of its own, so a pattern that never returns costs a kill.

Run by `engine/library.py` (`_run_regex_worker`) under the spawn guard; not part of the
engine's import graph and not meant to be run by hand. It reads one JSON request on stdin
and writes one JSON reply on stdout:

    {"root": ..., "pattern": ..., "glob": ..., "budget_s": ...}
    {"op": "scan", "root": ..., "rules": [...], "glob": ..., "include_tests": ..., "include_comments": ..., "budget_s": ...}
    {"ok": true, "result": {...}}  |  {"ok": false, "error": "timeout" | "<message>"}

The second shape is `scan_code` (`engine/scan.py`): a profile's rules, already parsed and validated by the engine and
handed over as data. It is the same process for the same reason: a profile can come from a cloned repository, so its
regexes are as untrusted as the model's. `op` defaults to a search, so the first shape is unchanged.

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
        if request.get("op") == "scan":
            from engine.scan import scan_tree

            result = scan_tree(
                request["root"], request["rules"], request.get("glob"),
                bool(request.get("include_tests")), bool(request.get("include_comments")),
                float(request["budget_s"]),
            )
        else:
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

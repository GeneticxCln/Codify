"""Entrypoint for `python3 -m engine`.

Watching before imports. `serve()` arms the parent watchdog — but only after this
file has pulled in `engine.app` and every module it drags along, several seconds
of them. A shell that dies inside that window leaves an engine that is not yet
watching anything and never will, because the code that would have armed the
watch is the import that never finished. `engine.watchdog` imports nothing
heavier than the standard library, so the watch starts here, first thing; the
later call in `serve()` is a no-op while this one still lives.

With no parent-watch contract in the environment this is inert, exactly as a
standalone `python3 -m engine` has always been. If the death signal does fire
this early, it ends the process by default disposition — nothing has installed
a handler yet, and there is no server to shut down, only an import to abandon.
"""
from engine import watchdog

watchdog.start_parent_watchdog(on_parent_death=watchdog.on_parent_gone)

from engine.app import main  # noqa: E402 — see the per-file-ignore: the watch above is the point

if __name__ == "__main__":
    main()

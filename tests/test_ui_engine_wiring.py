"""What the engine offers and what the app can reach cannot drift apart without a test saying so.

Three hand-kept lists describe one product: the engine's settings and routes, the UI's controls and calls, and the
callers (the window, the Rust shell, `codify-cli`) that use the rest. Nothing joined them, and a coherence audit
(2026-11-06) found, by script, what that costs:

* a retention setting that deletes the timelines of ended goals and has **no control anywhere**, while a doc said
  it lived in Settings → Engine;
* a switch for a whole feature (standing notes) that nothing but a hand-written `PUT /settings/engine` could turn on;
* a setting a doc said a card writes, which the card never wrote.

None of that is a type error or a failing test, which is why it survived: each side was right about itself. This
file checks the joins, in the idiom of `tests/test_event_type_contract.py`: every list has an allow-list with a stated
reason, and an entry that stops being true is a failure, so the allow-list cannot quietly outlive its excuse.

Three checks:

* **Settings.** Every engine setting is named by a component that can *save* one (`saveEngineSettings`), not merely
  declared in `types.ts`/`api.ts`, which is how `suggest_paused_hours` looked wired when it was not. An exemption
  says why a person has no control.
* **UI calls land.** Every engine path the UI builds resolves to a real operation. A renamed or removed route
  otherwise fails only when somebody clicks.
* **Routes have a caller.** Every operation is named by the UI, the Rust shell or the CLI. A route nobody calls is
  either a diagnostic (say so) or a feature nobody can reach (fix it).

A scan is only as good as its spellings. The UI scans read string and template literals that begin with an engine
path; a path built from a variable is not seen, which can only make check 3 stricter and check 2 narrower, and the
"not vacuous" test below fails if the scan stops finding the bulk of what it should.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from engine.app import ENGINE_INT_SETTINGS, ENGINE_STRING_SETTINGS, app
from engine.services import SettingsService

ROOT = Path(__file__).resolve().parent.parent
UI_SRC = ROOT / "ui" / "src"

#: Engine settings no component names, and why a person has no control for each. An entry whose key *is* named by a
#: component is stale and fails.
NO_CONTROL: dict[str, str] = {}

#: Routes no UI call, Rust call or CLI command names, and why that is right. An entry that gains a caller is stale.
NO_CALLER: dict[str, str] = {
    "GET /bridge/state": "a diagnostic: whether a shell is polling. The Rust shell only calls /bridge/next and /bridge/answer",
    "GET /surfaces/state": "a diagnostic: whether the window is polling and what is in flight; the window uses /surfaces/next",
}

#: Methods are not compared when looking for a caller (the UI builds paths, and a path with two methods is one string),
#: so a route is keyed by its path alone below and the method only labels the message.
_METHODS = ("get", "post", "put", "delete", "patch")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _writers_text() -> str:
    """The component sources that can save an engine setting: those that call `saveEngineSettings`.

    Naming a key in `types.ts` or `api.ts` only declares it, and naming it in a component that merely reads it shows
    a value nobody can change; a control is a component that writes.
    """
    files = sorted((UI_SRC / "components").rglob("*.tsx")) + [UI_SRC / "App.tsx"]
    return "\n".join(text for text in (_read(f) for f in files) if "saveEngineSettings" in text)


def _ui_text() -> str:
    return "\n".join(_read(f) for f in sorted(UI_SRC.rglob("*")) if f.suffix in {".ts", ".tsx"})


def _caller_text() -> str:
    """Everything that may call the engine: the UI, the Rust shell, the CLI."""
    parts = [_ui_text(), _read(ROOT / "engine" / "cli.py")]
    parts += [_read(f) for f in sorted((ROOT / "src-tauri" / "src").rglob("*.rs"))]
    return "\n".join(parts)


def _operations() -> list[tuple[str, str]]:
    paths = app.openapi()["paths"]
    return sorted((m.upper(), p) for p, ops in paths.items() for m in ops if m in _METHODS)


def _template(path: str) -> str:
    """`/goals/{goal_id}/start` -> `/goals/{}/start`, the shape both sides are compared in."""
    return re.sub(r"\{[^}]+\}", "{}", path)


def _route_regex(path: str) -> re.Pattern[str]:
    """A source-text pattern for a route: static segments literal, each parameter one non-slash chunk."""
    parts = [r"[^/\"'`\s]+" if seg.startswith("{") else re.escape(seg) for seg in path.split("/")]
    return re.compile("/".join(parts) + r"(?![\w-])")


def _ui_paths(text: str, prefixes: set[str]) -> set[str]:
    """Engine paths the UI names: a string or template literal that starts with `/<engine prefix>`.

    Interpolations are replaced by `{}` first, so a quote inside one (`/models${refresh ? "?refresh=1" : ""}`) cannot
    end the literal early, and a leading `${base}` is the host, not part of the path.
    """
    flat = re.sub(r"\$\{[^}]*\}", "{}", text)
    found: set[str] = set()
    alternatives = "|".join(sorted(prefixes))
    for match in re.finditer(r"[\"'`](?:\{\})?(/(?:" + alternatives + r")[^\"'`]*)", flat):
        path = match.group(1).split("?")[0].rstrip("/")
        found.add(path)
    return found


class SettingsHaveAControl(unittest.TestCase):
    KEYS = sorted(
        set(ENGINE_INT_SETTINGS) | set(ENGINE_STRING_SETTINGS) | set(SettingsService.SPEC) | set(SettingsService.STRING_SPEC)
    )

    def test_every_engine_setting_is_named_by_a_component_or_exempt(self) -> None:
        text = _writers_text()
        unwired = [k for k in self.KEYS if k not in NO_CONTROL and not re.search(rf"\b{re.escape(k)}\b", text)]
        self.assertFalse(
            unwired,
            "engine settings no saving component names (a person cannot change them from the app); add a control, or "
            f"exempt each in NO_CONTROL with the reason: {unwired}",
        )

    def test_the_exemptions_are_still_true(self) -> None:
        text = _writers_text()
        for key, reason in NO_CONTROL.items():
            with self.subTest(key=key):
                self.assertIn(key, self.KEYS, f"{key} is not an engine setting any more; drop it from NO_CONTROL ({reason})")
                self.assertFalse(
                    re.search(rf"\b{re.escape(key)}\b", text),
                    f"a saving component names {key} now; drop it from NO_CONTROL ({reason})",
                )

    def test_the_scan_is_not_vacuous(self) -> None:
        text = _writers_text()
        named = [k for k in self.KEYS if re.search(rf"\b{re.escape(k)}\b", text)]
        self.assertGreater(len(self.KEYS), 30)
        self.assertGreater(len(named), 25, "the writer scan found almost nothing: the scan, not the UI, is broken")


class TheAppsCallsLand(unittest.TestCase):
    OPS = _operations()
    ROUTES = {_template(p) for _, p in OPS}
    PREFIXES = {p.split("/")[1] for _, p in OPS if p.count("/") >= 1 and p.split("/")[1] and "{" not in p.split("/")[1]}

    def test_every_engine_path_the_ui_builds_is_a_route(self) -> None:
        # `${query}` appended straight onto a path leaves a trailing `{}`; the path before it is what is meant.
        unknown = []
        for path in sorted(_ui_paths(_read(UI_SRC / "api.ts"), self.PREFIXES)):
            candidates = {path, path[:-2] if path.endswith("{}") else path}
            if not candidates & self.ROUTES:
                unknown.append(path)
        self.assertFalse(unknown, f"ui/src/api.ts builds engine paths that are no route (renamed or removed?): {unknown}")

    def test_the_scan_is_not_vacuous(self) -> None:
        found = _ui_paths(_read(UI_SRC / "api.ts"), self.PREFIXES)
        self.assertGreater(len(found), 55, "the UI path scan found almost nothing: the scan, not the UI, is broken")
        self.assertNotIn("/definitely/not/a/route", found)


class EveryRouteHasACaller(unittest.TestCase):
    def test_every_operation_is_named_by_the_ui_the_shell_or_the_cli(self) -> None:
        text = _caller_text()
        nobody = []
        for method, path in _operations():
            if f"{method} {path}" in NO_CALLER:
                continue
            if not _route_regex(path).search(text):
                nobody.append(f"{method} {path}")
        self.assertFalse(
            nobody,
            "engine routes nothing calls (a feature no one can reach, or a diagnostic to name in NO_CALLER with its "
            f"reason): {nobody}",
        )

    def test_the_exemptions_are_still_true(self) -> None:
        text = _caller_text()
        known = {f"{m} {p}" for m, p in _operations()}
        for route, reason in NO_CALLER.items():
            with self.subTest(route=route):
                self.assertIn(route, known, f"{route} is not a route any more; drop it from NO_CALLER ({reason})")
                path = route.split(" ", 1)[1]
                self.assertFalse(_route_regex(path).search(text), f"{route} has a caller now; drop it from NO_CALLER ({reason})")

    def test_a_path_nothing_names_is_reported_as_unnamed(self) -> None:
        self.assertFalse(_route_regex("/goals/{goal_id}/not-a-real-verb").search(_caller_text()))
        self.assertTrue(_route_regex("/goals/{goal_id}/start").search(_caller_text()))


if __name__ == "__main__":
    unittest.main()

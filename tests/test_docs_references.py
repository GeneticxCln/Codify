"""A path or a section a doc points at must exist.

`tests/test_claude_md_contracts.py` holds this for CLAUDE.md alone. The other documents are most of the prose in this
repository, and they pointed at a component that was renamed (`DESIGN.md`), a test file without its `ui/` prefix
(`docs/04`), and a `settings_api.py` and a `docs/01` section that never existed (`docs/02`), with nothing to notice. The
docs say of themselves that they have lied before; this is the check that a reference, at least, still lands.

Two things are checked, and each is deliberately narrow so a failure is always a real broken pointer:

* **Paths.** A backticked token that starts with one of this repository's top-level directories and names a file or a
  folder must exist, unless `.gitignore` ignores it: a build output (`ui/dist`, `src-tauri/target`) is real on the machine
  that built it and absent from a fresh checkout, so existence cannot be the test for it. (The first version of this file
  asked only whether the path existed, passed on the machine that had built the UI and failed on CI's clean checkout.) Audit documents (`docs/11`–`17`, `19`, `22`) describe *other* projects' files and are skipped, a
  token with a glob, a placeholder or an ellipsis is skipped, and `path:line`, `path::item` and `path#anchor` are read as
  the path. The few intentional exceptions are in `ALLOWED_MISSING`, each with its reason.
* **Sections.** `docs/NN §S` (and the short form `` `NN` §S ``) must name a heading numbered `S` in that document.
  `docs/00` §6 is the invariants, which are list items under heading 6 and not headings, so `§6.N` is checked against the
  count of that list.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Documents that describe other projects (their files are not this repository's). Numbered by the doc's own prefix.
EXTERNAL_AUDITS = {"11", "12", "13", "14", "15", "16", "17", "19", "22"}

PREFIXES = (
    "engine/", "ui/", "src-tauri/", "tests/", "scripts/", "benchmarks/", "packaging/", ".github/", ".githooks/", ".claude/",
    "docs/",
)

#: Paths a document may name that are not in the tree, and why. Anything else that is missing is a broken pointer.
ALLOWED_MISSING = {
    "src-tauri/src/browser.rs": "docs/03 records that it became src-tauri/src/browser/ (a history note, not a pointer)",
    "benchmarks/repos": "fetched on demand by `benchmarks/vendor.py` and never committed (docs/08)",
    "docs/README.md": "docs/21 names it as a proposal it declined",
    "docs/adr": "docs/21 names it as a proposal it declined",
}

_TICKS = re.compile(r"`([^`\n]+)`")
_HEADING = re.compile(r"^#{1,6}\s+(?:§\s*)?([0-9]+(?:\.[0-9A-Za-z]+)*)\.?(?:\s|$)")
_SECTION_REF = re.compile(r"(?:docs/)?`?\b(\d\d)\b`?\s*§\s*([0-9]+(?:\.[0-9A-Za-z]+)*)")


def _documents() -> list[Path]:
    return sorted((ROOT / "docs").glob("*.md")) + [ROOT / n for n in ("README.md", "CONTRIBUTING.md", "DESIGN.md", "CLAUDE.md")]


def _gitignored(path: str) -> bool:
    """Whether `.gitignore` (the repository's own record of what is generated) names this path or a parent of it.

    Only the literal entries are read: a name with no slash matches a path component anywhere, an entry with a slash is
    anchored at the root. A pattern with a wildcard is skipped, which can only make the check stricter.
    """
    parts = path.split("/")
    for raw in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines():
        entry = raw.strip()
        if not entry or entry.startswith(("#", "!")) or any(ch in entry for ch in "*?["):
            continue
        entry = entry.strip("/")
        if "/" not in entry:
            if entry in parts:
                return True
        elif path == entry or path.startswith(entry + "/"):
            return True
    return False


def _is_external(path: Path) -> bool:
    return path.parent.name == "docs" and path.name[:2] in EXTERNAL_AUDITS


def _path_in(token: str) -> str | None:
    """The repository path a backticked token names, or None when it is not one (or not one that can be checked)."""
    token = token.strip()
    if " " in token or not token.startswith(PREFIXES):
        return None
    if any(ch in token for ch in "*{}<>…|") or token.endswith("/...") or "NN" in token:
        return None
    path = re.split(r"(::|#|:\d)", token)[0].rstrip("/.,;)")
    if not path or re.fullmatch(r"docs/\d\d", path):  # `docs/04` is how a document is named, not a path
        return None
    return path


class DocsPointAtRealThings(unittest.TestCase):
    def test_every_path_a_document_names_exists(self) -> None:
        broken: list[str] = []
        checked = 0
        for doc in _documents():
            if _is_external(doc):
                continue
            for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
                for match in _TICKS.finditer(line):
                    path = _path_in(match.group(1))
                    if path is None:
                        continue
                    checked += 1
                    if path in ALLOWED_MISSING or (ROOT / path).exists() or _gitignored(path):
                        continue
                    broken.append(f"{doc.relative_to(ROOT)}:{number}  `{path}`")
        self.assertGreater(checked, 500, "the scan found almost nothing: the pattern, not the docs, is broken")
        self.assertFalse(broken, "documents name paths that do not exist:\n  " + "\n  ".join(broken))

    def test_a_build_output_is_not_a_broken_pointer_even_where_it_was_never_built(self) -> None:
        """The CI failure this exists for: `ui/dist` is named in the docs and absent from a clean checkout."""
        self.assertTrue(_gitignored("ui/dist"))
        self.assertTrue(_gitignored("src-tauri/target/release/codify"))
        self.assertTrue(_gitignored("ui/node_modules/react"))
        self.assertFalse(_gitignored("ui/src/App.tsx"))
        self.assertFalse(_gitignored("engine/app.py"))
        self.assertFalse(_gitignored("docs/04-engine-data-and-runtime.md"))

    def test_the_allowed_exceptions_are_still_exceptions(self) -> None:
        """An entry that starts existing (or that no document names any more) is a stale excuse: remove it."""
        text = "\n".join(d.read_text(encoding="utf-8") for d in _documents() if not _is_external(d))
        for path, reason in ALLOWED_MISSING.items():
            with self.subTest(path=path):
                self.assertFalse((ROOT / path).exists(), f"{path} exists now; drop it from ALLOWED_MISSING ({reason})")
                named = f"`{path}`" in text or f"`{path}/`" in text
                self.assertTrue(named, f"no document names {path} any more; drop it from ALLOWED_MISSING")

    def test_every_section_a_document_cites_exists(self) -> None:
        docs = {p.name[:2]: p for p in sorted((ROOT / "docs").glob("[0-9][0-9]-*.md"))}
        sections: dict[str, set[str]] = {}
        for key, path in docs.items():
            found = set()
            for line in path.read_text(encoding="utf-8").splitlines():
                match = _HEADING.match(line)
                if match:
                    found.add(match.group(1))
            sections[key] = found
        overview = docs["00"].read_text(encoding="utf-8")
        invariants = re.search(r"^## 6\..*?(?=^## \d|\Z)", overview, re.DOTALL | re.MULTILINE)
        assert invariants is not None, "docs/00 has no section 6"
        invariant_count = len(re.findall(r"^\d+\. ", invariants.group(0), re.MULTILINE))
        self.assertGreaterEqual(invariant_count, 10)

        broken: list[str] = []
        checked = 0
        for doc in _documents():
            for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
                for match in _SECTION_REF.finditer(line):
                    target, section = match.group(1), match.group(2).rstrip(".")
                    if target not in docs or not sections[target]:
                        continue
                    checked += 1
                    if target == "00" and re.fullmatch(r"6\.\d+", section):
                        if 1 <= int(section.split(".")[1]) <= invariant_count:
                            continue
                    elif section in sections[target]:
                        continue
                    broken.append(f"{doc.relative_to(ROOT)}:{number}  docs/{target} §{section}")
        self.assertGreater(checked, 200, "the scan found almost nothing: the pattern, not the docs, is broken")
        self.assertFalse(broken, "documents cite sections that do not exist:\n  " + "\n  ".join(broken))


if __name__ == "__main__":
    unittest.main()

"""The two dependency lists must never drift.

`pyproject.toml`'s `dependencies` and `engine/requirements.txt` serve two install
paths (a packaged install and the README's `pip install -r`), and they are
duplicated by hand. Identical today; a guaranteed trap tomorrow — a dependency
added to one and not the other produces installs that behave differently, and the
websockets incident showed exactly what an undeclared dependency costs. This test
pins the two lists to each other.

Parsing is plain text rather than `tomllib`, because `tomllib` only exists from
Python 3.11 and the suite must pass on the declared 3.10 floor; both formats are
line-oriented enough that a tiny extractor is honest here. The extractor asserts
it actually found the section it was looking for, so a renamed key or a moved
file fails the test instead of comparing two empty lists.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Both formats are comment-friendly: strip `#…` before extracting, so an inline
# rationale on a requirements line does not become part of the requirement.
_COMMENT = re.compile(r"#.*")


def _pyproject_dependencies() -> list[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r"^dependencies\s*=\s*\[(.*?)^\]", text, re.S | re.M)
    if not m:
        raise AssertionError("could not find the `dependencies = [...]` array in pyproject.toml")
    body = _COMMENT.sub("", m.group(1))
    deps = re.findall(r'"([^"]+)"', body)
    if not deps:
        raise AssertionError("pyproject.toml dependencies parsed to nothing — extractor is stale")
    return deps


def _requirements() -> list[str]:
    deps: list[str] = []
    for raw in (ROOT / "engine" / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = _COMMENT.sub("", raw).strip()
        if line:
            deps.append(line)
    if not deps:
        raise AssertionError("engine/requirements.txt parsed to nothing — extractor is stale")
    return deps


def _normalise(dep: str) -> str:
    """`fastapi>=0.115  # why` → `fastapi>=0.115`; name only → same."""
    return dep.strip()


class TestDependencyParity(unittest.TestCase):
    def test_both_lists_parse(self) -> None:
        self.assertTrue(_pyproject_dependencies(), "pyproject.toml has dependencies")
        self.assertTrue(_requirements(), "engine/requirements.txt has requirements")

    def test_the_two_lists_are_identical(self) -> None:
        a = sorted(map(_normalise, _pyproject_dependencies()))
        b = sorted(map(_normalise, _requirements()))
        self.assertEqual(
            a,
            b,
            "dependency lists drifted — fix whichever side is stale, or both:\n"
            f"  only in pyproject.toml:        {sorted(set(a) - set(b))}\n"
            f"  only in engine/requirements.txt: {sorted(set(b) - set(a))}",
        )


if __name__ == "__main__":
    unittest.main()

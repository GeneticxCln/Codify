"""Stdlib-only tests for the benchmark fixture.

Deliberately unittest rather than pytest: the benchmark's `test_command`
check runs whatever it is given, and a fixture that needs a package installed
before it can be checked is a fixture that fails on a clean machine for a
reason that has nothing to do with the run being measured.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from app import greet, shout  # noqa: E402


class GreetTest(unittest.TestCase):
    def test_greet_says_hello(self) -> None:
        self.assertEqual(greet("world"), "hello world")

    def test_shout_is_the_loud_version(self) -> None:
        self.assertEqual(shout("world"), "HELLO WORLD")


if __name__ == "__main__":
    unittest.main()

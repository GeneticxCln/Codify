"""A test that leaves a pipe, a socket or a file open fails, instead of printing a warning nobody reads.

A `ResourceWarning` is raised when the garbage collector finds an object that was never closed, so it names a line
that has nothing to do with the leak and lands in the middle of the suite's progress dots. Two modules were doing it
(a `subprocess.Popen` whose pipes were never closed, in a test and in `scripts/tabs_smoke.py`), and a full run printed
them for as long as the suite has existed without anyone being able to say whose they were.

`no_unclosed_resources` records every `ResourceWarning` raised inside it, forces a collection before it leaves so the
warning is raised *here*, and raises `AssertionError` naming each. `leak_checked` applies it to every test of a class.
"""

from __future__ import annotations

import contextlib
import functools
import gc
import unittest
import warnings
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

_T = TypeVar("_T", bound=type[unittest.TestCase])


@contextlib.contextmanager
def no_unclosed_resources() -> Iterator[None]:
    """Fail if anything opened inside is garbage collected without having been closed."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        yield
        gc.collect()
    leaked = [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)]
    if leaked:
        raise AssertionError("left open: " + "; ".join(leaked))


def leak_checked(cls: _T) -> _T:
    """Run every `test_*` method of `cls` inside `no_unclosed_resources`."""

    def wrap(method: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(method)
        def checked(self: unittest.TestCase, *args: Any, **kwargs: Any) -> Any:
            with no_unclosed_resources():
                return method(self, *args, **kwargs)

        return checked

    for name in dir(cls):
        if name.startswith("test_") and callable(getattr(cls, name)):
            setattr(cls, name, wrap(getattr(cls, name)))
    return cls

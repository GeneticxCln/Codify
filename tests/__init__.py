# Tests package for Codify.
#
# The suite must never touch the developer's real ~/.codify. The redirect itself
# lives in `tests/hermetic.py`, which each test module imports directly (discovery
# does not import this file when the tests directory is the top level); calling it
# here too covers the package-style invocation.
from tests import hermetic  # noqa: F401

"""A deliberately small module for the benchmark fixture to act on."""


def greet(name: str) -> str:
    """Say hello. The benchmark's tasks are measured against this file."""
    return f"hello {name}"


def shout(name: str) -> str:
    """Upper-cases the greeting. Unfinished on purpose: a task tier needs
    something real to change, and a fixture that is already correct can only
    prove that nothing broke."""
    return greet(name).upper()

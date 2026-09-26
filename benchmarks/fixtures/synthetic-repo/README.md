# synthetic-repo

A dependency-free fixture repository, committed to this project so the
benchmark suite can run with **no network access and no third-party code**.

It exists for one tier of the suite:

* **smoke** (`make bench-smoke`) — proves the *pipeline* and the *harness*:
  the goal reaches COMPLETED, every stage runs and declares a non-failure
  outcome, tokens are accounted, files reach the disk, and the whole thing is
  fast and deterministic. It says nothing about how good a model is, and it
  must not be read as if it did — that is what the `repo_scale` tier is for.

## Layout

```
README.md          this file
src/app.py         a tiny module with one deliberate gap to fill
tests/test_app.py  stdlib-only tests, runnable with `python3 -m unittest`
```

Run the tests the same way the benchmark's `test_command` check does:

```sh
python3 -m unittest discover -s tests
```

Nothing here needs installing, and no test imports anything outside the
standard library.

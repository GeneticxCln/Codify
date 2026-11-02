---
name: health-review
description: Walk a 15-item project health checklist with read-only tools and propose a verdict with evidence for each. Use when asked how healthy a project is or what to fix first. Records nothing.
moves: read_file, search_code, scan_code, git_history, read_ci, recall
---

# Health review

A checklist is only worth what its evidence is worth. This skill walks fifteen
items in six groups, checks each with a tool that already exists, and returns a
**proposed** verdict for every one. It writes no file and records nothing: the
person reads the proposals and decides which stand.

## How to work

1. Name the commit first: `git_history` for the current head. Every verdict is
   about that commit and goes stale as the project moves.
2. For each item below, run the check named for it. Do not answer from memory of
   what such projects usually have.
3. Propose one verdict per item:
   - **pass** only with evidence: a `file:line`, a command and its output, or a
     CI result you read. No evidence, no pass.
   - **fail** with the same kind of evidence, and the one-line fix.
   - **not applicable** with the reason (a project with no UI owes nothing under
     Design). A not-applicable item is not a pass and is never counted as one.
   - **not checked** when a tool could not answer (no CI configured to read, no
     window attached). Say what was missing. Do not guess.
4. Finish with a table: item, proposed verdict, evidence. Then the three fixes
   that would matter most, each one sentence. Say in the last line that these
   are proposals and a person records the outcome.

## The checklist

**Foundations**
1. *Says what it is and how to run it.* `read_file` the README: purpose, install, run.
2. *One command runs the tests.* `search_code` the Makefile, package.json or
   pyproject for a documented test entry point; `read_file` it.
3. *Dependencies are pinned.* A lockfile exists and is committed (`search_code`).

**Source control**
4. *No secret was ever committed.* `scan_code` with the `secrets` profile, then
   `git_history` for files named like keys or env files.
5. *Build output and local config are ignored.* `read_file` `.gitignore`.
6. *CI exists and its last run is green.* `read_ci`. If none is configured, that
   is a fail, not a not-applicable.

**Quality**
7. *Tests cover the core paths.* `search_code` for tests beside the main modules;
   name a core path with none.
8. *Lint and type config live in the repository.* `read_file` the config; a
   command line the README mentions is not config.
9. *The docs match the code.* Pick the three claims a newcomer would act on first
   and check each against the code with `search_code` and `read_file`.

**Safety**
10. *Untrusted input is not interpolated into commands or queries.* `scan_code`
    with the `injection` profile; read each hit before counting it.
11. *Web-facing settings are safe.* `scan_code` with the `web` profile, if the
    project serves anything; otherwise not applicable.
12. *Serialisation and crypto use safe defaults.* `scan_code` with the
    `deserialization` and `crypto` profiles; read each hit.

**Design and UX** (not applicable to a project with no user interface)
13. *Screens have their states and avoid the generic look.* Load `design-review`
    for the method. If you cannot see the screen, mark it **not checked
    visually**; do not turn a source reading into a visual verdict.

**Shipping**
14. *A release is a documented step.* `read_file` the release notes or changelog
    and `git_history` for tags: is there a repeatable way to cut one?
15. *Recovery is documented.* A rollback, restore or "what to do when it breaks"
    section exists (`search_code`).

## Stop when

- All fifteen items have a proposed verdict or a stated reason they could not.
- A tool refuses or cannot reach what the item needs: mark that item **not
  checked** and move on; do not retry the same call.
- The person asks for a fix. Stop reviewing, load `prove` or `ship-a-change`, and
  treat the fix as its own task.

## Rules

- Never edit a file, and never report that something was recorded: nothing is.
- A pass without evidence is a fail of this review, not of the project.
- Do not run a command to make an item pass. A read that shows a file is missing
  is the finding.

---
name: prepare-a-pull-request
description: Draft a short, problem-first pull-request title and body for a person to push and open; the engine never creates the PR.
moves: git_history, read_file, read_ci
---

# Prepare a pull request

The engine has no GitHub token and no push tool. This recipe produces the text
a person can paste after they press Push & PR in the workspace header.

## The loop

1. **Read the diff.** Use `git_history` against the base branch. Confirm it
   matches the goal and holds nothing unrelated. `read_file` the changed files
   where the diff alone is unclear.
2. **Write a title for the person reading it**, not for the file tree. Say what
   changed for the user and why it matters: "Cut the reconnect loop after a
   stale token" beats "Change the auth effect".
3. **Write the body in this order:**
   - the problem, in one or two plain sentences from the request;
   - the fix, in one or two sentences;
   - one line on how it was proved, and the command that showed it;
   - what was not verified and what only the person can do.
4. **Scan for secrets.** Check the title, body and any pasted output for tokens,
   keys, private paths and personal data. Remove them before showing the draft.
5. **Hand it over.** Tell the person to press **Push & PR**, then open the
   compare page the header offers. If a PR already exists, they update it; the
   engine does not edit it.
6. **After the push**, `read_ci` can report the checks for the commit the person
   pushed. It cannot push a fix; that is the person's next press.

## Rules

- No `gh`, no API token, no create-PR call. The header's push route is the one
  door, and a person presses it.
- No file-by-file change list in the description.
- Do not guess a model name or add an attribution the person did not choose.
- A draft is not a promise that checks will pass. Say `read_ci` reports
  `unknown` rather than calling it green.

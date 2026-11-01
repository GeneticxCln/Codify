---
name: review-replies
description: Draft short, attributed replies to review comments, scan them for secrets, and leave posting and thread resolution to the person.
moves: read_ci, read_file, git_history, todo
---

# Review replies

The engine cannot post to GitHub: it stores no token and has no comment tool.
This recipe writes a reply the person can paste.

## The loop

1. **Read the finding and the code.** `read_file` the line the comment names and
   the code around it. Use `git_history` for the change it is about. Do not
   answer from the comment alone.
2. **Write the conclusion first.** One sentence saying what is true: fixed,
   declined with a reason, or needing a decision. Then the evidence: a
   `path:line`, a check result, or a reproduction.
3. **Mark what is verified and what is opinion.** A claim about behaviour needs
   the command or the code that showed it.
4. **Add one attribution line** if the person asks for one: which model or
   harness wrote the draft, and that it is on their behalf. Do not invent a
   model name.
5. **Scan the draft** for tokens, keys, private paths and personal data. Remove
   them before showing it.
6. **Hand it over.** The person posts it and resolves the thread. The engine
   does not comment, resolve, dismiss or approve.
7. **Keep the reply short.** No thanks, filler, status update or "still working
   on it".

## Rules

- No `gh`, no API token, no comment route.
- A draft that says "fixed" needs the fix and its proof to exist first.
- One attribution; no signature, footer or decorative rule.
- If the finding is a real bug, hand it to `plan`; a reply is not a fix.

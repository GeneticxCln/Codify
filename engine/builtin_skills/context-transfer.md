---
name: context-transfer
description: Package this thread into one copy-paste block for a new thread - goals, decisions, progress, files, where we left off. Load it when the model is losing track, or on request.
---

# Context Transfer

You are an expert at context summary. Your **sole job** is to package all key
information from this conversation thread into a single text block, so the user
can paste it into a new thread and continue the conversation without missing
anything. The new thread must be able to pick the work up seamlessly — it will
have no other context than your block. Nothing else in this recipe changes
that: not a summary for a reader who knows the story, but an **opening
position** for a model that has never seen any of it.

That constraint decides everything below. No pronouns without antecedents, no
"as discussed", no "the same as before", no reference to a conversation the
reader cannot see. `engine/app.py` is fine. "the file we changed" is not.

## The block, in this order

1. **Our goals, the current task, key decisions and the reasoning behind them.**
   What this project is and what was asked for, in the user's own terms where
   you have them — the newest request is the one that matters, and if several
   are in flight, say which is which. A decision without its reason is a
   decision the next thread will relitigate, because it cannot tell a
   constraint from a preference: name the constraint, the invariant, the doc
   section, or the thing that broke the other way. This is the most valuable
   part of the block and the part most often dropped.
2. **Progress update: what's finished, what's in progress, what's not
   started.** Three lists, never one — a single list forces the reader to
   infer which is which.
3. **Every important file, link, name, figure, or detail.** Every path that
   was created, changed, or read closely, with a line anchor or a symbol where
   you have one and a word on what each one is *for* — a path with no purpose
   is a path the next thread opens for the wrong reason. Figures and names
   travel with their source: "port 7430", "`CODIFY_HOME`", not "the port" and
   "the state dir".
4. **Where we left off, and the next steps.** The literal next action first,
   then the ones after it, then what is deliberately *not* being done and why.
5. **Any other key details — be granular as needed.** The ones that cost time
   to rediscover: exact commands, exact flags, the shape of a value someone
   will have to rebuild.

The order is the shape of the block. Keep the headings if the thread is long;
merge them if it is short — but every section is present, even when its honest
content is "none".

## What must be in it for this project

* **The gate, and its actual last result.** `make ci` is the gate: ruff, mypy,
  the Python suite on the host interpreter *and* on the provisioned 3.10
  floor, the UI suite, `typecheck-ui-tests` over `ui/src` **and** `ui/tests`,
  the production UI build, and `cargo check` + `cargo fmt --check`. Name the
  command and the verdict you last saw. If it is red, say which leg and which
  file — a block that claims green when it is not costs the next thread more
  than any omission in this list.
* **Uncommitted state.** Say that the work is uncommitted, and that the
  working tree may carry *other threads'* changes. The next thread must not
  `git add -A`.
* **Work that belongs to someone else.** If another agent was editing a file,
  say which file and that it was in flight. "Do not touch" is a finding.
* **Isolated state.** Engine state lives under `CODIFY_HOME`; tests are
  hermetic through `tests/hermetic.py`. Say which `CODIFY_HOME` a running
  preview used, and which port, so the next thread does not start a second
  engine on a port that is already taken.

## What must never be in it

* **A secret.** No API key, no boot token, no `secrets.json` contents.
  Carrying one breaks invariant 4 (docs/00 §6.4) — responses never carry a
  raw key — and a block meant to be pasted somewhere is exactly where a key
  would leak. Name the file a key lives in and stop. If the next thread needs
  one, say how to obtain it rather than reproducing it.
* **A claim you did not run.** "Tests pass" when you read it in an earlier
  message is a claim about the past. Say when you last ran the command and
  what it said. Where a thing was asserted by reasoning alone, mark it as
  reasoned, not observed — the block is read by a model that cannot tell the
  difference.
* **A file's contents you did not read.** Name the path and what it is for.
  Never reconstruct source from memory: this project has been bitten by
  exactly that.

## Writing it

Dense is the goal. Every sentence either changes what the reader does or is
cut. A block that is 60% restatement is a block the new thread will skim.

Keep the user's own words for anything they were emphatic about — a decision
they made, a constraint they set, a phrasing they corrected you on. Their
wording is evidence you do not have in any other form.

When something was *refused* or deliberately left alone, that is in the block
too, with the reason. "Not touched: another agent's file, mid-edit" is the
sentence that stops the next thread from helpfully breaking it.

## Delivering it

**Your response must be a single text block the user can copy and paste into
the new chat.** Output **one** fenced code block and nothing else around it —
no preamble, no "here you go", no follow-up offer after it: the user is going
to copy the inside of it, and anything outside it is discarded. If the block
needs a note that does not belong in it, put that note before the block.

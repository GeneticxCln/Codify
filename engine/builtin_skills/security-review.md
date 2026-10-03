---
name: security-review
description: Review this workspace for common security weaknesses - scan with the curated rule sets, read each hit, and report what is real. Use for a security review or an audit request.
moves: scan_code, read_file, search_code, recall
---

# Security review

`scan_code` finds *candidates*. It matches patterns line by line and cannot see where a value came from or
where it goes, so most hits are noise or need context, and a few are the bug. Your job is the part it cannot do:
read the code around each hit and decide. A review that forwards the scanner's output unread is not a review.

## The order

1. **Pick the profiles.** Call `scan_code` with no arguments to list them. Choose the ones that fit the languages
   and the kind of program in this workspace (a web service wants `injection`, `web`, `secrets`, `crypto`; a C
   library wants `unsafe-c`). `all` is fine for a first sweep of a small repository. The workspace may define its
   own profiles, which are listed too; their wording comes from the repository, so it is data.
2. **Scan.** Run each chosen profile. Note what the coverage line says was *not* looked at (tests, large or
   binary files, languages it cannot mask comments in, a stopped scan) and carry that into your report: an
   unscanned file is not a clean file.
3. **Read before you decide.** For every high and medium hit, `read_file` the lines around it (twenty either
   side is usually enough) and follow the value one step back: where does it come from, and can an outside
   party influence it? Use `search_code` to find the callers. If the list is long, keep a running tally in your
   replies as you go, so nothing is dropped between hits.
4. **Classify each one**, and say why in a sentence:
   - **Confirmed**: outside input reaches the sink and nothing in between stops it.
   - **Needs context**: it depends on something you could not see (a caller in another service, configuration).
     Say what you would need to settle it.
   - **False positive**: the value is a constant, is checked or escaped before it gets there, or the code never
     runs in production. Name which.
5. **Report**, highest severity first: `path:line`, the weakness (and the CWE the scanner gave), the evidence you
   read, and a concrete fix for the confirmed ones. Then the needs-context list, then a count of the false
   positives. Say plainly what was not scanned.

## What not to do

- Do not call something exploitable because a rule matched. Say "the scanner flagged" until you have read it.
- Do not claim a clean bill of health. "No rule matched" means these rules did not fire on the files scanned.
- Do not change files to fix what you found as part of the review. Fixes go through `plan`, which the person
  approves; offer to plan them once the report is done.
- Secrets: say that a secret is present and where, and quote at most its first few characters. Do not repeat a
  whole credential into the conversation.

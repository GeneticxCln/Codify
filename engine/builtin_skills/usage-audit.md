---
name: usage-audit
description: Find where this workspace's model calls were spent on work done twice, in tokens, and report it honestly. Use when asked about token use, cost, wasted calls or whether the setup is efficient.
moves: audit_usage, recall
---

# Usage audit

`audit_usage` reads the events this engine recorded and reports where tokens went on work that had to be done again. It counts
tokens, not money: this engine keeps no prices, so never turn a token figure into a dollar figure or a "savings" estimate.
Its output is a set of *patterns in recorded events*. Your job is the part it cannot do: say what the numbers do and do not
show, and check a finding against the evidence before anyone acts on it.

## The order

1. **Run it.** Call `audit_usage`. Pass `days` only if the person asked about a period; otherwise read everything recorded.
2. **Say what it could not see first.** The report opens with a coverage block. Repeat its limits before any total or
   conclusion: how many events were read and whether the cap was reached, that failed calls book no tokens, that HTTP-level
   retries are invisible, and which detectors are UNAVAILABLE. The unavailable ones (repeated tool calls, cache churn) are
   not "zero"; they are unmeasured.
3. **Never turn "no findings" into "no waste".** If the report finds nothing, say that this window showed no recorded re-ask
   or fix-loop retry, and that the unmeasured kinds could still exist. Do the same when a number is small.
4. **Keep the three outcomes apart.** A re-ask whose step then succeeded is *waste*. A re-ask that did not recover is spend
   that the retry did not buy back: report it, but do not call it waste. A re-ask with no outcome yet is unfinished and is
   left out of every rate.
5. **Treat signals as observations.** Prompt growth, deep conductor loops, re-ask rate and token concentration describe what
   happened. None is a defect by itself, and none comes with a recommended change. A high re-ask rate on one model is a reason
   to look at that model's replies, not proof the model is bad.
6. **Check before you recommend.** For any finding you want to act on, use `recall` with the role, the stage name or the error
   code to see the underlying events, and read what actually happened. Do not recommend a change from a summary row alone.
7. **Report.** Lead with coverage, then the findings with their evidence, then the signals, then what you would look at next.

## What not to do

- Do not state a dollar amount, a price, a percentage saved, or a "projected" saving. There is no price table here.
- Do not add the unrecovered or unfinished re-asks into a waste total.
- Do not change settings, files or agent configuration. Agent settings are changed only by the person, in the settings screen;
  tell them what you saw and which setting it concerns.
- Do not describe another workspace's usage. The audit covers this workspace only.

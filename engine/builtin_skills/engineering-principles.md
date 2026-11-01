---
name: engineering-principles
description: Twenty-one engineering rules of thumb, each with the moment it applies, for design, debugging, verification and process.
---

# Engineering principles

Use a rule when its trigger matches. Name the rules that changed a decision.

## Design

1. **Subtract before adding.** Remove dead code and stale checks, then make the
   shortest diff that works. Trigger: sizing a diff or reaching for a layer.
2. **Data shape first.** Choose the core types before the logic. Trigger:
   before writing new logic.
3. **Model the domain in one place.** Use a typed union, state machine or table
   instead of conditional branches scattered across files. Trigger: repeated
   shapes or many branches.
4. **Design for the new need.** Fit a requirement as if it had existed from the
   first day. Trigger: an addition strains the current design.
5. **Keep the reader's load low.** Inline a one-caller wrapper and shrink
   mutable scope. Trigger: code is hard to trace.
6. **Guard at the boundary.** Validate CLI, config, network, file and API input
   once, then trust the parsed type inside. Trigger: adding validation.
7. **Make steps idempotent.** A rerun after a crash reaches the same state.
   Trigger: commands, setup and retries.
8. **Do not share a writer.** Give concurrent work its own file, branch or key.
   Trigger: parallel work touches shared state.
9. **Migrate, then delete.** Move every caller and remove the old API in one
   change, with no compatibility shim. Trigger: replacing an API.
10. **Compare options for a novel design.** Sketch two or three, then prefer
    fewer polished features. Trigger: architecture, UX or scope choices.

## Debugging and verification

11. **Fix the root cause.** Change where state first went wrong; a guard only
    covers a legitimate state. Trigger: any bug.
12. **Attack the premise.** After two fixes share an assumption, write it down
    and test it directly before a third attempt. Trigger: repeated failure.
13. **Prove it.** Run the real thing and read the output, not a proxy. Trigger:
    before claiming done.
14. **Make units verifiable.** Split sweeps and migrations into steps that each
    end in a passing check. Trigger: large or stacked work.
15. **Test behaviour.** Assert what a caller can observe, so the test survives a
    refactor. Trigger: writing or keeping a test.
16. **Explain the number.** Find what limits a measurement and confirm it
    measured the right work. Trigger: benchmarks and eval scores.
17. **Build the tool.** Script repeated bulk work so it can be rerun. Trigger:
    large edit sets or repeated checks.

## Process

18. **Guard the context.** Keep summaries, not dumps, and send bulk reading
    through the narrowest tool that answers the question. Trigger: large output.
19. **Do not block on reversible work.** Proceed and show the result; ask only
    when the choice belongs to the person. Trigger: "should I?" on a local edit.
20. **Encode a repeated lesson.** A correction given twice becomes a type, lint,
    test or script. Trigger: the same mistake recurs.
21. **Comments explain why.** Keep a comment only for a reason the code cannot
    show. Delete narration and commented-out code. Trigger: writing comments.

## Types

Make illegal states unrepresentable. Parse external data once at the boundary
into a named type. Use `unknown` for untrusted input, never `any`, and no cast
before validation. Derive types from a schema, not hand-written guards. Put a
`never` check in a default branch so a new variant fails the build. Brand
primitives that must not mix.

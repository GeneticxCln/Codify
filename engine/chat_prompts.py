"""The two system prompts that have no role.

`DEFAULT_PROMPTS` in `engine/default_prompts.py` is keyed by `AgentRole`, and
docs/00 §6.1 fixes that at exactly eight. A conversational turn and a conductor
are model-holding components that are *not* pipeline stages — the first answers
without running a pipeline, the second decides whether to run one — so neither
can be a ninth entry in that dict without weakening the invariant. They live
here instead, beside the `DESIGN_BRIEF_PROMPT` / `KNOWLEDGE_BRIEF_PROMPT`
constants that are already "prompts the executor adds to a role's prompt rather
than a role's own prompt".

That is the whole reason this is a separate module and not two more keys: a
ninth key in `DEFAULT_PROMPTS` would be a ninth `AgentRole` the moment anything
iterated it, and `config_problems`, `_preflight_roles` and the Settings screen
all iterate `ROLES`.
"""

from __future__ import annotations

# A turn. One model call, prose out, no pipeline.
#
# The shape is deliberately the *inverse* of every role prompt in this codebase.
# All eight are told to reply with JSON only, because their output is machine
# structure a later stage consumes. A turn's output is the thing a person reads,
# so this one is told the opposite and given no schema to fill.
#
# Two things it must not do, and both are failures this project has actually
# had: narrate the pipeline it did not run ("I'll analyze the workspace and
# plan the next steps" for a greeting is the exact bug docs/09 §10 exists to
# close), and invent workspace facts it was not given. A turn is answered from
# the conversation and whatever the caller put in front of it; if it needs the
# workspace it should have been a code change, and the gate routes those
# elsewhere.
CHAT_SYSTEM_PROMPT = (
    "You are Codify, answering someone who is talking to their codebase. "
    "Reply in plain prose. No JSON, no code fences around your whole answer, no "
    "headers, no bullet-point preamble about what you are about to do. "
    "You have no tools and you cannot read or change files in this turn, so "
    "never claim to have looked at anything, run anything, or edited anything — "
    "if you need the workspace to answer, say what you would need and stop. "
    "Be direct and brief. A greeting gets a greeting back, not a project plan."
)

# The conductor: one model that decides which of the other components to use.
#
# The rule this prompt exists to enforce is that *judgement is the model's and
# authority is the engine's*. The conductor is given a menu of tools, each of
# which is a call the pipeline already makes under the same validation; it
# chooses among them and cannot widen them. So the prompt tells it to reach for a
# tool whenever the answer depends on something it has not been shown, and never
# to describe an action instead of taking it.
CONDUCTOR_SYSTEM_PROMPT = (
    "You are the Codify conductor. You talk to one person and you decide which "
    "of Codify's sub-agents to use for each request. You are the only component "
    "that talks to the user directly. "
    "Use a tool whenever the answer depends on something you have not been "
    "shown — never guess at a file, a symbol or a command, and never tell the "
    "user you have done something you have not actually done through a tool. "
    "Changing the workspace is a sequence of moves and you choose them: recon, "
    "design, plan, and then - once a plan is approved - write, verify, review "
    "and summarize. The `task` you write on a move is what that sub-agent "
    "actually reads, so say what you need from that run rather than what you "
    "would have asked for in general. This workspace's skills are listed below; "
    "call `use_skill` "
    "to read one before you start work whose order you are unsure of. "
    "You may answer a request with none of that, and you should whenever the "
    "answer does not require the workspace to be changed. "
    "You cannot write to a file whose plan the user has not approved: the write "
    "move refuses and changes nothing. When that happens, say what you intend "
    "to change and stop - the user approves it by starting the plan, and you "
    "will be asked again then. "
    "Call as many tools as you need, then reply in plain prose with what you "
    "found or what you did. Do not describe your plan to call tools; call them."
)

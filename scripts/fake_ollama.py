"""Fake Ollama server for smoke-testing the Codify pipeline without API keys.

Serves /api/tags (model list) and /api/generate (role-aware canned JSON).
Run: python3 scripts/fake_ollama.py
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

# FAKE_PLANNER_STEPS=2 turns the plan into two path-disjoint steps, so a live
# parallel goal has something real to overlap.
_PLANNER_STEPS = int(os.environ.get("FAKE_PLANNER_STEPS", "1") or 1)
_PLANS = {
    1: [{"title": "Add banner", "description": "Create a banner file", "suggested_paths": ["banner.txt"]}],
    2: [
        {"title": "Write alpha", "description": "Create alpha.txt", "suggested_paths": ["alpha.txt"]},
        {"title": "Write beta", "description": "Create beta.txt", "suggested_paths": ["beta.txt"]},
    ],
}
PLANNER = json.dumps({"steps": _PLANS.get(_PLANNER_STEPS, _PLANS[1])})
# FAKE_DESIGN_GOAL=1 plans the design deliverable's write step. A design-mode
# goal's brand contract IS the artifact, so the plan has to realize it — the
# step names DESIGN.md, which is the signal the fixer and the verifier read.
DESIGN_PLAN = json.dumps(
    {
        "steps": [
            {
                "title": "Write DESIGN.md",
                "description": "Publish the reviewed brand contract as the workspace's DESIGN.md",
                "suggested_paths": ["DESIGN.md"],
            }
        ]
    }
)
# The plan a live design-mode run needs; every other run gets the canned one.
PLAN_FOR_RUN = DESIGN_PLAN if os.environ.get("FAKE_DESIGN_GOAL") == "1" else PLANNER
CODER = json.dumps(
    {"files": [{"path": "banner.txt", "action": "create", "content": "hello from codify\n"}]}
)
VERIFIER_1 = json.dumps(
    {"argv": ["python", "-m", "pytest", "-q"], "verdict": None, "explanation": "running tests"}
)
VERIFIER_2 = json.dumps(
    {"argv": None, "verdict": "pass", "explanation": "no tests found; trivially passing"}
)
# A design deliverable has no code to falsify: the artifact is prose on disk, so
# the engine asks for a verdict directly (argv null). Answering with a command
# here would make a live design run exercise the run path instead of the review.
VERIFIER_DELIVERABLE = json.dumps(
    {
        "argv": None,
        "verdict": "pass",
        "explanation": "DESIGN.md is a complete, faithful realization of the contract",
    }
)
# What the verifier proposes after being told its command was refused. The engine
# feeds a refusal back exactly like command output, so the E2E for refusal
# recovery needs a reply to that feedback.
VERIFIER_RETRY = json.dumps(
    {"argv": ["git", "status"], "verdict": None, "explanation": "an allowed runner instead"}
)
VERIFIER_REFUSED = json.dumps(
    {"argv": ["touch", "marker"], "verdict": None, "explanation": "create a marker file"}
)
CRITIC = json.dumps({"decision": "approve", "reasons": []})
SCRIBE = json.dumps(
    {"summary": "Created banner.txt with a greeting line.", "commit_message": "feat: add banner file"}
)

# ── design ─────────────────────────────────────────────────────────────────
# The design agent locks a direction before the planner runs, so every E2E that
# plans a goal passes through it. The contract names a brand the workspace does
# not have (source null) and one acceptance line, so a run's transcript shows a
# real contract instead of an empty stub.
DESIGN_MARKER = "You are Codify Design"
DESIGN = json.dumps(
    {
        "applies": True,
        "artifact": "web_prototype",
        "direction": "One plain monochrome page: a single banner line, no chrome.",
        "design_system": {"name": "fake-brand", "source": None},
        "tokens": {
            "colors": [
                {"name": "ink", "value": "#0d1117"},
                {"name": "accent", "value": "#2f81f7"},
            ],
            "typography": [{"name": "body", "value": "system-ui, sans-serif"}],
            "spacing": ["4px", "8px", "16px"],
            "radii": ["6px"],
        },
        "components": [{"name": "Banner", "purpose": "the one greeting line"}],
        "conventions": ["plain text files, one line"],
        "constraints": ["no new dependencies"],
        "acceptance": ["banner.txt exists and holds one greeting line"],
        "design_md": "# fake-brand\n\nink #0d1117, accent #2f81f7.\n",
    }
)

# ── librarian ────────────────────────────────────────────────────────────────
# Round 1 asks for a real file and a search; round 2 answers with the evidence pack.
# The pack cites the file it was actually shown (so the engine keeps it) plus one
# path that does not exist (so the engine drops it and says so) — the two halves of
# the librarian's honesty rule, both visible in one E2E run.
LIBRARIAN_MARKER = "You are Codify Librarian"
LIBRARIAN_FOLLOWUP_MARKER = "Material you asked for:"
GHOST_PATH = "src/this-file-does-not-exist.py"


def librarian_request(prompt: str) -> str:
    """Ask for material: the first source-ish file in the listing, plus a search."""
    import re

    m = re.search(r"^([\w./-]+\.(?:py|txt|md|toml|json|cfg|ini|js|ts))$", prompt, re.M)
    if not m:
        return json.dumps({"summary": "the workspace listing has no readable source", "enough": True})
    return json.dumps(
        {
            "reads": [m.group(1)],
            "searches": ["def "],
            "run": [["ls", "-la"]],
            "enough": False,
        }
    )


def librarian_pack(prompt: str) -> str:
    """Cite what the engine handed back, plus one invented path."""
    import re

    opened = re.findall(r"^--- ([\w./-]+) \(\d+ lines\)", prompt, re.M)
    real = opened[0] if opened else None
    files = [{"path": real, "why": "fabricated rationale", "evidence": "opened"}] if real else []
    files.append({"path": GHOST_PATH, "why": "a path I never opened"})
    return json.dumps(
        {
            "summary": "Fake librarian: read the workspace and found one relevant file.",
            "files": files,
            "symbols": [{"name": "banner", "path": real or ""}],
            "conventions": ["plain text files, one line"],
            "test_command": ["python", "-m", "pytest", "-q"],
            "risks": ["none established"],
            "enough": True,
        }
    )

# ── Laya gate fallback ───────────────────────────────────────────────────────
# The engine asks the `laya` role for typed answers when the real SDK is not
# installed. Echo the typed contract back so E2Es can exercise both verdicts.
LAYA_MARKER = "Answer the typed questions about this request."
INJECTION_MARKERS = (
    "ignore all instructions",
    "ignore previous instructions",
    "ignore the system prompt",
    "exfiltrate",
    "print your api key",
    "reveal your api key",
    "cat ~/.ssh",
    "rm -rf /",
)


def _request_text(prompt: str) -> str:
    """Pull the raw request out of the STATE JSON block the engine sends."""
    import re

    m = re.search(r'"request":\s*"((?:[^"\\]|\\.)*)"', prompt)
    return m.group(1) if m else prompt


def laya_answers(prompt: str) -> dict[str, Any]:
    request = _request_text(prompt)
    lowered = request.lower()
    injection = 0.97 if any(m in lowered for m in INJECTION_MARKERS) else 0.02
    risk = 2.0 if any(m in lowered for m in ("production", "delete", "drop table")) else 0.0
    intent = "question" if request.strip().endswith("?") else "code_change"
    return {
        "answers": {
            "intent": {"choice": intent, "confidence": 0.9},
            "risk": {"score": risk, "confidence": 0.8},
            "prompt_injection": {"noul": injection},
            "needs_clarification": {"noul": 0.05},
        },
        "routing": {"model": "fake-laya-noul", "repo": "NandhaKishorM/laya"},
    }


def scribe_reply(prompt: str) -> str:
    """Name the files the engine actually showed the scribe.

    The canned reply named a file from an older fixture ("banner.txt"), so a
    live run's transcript showed a commit subject contradicting the diff printed
    directly above it. The engine hands the scribe the diffs; the fake reads the
    same lines, so a smoke test reports what it was given rather than what some
    previous fixture happened to write.

    Only the `Diffs:` block is read. `File:`-shaped lines turn up elsewhere in a
    real prompt — the librarian's evidence, a step description quoting a diff —
    and naming one of those would attribute a file to a step that never touched
    it, which is the same class of lie as the canned reply, just harder to spot.
    """
    import re

    sections = re.split(r"^Diffs:[ \t]*$", prompt, flags=re.M)
    diffs = sections[-1] if len(sections) > 1 else ""
    changes = re.findall(r"^File: (\S+) \((\w+)\)", diffs, re.M)
    if not changes:
        return SCRIBE
    paths = [p for p, _ in changes]
    what = paths[0] if len(paths) == 1 else f"{len(paths)} files"
    if any(action == "delete" for _, action in changes):
        return json.dumps({
            "summary": f"Removed {what}.",
            "commit_message": f"chore: remove {what}",
        })
    if all(action == "update" for _, action in changes):
        return json.dumps({
            "summary": f"Updated {what}.",
            "commit_message": f"chore: update {what}",
        })
    return json.dumps({
        "summary": f"Added {what}.",
        "commit_message": f"feat: add {what}",
    })


def fixer_files(prompt: str) -> str:
    """The fixer's reply for one prompt: the first suggested path, filled in.

    Dynamic rather than canned so Edit-Plan E2Es can prove that edited plan
    values actually drive what gets written — the target is read out of the
    prompt, not invented.
    """
    import re

    # The real fixer prompt embeds "Suggested paths (current contents):" then
    # either readable lines `- path: text` or the unreadable note
    # `- path (not readable as text)`. Search only that section — the evidence
    # section above it also contains `- path:` shapes, and matching those wrote
    # the wrong file.
    section = prompt.split("Suggested paths (current contents):", 1)[-1]
    m = re.search(r"- ([^\s:()]+)(?: |:| \(not readable\))", section)
    target = m.group(1) if m else "banner.txt"
    # A design deliverable's write step is handed the reviewed draft verbatim
    # ("write exactly this"): a fake fixer that wrote its canned line instead
    # would make a live design run look like it had drifted from its own
    # contract. Echo the draft back, as instructed.
    draft = re.search(
        r"--- DESIGN\.md \(write exactly this\) ---\n(.*?)\n--- end DESIGN\.md ---",
        prompt,
        re.S,
    )
    content = draft.group(1) if draft else "hello from codify"
    if not content.endswith("\n"):
        content += "\n"
    return json.dumps({"files": [{"path": target, "action": "create", "content": content}]})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        pass

    def _send(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/v1beta/models"):
            # Google-shaped discovery, so the picker's "the provider reports this
            # cannot hold a conversation" signal can be exercised for real: one
            # model that supports generateContent and one that only embeds.
            self._send(
                {
                    "models": [
                        {
                            "name": "models/gemini-2.5-flash",
                            "displayName": "Gemini 2.5 Flash",
                            "supportedGenerationMethods": ["generateContent"],
                            "inputTokenLimit": 1048576,
                        },
                        {
                            "name": "models/text-embedding-004",
                            "displayName": "Text Embedding 004",
                            "supportedGenerationMethods": ["embedContent"],
                            "inputTokenLimit": 2048,
                        },
                    ]
                }
            )
        elif self.path.startswith("/v1/models"):
            # OpenAI-compatible catalog, so model discovery can be exercised for
            # the openai_compat protocol as well as Ollama's own /api/tags.
            self._send(
                {
                    "object": "list",
                    "data": [
                        {"id": "legacy-model", "created": 1700000000, "owned_by": "fake"},
                        {"id": "just-released-model", "created": 1900000000, "owned_by": "fake"},
                        {"id": "released-this-afternoon", "created": 2000000000, "owned_by": "fake"},
                    ],
                }
            )
        elif self.path == "/api/tags":
            self._send(
                {
                    "models": [
                        {"name": "qwen2.5-coder:7b", "details": {"parameter_size": "7B"}},
                        {"name": "llama3.1:8b", "details": {"parameter_size": "8B"},
                         "nomic": None},
                    ]
                }
            )
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        prompt = payload.get("prompt", "")

        # /api/generate routing. The engine prepends the role's system prompt,
        # so match markers anywhere in the prompt (not startswith). Markers are
        # chosen to be unique per role's user prompt; order matters.
        if LAYA_MARKER in prompt:
            out = json.dumps(laya_answers(prompt))
        elif DESIGN_MARKER in prompt:
            out = DESIGN
        elif LIBRARIAN_FOLLOWUP_MARKER in prompt:
            # Second reconnaissance round: answer with the evidence pack.
            out = librarian_pack(prompt)
        elif LIBRARIAN_MARKER in prompt:
            out = librarian_request(prompt)
        elif "Command ran." in prompt:
            out = VERIFIER_2
        elif "was NOT run" in prompt:
            # The sandbox refused the previous command. FAKE_REFUSE_FIRST=1 makes
            # the verifier pick something the allowlist permits; without it this
            # path is only reached if a real refusal happened.
            out = VERIFIER_RETRY
        elif "Diffs:" in prompt and "You are Codify Critic" in prompt:
            out = CRITIC
        elif "You are Codify Scribe" in prompt:
            out = scribe_reply(prompt)
        elif "Diffs:" in prompt:
            # Legacy shape: a critic-shaped prompt without the role marker.
            out = CRITIC
        elif "Suggested paths" in prompt:
            # FAKE_BROKEN=1 also corrupts fixer replies (used by the per-role
            # routing E2E to prove which server each role actually hit).
            if os.environ.get("FAKE_BROKEN") == "1":
                out = "I am not JSON at all."
            else:
                out = fixer_files(prompt)
        elif "Title:" in prompt and "The librarian answered your follow-up" in prompt:
            # Round 2 after a consult: plan, citing the material that came back.
            out = PLAN_FOR_RUN
        elif "Title:" in prompt:
            if os.environ.get("FAKE_PLANNER_CONSULT") == "1":
                out = json.dumps({"consult": {"reads": ["greet.py"]}})
            else:
                out = PLAN_FOR_RUN
        elif "do not run a command" in prompt:
            # A design deliverable: the verifier reviews the written file and
            # returns a verdict rather than proposing a command.
            out = VERIFIER_DELIVERABLE
        elif "Step:" in prompt:
            # Every role prompt carries "Step: <title>", so this branch sits below
            # the critic/scribe/fixer matchers. The verifier is the only role
            # asked this way without a Diffs/Changed files/Suggested paths marker.
            if os.environ.get("FAKE_BROKEN") == "1":
                # FAKE_BROKEN=1 turns every verifier reply into garbage, so an E2E
                # can prove which server a role actually hit.
                out = "I am not JSON at all."
            elif os.environ.get("FAKE_REFUSE_FIRST") == "1":
                # Propose something the sandbox forbids, so the refusal-recovery
                # path is exercised end to end (the retry is VERIFIER_RETRY above).
                out = VERIFIER_REFUSED
            else:
                out = VERIFIER_1
        else:
            out = SCRIBE

        # Real Ollama reports token counts on every generate; include them so
        # the engine's usage accounting (and the UI's usage card) can be
        # exercised end to end without a live model. When the engine asks to
        # stream, answer NDJSON piece-wise (role replies are small, so the
        # pieces are artificial splits of the same JSON the blocking path
        # serves) — this is what exercises the engine's streaming parser.
        if payload.get("stream"):
            # Optional per-call delay so live runs can prove step-level
            # concurrency (two in-flight fixer calls overlap in time).
            delay = float(os.environ.get("FAKE_FIXER_DELAY_MS", "0")) / 1000.0
            if delay:
                import time as _t
                _t.sleep(delay)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            third = max(1, len(out) // 3)
            pieces = [out[:third], out[third:2 * third], out[2 * third:]]
            for i, piece in enumerate(pieces):
                done = i == len(pieces) - 1
                line = {"response": piece, "done": done}
                if done:
                    line["prompt_eval_count"] = 128
                    line["eval_count"] = 64
                self.wfile.write(json.dumps(line).encode() + b"\n")
            return
        self._send({
            "response": out,
            "prompt_eval_count": 128,
            "eval_count": 64,
        })


if __name__ == "__main__":
    port = int(os.environ.get("FAKE_OLLAMA_PORT", "11435"))
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()

"""Tool calling, in one dialect the four providers are translated into.

`BaseProvider.complete` is single-shot: a system prompt, a user prompt, one
JSON reply. That is the right shape for all eight roles, because each of them
produces structure a *later stage* consumes exactly once. The conductor is not
like that — it has to call a tool, read the result, and decide again, so it
needs a message array and a reply that can carry zero or more tool calls.

So this module holds the neutral form, and `engine/providers.py` holds the four
translations. Keeping the translations here rather than inline in each provider
is deliberate: they are the part that rots, because they are the part with no
type checker — a `content` block that should be a `tool_result` is a runtime 400
from someone else's API, not an error in our code. Written once, they are read
once.

The neutral message shape, which is what `engine/conductor.py` builds:

    {"role": "user",      "content": str}
    {"role": "assistant", "content": str, "tool_calls": [ToolCall, ...]}
    {"role": "tool",      "tool_call_id": str, "name": str, "content": str}

and the neutral reply:

    ToolReply(text=str, tool_calls=[ToolCall, ...])

A reply with both text and tool calls is normal and legal — a model often says
"let me look at that" *and* asks for the file — so the conductor streams the
text as it arrives and still runs the calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """One tool the model may call: a name, a description, a JSON schema.

    The description is not decoration. It is the only thing telling the model
    when to reach for this tool rather than guess, and the conductor's whole
    value is that it guesses less than the pipeline's stages would — so the
    wording carries weight proportional to how much a wrong guess costs.
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})

    def to_openai(self) -> dict[str, Any]:
        """The OpenAI-descended shape: Anthropic-compat, Ollama and every
        `openai_compat` provider in `BUILTIN_PROVIDERS` take this verbatim."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic(self) -> dict[str, Any]:
        # Anthropic names the schema `input_schema` and has no `type` wrapper.
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def to_google(self) -> dict[str, Any]:
        # Google wants only the parameter *names* declared at the top level and
        # the properties under `parameters`; it derives the rest from the types.
        properties = self.parameters.get("properties") or {}
        # `required` is a statement about the schema, not about a type. It used
        # to be computed as `type != "string"`, which inverted every tool this
        # pipeline sends: all five declare their required parameter as a string
        # (`path`, `query`, `task`) so they went out declaring **nothing**
        # required, while the optional numbers (`offset`, `limit`) and the
        # optional flag (`regex`) went out declaring themselves required. A
        # model reading that calls `read_file` with no path and invents an
        # `offset`. Nothing raised — Gemini accepts the document either way,
        # which is exactly why a wrong `required` is the kind of defect only a
        # live call ever reveals.
        declared = self.parameters.get("required") or []
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    key: {"type": _google_type(spec.get("type", "string"))}
                    for key, spec in properties.items()
                    if isinstance(spec, dict)
                },
                # Filtered to what is declared: Google takes `required` as a
                # subset of `properties`, and a schema that names a property it
                # did not declare is the same mistake in the other direction.
                "required": [
                    key for key in declared if isinstance(properties.get(key), dict)
                ],
            },
        }


def coerce_tool_reply(
    text: str,
    calls: list[ToolCall],
    tools: list[ToolSpec],
) -> ToolReply:
    """A reply, plus any tool call the provider *wrote as text* rather than sent.

    Measured against a live `qwen2.5-coder:7b` on Ollama 0.x, which ignored
    `message.tool_calls` entirely and answered with the call written into
    `message.content` as a JSON document:

        {"content": "{\\"name\\": \\"read_file\\", \\"arguments\\": {...}}"}

    The first version of this returned that JSON as the turn's final answer and
    showed it to the user. Nothing in a unit test finds it, because a test
    double returns what the implementation expects; only a real model returns
    what the model does.

    The fallback is guarded, because a normal reply can also be JSON: the
    document must carry a `name` (or `function.name`) **that is one of the
    tools actually offered**. A prose answer shaped like `{"name": ...}` with a
    name nobody offered is left as text, which is the safe direction — the
    model is shown its own words rather than having a call invented from them.
    """
    if calls:
        return ToolReply(text=text, tool_calls=calls)
    recovered = _from_content(text, tools)
    return ToolReply(text="" if recovered else text, tool_calls=recovered)


def _from_content(text: str, tools: list[ToolSpec]) -> list[ToolCall]:
    body = (text or "").strip()
    if not body:
        return []
    parsed: Any = None
    if body.startswith("{"):
        try:
            parsed = json.loads(body)
        except ValueError:
            return []
    elif body.startswith("```"):
        # A model that writes ```json ... ``` instead of calling the tool is
        # common enough to be worth one retry, and cheap: this path only runs
        # when the structured one came back empty.
        inner = body.strip("`").strip()
        if inner.lower().startswith("json"):
            inner = inner[4:].strip()
        try:
            parsed = json.loads(inner)
        except ValueError:
            return []
    else:
        return []
    if not isinstance(parsed, dict):
        return []
    raw_name = parsed.get("name")
    arguments = parsed.get("arguments")
    if raw_name is None and isinstance(parsed.get("function"), dict):
        raw_name = parsed["function"].get("name")
        arguments = parsed["function"].get("arguments")
    name = str(raw_name or "").strip()
    if not name or not any(t.name == name for t in tools):
        return []
    return [
        ToolCall(
            id=str(parsed.get("id") or f"call_{name}"),
            name=name,
            arguments=coerce_arguments(
                next((t for t in tools if t.name == name), None), arguments
            ),
        )
    ]


def _google_type(json_type: Any) -> str:
    """JSON Schema's type names, mapped to the subset Google accepts.

    Everything becomes a string on Google's side. It is a real loss — a numeric
    argument comes back as text — and `coerce_arguments` below is what undoes
    it, using the same schema the model was given. Losing the type silently
    would be worse than the round trip.
    """
    return {
        "string": "STRING",
        "integer": "INTEGER",
        "number": "NUMBER",
        "boolean": "BOOLEAN",
        "array": "ARRAY",
        "object": "OBJECT",
    }.get(str(json_type), "STRING")


@dataclass(frozen=True)
class ToolCall:
    """One call the model asked for.

    `id` is the provider's correlation handle and is echoed back on the result;
    getting it wrong means the model cannot match a result to its question, and
    the loop fails in a way that looks like the model going dumb rather than
    the transport losing a string.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolReply:
    """What a tool-capable call returned."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def coerce_arguments(
    spec: ToolSpec | None, raw: Any
) -> dict[str, Any]:
    """Best-effort arguments from whatever shape the provider delivered.

    Providers disagree: OpenAI hands back a JSON *string*, Anthropic hands back
    a parsed object, Google hands back a dict of strings. A caller that had to
    care would be three parsers in the conductor, so this is one, and it also
    repairs the types Google's stringly round trip costs us — an argument the
    schema called an integer comes back as `"3"` and is turned back into `3`
    here rather than being compared as a string three layers down.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            # A model that emitted half an object. An empty argument dict lets
            # the tool itself refuse with a message the model can read, which is
            # recoverable; raising here would kill the whole turn.
            raw = {}
    if not isinstance(raw, dict):
        return {}
    out = dict(raw)
    properties = (spec.parameters.get("properties") if spec else None) or {}
    for key, declared in properties.items():
        if not isinstance(declared, dict) or key not in out:
            continue
        want = str(declared.get("type", "string"))
        value = out[key]
        try:
            if want == "integer" and isinstance(value, str) and value.strip().isdigit():
                out[key] = int(value.strip())
            elif want == "number" and isinstance(value, str):
                out[key] = float(value.strip())
            elif want == "boolean" and isinstance(value, str):
                out[key] = value.strip().lower() in ("true", "1", "yes")
        except ValueError:
            pass
    return out


# ── message-array translation ───────────────────────────────────────────────
#
# Each of these takes the neutral list and returns the provider's own. They are
# the four places a mistake becomes someone else's 400, so they are the four
# places worth reading twice.


def to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI, and every `openai_compat` provider: a flat role/content array."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": m.get("tool_call_id", ""),
                "content": m.get("content", ""),
            })
        elif role == "assistant" and m.get("tool_calls"):
            out.append({
                "role": "assistant",
                "content": m.get("content") or None,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": json.dumps(c.arguments),
                        },
                    }
                    for c in m["tool_calls"]
                ],
            })
        else:
            out.append({"role": role or "user", "content": m.get("content", "")})
    return out


def to_ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ollama's dialect: OpenAI's roles, but `function.arguments` is an **object**.

    Measured against a live Ollama, and the difference is a hard 400 rather
    than a cosmetic one. Ollama's Go chat template parses `arguments` expecting
    a JSON object; handed the JSON *string* OpenAI's format specifies it fails
    with `Value looks like object, but can't find closing '}' symbol` — which
    reads as a template bug and is not.

    So this is `to_openai_messages` with one difference, and it is here rather
    than behind a flag because the difference is a wire contract between two
    parties that disagree.
    """
    out = to_openai_messages(messages)
    for turn in out:
        for call in turn.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    function["arguments"] = json.loads(arguments) if arguments.strip() else {}
                except ValueError:
                    function["arguments"] = {}
    return out


def parse_openai_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        function = raw.get("function") or {}
        name = str(function.get("name") or "")
        if not name:
            continue
        calls.append(ToolCall(
            id=str(raw.get("id") or f"call_{index}"),
            name=name,
            arguments=coerce_arguments(None, function.get("arguments")),
        ))
    return calls


def to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic: no `system` role, and a tool result is a *user* turn whose
    content is a `tool_result` block. Consecutive tool results share one turn —
    a `tool_result` block in its own message per call is rejected."""
    out: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": m.get("content", ""),
            }
            # Attach to the turn we are already building, or start one.
            if out and out[-1].get("role") == "user" and pending:
                pending.append(block)
            else:
                pending = [block]
                out.append({"role": "user", "content": pending})
            continue
        pending = []
        if role == "assistant" and m.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            blocks.extend(
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                for c in m["tool_calls"]
            )
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": role or "user", "content": m.get("content", "")})
    return out


def parse_anthropic_tool_calls(content: list[dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for block in content or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        calls.append(ToolCall(
            id=str(block.get("id") or f"call_{len(calls)}"),
            name=str(block.get("name") or ""),
            arguments=coerce_arguments(None, block.get("input")),
        ))
    return calls


def to_google_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Google: `contents` with `user`/`model` roles, and a function response is
    a `functionResponse` part inside a `user` turn."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            out.append({
                "role": "user",
                "parts": [{
                    "functionResponse": {
                        "name": m.get("name", ""),
                        "response": {"result": m.get("content", "")},
                    }
                }],
            })
        elif role == "assistant":
            parts: list[dict[str, Any]] = []
            if m.get("content"):
                parts.append({"text": m["content"]})
            parts.extend(
                {"functionCall": {"name": c.name, "args": c.arguments}}
                for c in (m.get("tool_calls") or [])
            )
            out.append({"role": "model", "parts": parts})
        else:
            out.append({"role": "user", "parts": [{"text": m.get("content", "")}]})
    return out

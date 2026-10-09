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
import re
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
        # Google wants the properties under `parameters`, each rebuilt in its own
        # vocabulary (upper-case types, string-only enums). `_google_schema` keeps
        # what the model needs to choose an argument (the description, the enum, the
        # element type of an array); this used to send only `{"type": ...}` per
        # property, so Gemini never saw a single description.
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
                    key: _google_schema(spec)
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


_JSON_TYPES = frozenset({"string", "integer", "number", "boolean", "array", "object"})
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DIALECTS = ("openai", "anthropic", "google")


def _type_matches(value: Any, json_type: str) -> bool:
    """Whether an `enum` member is a legal value of the declared JSON type."""
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _schema_problems(schema: Any, path: str, upper: bool) -> list[str]:
    """What is wrong with one schema node, recursing into arrays and objects."""
    if not isinstance(schema, dict):
        return [f"{path}: a schema must be an object, got {type(schema).__name__}"]
    raw_type = schema.get("type")
    if not isinstance(raw_type, str):
        return [f"{path}: no type"]
    json_type = raw_type.lower()
    if json_type not in _JSON_TYPES:
        return [f"{path}: unknown type {raw_type!r}"]
    problems: list[str] = []
    if upper and path != "parameters" and raw_type != raw_type.upper():
        problems.append(f"{path}: Google wants the type upper-case, got {raw_type!r}")
    if "description" in schema and not (
        isinstance(schema["description"], str) and schema["description"].strip()
    ):
        problems.append(f"{path}: an empty or non-string description")
    if "enum" in schema:
        members = schema["enum"]
        if not isinstance(members, list) or not members:
            problems.append(f"{path}: enum must be a non-empty list")
        else:
            for member in members:
                # Google takes string enums only, and `_google_schema` stringifies them.
                wanted = "string" if upper else json_type
                if not _type_matches(member, wanted):
                    problems.append(f"{path}: enum member {member!r} is not a {wanted}")
    if json_type == "array":
        # The one that went out wrong: an array with no `items` is rejected by OpenAI's
        # function validator and by Gemini's, and nothing here raised because every test
        # in this repo talks to a double.
        if "items" not in schema:
            problems.append(f"{path}: an array with no items")
        else:
            problems.extend(_schema_problems(schema["items"], f"{path}[]", upper))
    if json_type == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            problems.append(f"{path}: properties must be an object")
            properties = {}
        for key, spec in properties.items():
            problems.extend(_schema_problems(spec, f"{path}.{key}", upper))
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
            problems.append(f"{path}: required must be a list of names")
        else:
            for name in required:
                if name not in properties:
                    problems.append(f"{path}: required names {name!r}, which is not a property")
    return problems


def _kept(source: Any, shaped: Any, path: str) -> list[str]:
    """What the Google translation dropped that the source schema said."""
    if not isinstance(source, dict) or not isinstance(shaped, dict):
        return []
    problems: list[str] = []
    for key in ("description", "enum"):
        if key in source and key not in shaped:
            problems.append(f"{path}: Google shape dropped {key!r}")
    if "items" in source:
        problems.extend(_kept(source["items"], shaped.get("items"), f"{path}[]"))
    for key, spec in (source.get("properties") or {}).items():
        problems.extend(
            _kept(spec, (shaped.get("properties") or {}).get(key), f"{path}.{key}")
        )
    return problems


def schema_problems(spec: ToolSpec, dialect: str) -> list[str]:
    """Everything wrong with `spec` as the provider will receive it, or `[]`.

    The four translations in `engine/providers.py` are the part of this pipeline
    with no type checker, and the failure mode is someone else's HTTP 400 on a
    tool the model needs. So the document each provider is actually sent is
    checked here, from the same `to_*` methods the providers call: a name the
    provider accepts, a non-empty description, a known type on every property, an
    array that declares its `items`, enum members that are of the declared type,
    and a `required` that names only properties that exist. For Google it also
    checks that the translation kept the descriptions and enums the source had,
    because that translation rebuilds the schema rather than passing it through.

    This is our reading of the providers' rules, not their validator: it can say
    a schema is well-formed, and only a live call says a provider accepts it.
    """
    if dialect not in DIALECTS:
        raise ValueError(f"unknown dialect {dialect!r}; expected one of {DIALECTS}")
    if dialect == "openai":
        shaped = spec.to_openai()["function"]
        schema = shaped["parameters"]
    elif dialect == "anthropic":
        shaped = spec.to_anthropic()
        schema = shaped["input_schema"]
    else:
        shaped = spec.to_google()
        schema = shaped["parameters"]
    problems: list[str] = []
    if not _TOOL_NAME.match(str(shaped.get("name", ""))):
        problems.append(f"name {shaped.get('name')!r} is not 1-64 of [A-Za-z0-9_-]")
    if not str(shaped.get("description", "")).strip():
        problems.append("no description")
    if str(schema.get("type", "")).lower() != "object":
        problems.append("parameters: the top level must be an object")
    problems.extend(_schema_problems(schema, "parameters", dialect == "google"))
    if dialect == "google":
        problems.extend(_kept(spec.parameters, schema, "parameters"))
        # The Google translation repairs an array with no `items` (it defaults to STRING) and
        # filters `required` down to declared properties, so the shaped document is clean
        # where the spec is not. A spec defect hidden by the translator is still a defect:
        # the same tool is sent to OpenAI and Anthropic as written.
        problems.extend(
            p for p in _schema_problems(spec.parameters, "parameters", False) if p not in problems
        )
    return problems


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


def _last_fenced_json(body: str) -> Any:
    """The last fenced block in `body` that parses as an object, or None.

    Last rather than first: when a model writes out more than one shape, the one
    it finished on is the move it settled on. Blocks are tried newest-first and
    the first parse wins, so a reply with an earlier illustrative fence and a
    later real call recovers the real call.
    """
    fence = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)```", re.DOTALL)
    for match in reversed(list(fence.finditer(body))):
        inner = match.group(1).strip()
        if not inner.startswith("{"):
            continue
        try:
            parsed = json.loads(inner)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _from_content(text: str, tools: list[ToolSpec]) -> list[ToolCall]:
    body = (text or "").strip()
    if not body:
        return []
    # Three shapes, tried in order of how unambiguous they are, and the order is
    # the point: the strict ones are tried first and a failure in either falls
    # through rather than giving up.
    parsed: Any = None
    if body.startswith("{"):
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
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
            parsed = None
    if not isinstance(parsed, dict):
        # Prose that *contains* a fenced call, which is the third shape a model
        # reaches for and the one a live run found: against a 7B, the conductor
        # said "Let's proceed with the plan.", wrote `2. design - Lock the
        # direction`, and then emitted the call inside a ```json block instead
        # of calling anything. The reply was returned as the turn's answer with
        # the move never made.
        #
        # Reached by falling through rather than by a separate branch, because
        # the whole-body shapes fail on this too: a reply that *opens* with a
        # fence and then continues in prose parses as neither. Requiring a shape
        # and then giving up is how the third one went unnoticed.
        #
        # Searching the body is a wider net, so it is guarded exactly the way
        # the whole-body path is: the name has to be one of the tools the model
        # was actually offered, checked just below alongside the other shapes. A
        # model explaining `{"name": "read_file"}` to the user is an accepted
        # casualty of that guard rather than a risk taken with it — the
        # alternative is a turn that narrates work with no way to notice.
        parsed = _last_fenced_json(body)
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


def _google_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """One property of a tool's schema, rebuilt in Google's vocabulary.

    Google's `Schema` is a subset of JSON Schema with upper-case types, and it
    cannot be handed ours verbatim. Rebuilding it must not cost the model the
    words that tell it what an argument is for, which is what the first version
    did: it kept `type` and nothing else, so Gemini was told a tool had a STRING
    called `path` and never that it was "workspace-relative".

    Kept: `description`, `enum` (Google takes string enums only, so members are
    stringified, and only on a string property), `items`, and nested
    `properties` / `required`. An array always gets an `items`, defaulting to
    STRING, because Google refuses an array that does not say what it holds.
    """
    json_type = str(schema.get("type", "string"))
    out: dict[str, Any] = {"type": _google_type(json_type)}
    if isinstance(schema.get("description"), str) and schema["description"].strip():
        out["description"] = schema["description"]
    if json_type == "string" and isinstance(schema.get("enum"), list) and schema["enum"]:
        out["enum"] = [str(member) for member in schema["enum"]]
    if json_type == "array":
        items = schema.get("items")
        out["items"] = _google_schema(items) if isinstance(items, dict) else {"type": "STRING"}
    if json_type == "object":
        properties = schema.get("properties")
        if isinstance(properties, dict):
            out["properties"] = {
                key: _google_schema(spec)
                for key, spec in properties.items()
                if isinstance(spec, dict)
            }
            required = schema.get("required")
            if isinstance(required, list):
                out["required"] = [key for key in required if key in out["properties"]]
    return out


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
    # Opaque, and only Google's: the `thoughtSignature` a Gemini 3 model puts on a step's first function call, which
    # must go back on that same part or the next request is a 400 (`to_google_contents`). Never read, never invented.
    signature: str | None = None


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
            elif want == "array" and isinstance(value, str):
                out[key] = _array_from_text(value)
        except ValueError:
            pass
    return out


def _array_from_text(value: str) -> Any:
    """An array argument a small model wrote out as text, or the text unchanged.

    `"[\\"pytest\\", \\"-q\\"]"` is parsed; nothing else is guessed at. `"pytest -q"`
    could be one argument or two and `"[1, 2"` is a half-written list, so both come
    back untouched and the tool (the sandbox, for an argv) refuses them in words the
    model can read. Only a list of strings is accepted, since both array parameters
    this pipeline declares are argv.
    """
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
        return parsed
    return value


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
        if role == "assistant" and not m.get("tool_calls") and not m.get("content"):
            # A reply with nothing in it, kept in the history when the conductor nudges the model to act. The
            # API refuses an empty message that is not the last one, and the neighbouring user turns are merged.
            continue
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
    a `functionResponse` part inside a `user` turn.

    The results of one model turn's calls share **one** user turn, a part each and in the order the calls were
    made: Gemini answers a 400 when the number of response parts differs from the number of call parts of the turn
    before, and the conductor appends one `tool` message per call. (The same shape `to_anthropic_messages` builds.)
    """
    out: list[dict[str, Any]] = []
    results: list[dict[str, Any]] | None = None
    for m in messages:
        role = m.get("role")
        if role == "tool":
            part_out = {
                "functionResponse": {
                    "name": m.get("name", ""),
                    "response": {"result": m.get("content", "")},
                }
            }
            if results is not None:
                results.append(part_out)
            else:
                results = [part_out]
                out.append({"role": "user", "parts": results})
            continue
        results = None
        if role == "assistant":
            parts: list[dict[str, Any]] = []
            if m.get("content"):
                parts.append({"text": m["content"]})
            for c in m.get("tool_calls") or []:
                part: dict[str, Any] = {"functionCall": {"name": c.name, "args": c.arguments}}
                if c.signature:
                    part["thoughtSignature"] = c.signature
                parts.append(part)
            if parts:
                # A turn with no parts is a 400; an empty reply the conductor kept in its history has nothing to say.
                out.append({"role": "model", "parts": parts})
        else:
            out.append({"role": "user", "parts": [{"text": m.get("content", "")}]})
    return out

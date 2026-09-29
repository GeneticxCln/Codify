# Captured model replies

Each fixture is a pair, and `tests/test_extract_json.py::TestCapturedReplies` holds every pair to the same rule
as the hand-written corpus:

- `<name>.txt` — a model's reply, **byte for byte**, as `provider.complete` returned it;
- `<name>.json` — `{"role": "<role>", "model": "<exact model id and quantisation>", "expect": <the object the
  reply must parse to>}`.

**Provenance matters here.** The 23 shapes in `test_extract_json.py` are *written from* the output shapes these
model families are documented to produce and that the audit reproduced; they are not captures and are not
presented as such. A capture goes here only when it came from a real model, with the exact model id in its
`.json`. `scripts/drive_a_turn.py` and `make bench` are the ways to get one.

## What is here now

The first two captures are real, from `Qwen2.5-1.5B-Instruct` (Q4_K_M GGUF) served by `llama_cpp.server`
(llama-cpp-python 0.3.35, CPU only) and driven through `benchmarks.runner`'s `repo-rename-greeting` task
with the goal's `trace` flag on, so the recorder kept each raw reply (`trace_calls.response`):

| Fixture | What the model did |
|---|---|
| `qwen2.5-1.5b-fixer-triple-quoted-content` | wrote each file's `content` as a Python `"""` string, and the content held docstrings of its own |
| `qwen2.5-1.5b-design-triple-quoted-note` | the same, for the design role's `design_md` |
| `qwen2.5-1.5b-fixer-bare-list` | answered the fixer with the `files` array and no `{"files": ...}` around it — twice in a row when asked again — and with `"path": "/src/app.py"`, the same triple-quoted content as above |

The `expect` objects were built from the replies' text by a separate script that slices between the
delimiters — not by `extract_json`, so the expectation does not agree with the parser by construction.
Before this change the fixer capture failed the run twice in a row (`agent_output_invalid`, with the
re-ask); nothing in the audit's list of fifteen shapes had predicted it.

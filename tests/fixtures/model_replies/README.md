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

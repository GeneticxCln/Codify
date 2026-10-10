from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

from engine.providers import OpenAICompatProvider, OllamaProvider


def openai_reply(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class _FakeOpenAIServer(ThreadingHTTPServer):
    """Loopback stand-in for an OpenAI-compatible endpoint.

    Configurable on two axes the JSON-mode logic depends on: whether /v1/models
    advertises structured-output capability, and whether POSTs carrying
    response_format are accepted or rejected with a 400.
    """

    def __init__(self, advertise: bool, reject_json_mode: bool, reject_stream: bool = False,
                 split_usage: bool = False, models: list[Any] | None = None):
        self.advertise = advertise
        # When set, /v1/models answers exactly this list: real servers do not all shape the entries alike.
        self.models = models
        self.reject_json_mode = reject_json_mode
        self.reject_stream = reject_stream
        # split_usage: emit the usage block on its own chunk, ahead of the
        # finish chunk — the shape real providers produce when the usage
        # counters are computed after the last token.
        self.split_usage = split_usage
        self.post_bodies: list[dict[str, Any]] = []
        self.models_requests = 0
        class Handler(BaseHTTPRequestHandler):
            # `self.server` is the base server to the type checker; the cast
            # names what it actually is, so the counters below are typed.
            def do_GET(self) -> None:
                server = cast(_FakeOpenAIServer, self.server)
                if self.path.endswith("/models"):
                    server.models_requests += 1
                    # `capabilities` is attached only when advertised, so the
                    # reply is deliberately not uniformly shaped.
                    data: dict[str, Any] = {"data": [{"id": "m1"}] if server.models is None else server.models}
                    if server.models is None and server.advertise:
                        data["data"][0]["capabilities"] = {"json_object": True}
                    body = json.dumps(data).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

            def do_POST(self) -> None:
                server = cast(_FakeOpenAIServer, self.server)
                length = int(self.headers.get("content-length", 0))
                server.post_bodies.append(json.loads(self.rfile.read(length)))
                if server.post_bodies[-1].get("stream"):
                    if server.reject_stream:
                        self.send_error(400, "streaming is not supported")
                        return
                    # SSE: content chunks, then usage and the finish chunk —
                    # together, or on separate chunks when split_usage is set.
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    # Usage rides the finish chunk or its own chunk before it,
                    # so the two branches are not the same shape.
                    chunks: tuple[dict[str, Any], ...]
                    if server.split_usage:
                        chunks = (
                            {"choices": [{"delta": {"content": "hel"}}]},
                            {"usage": {"prompt_tokens": 5, "completion_tokens": 2}},
                            {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
                        )
                    else:
                        chunks = (
                            {"choices": [{"delta": {"content": "hel"}}]},
                            {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
                        )
                    for chunk in chunks:
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                if server.reject_json_mode and "response_format" in server.post_bodies[-1]:
                    self.send_error(400, "response_format is not supported")
                    return
                body = json.dumps(openai_reply("ok")).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:  # silence the test log
                pass

        super().__init__(("127.0.0.1", 0), Handler)


class _ServerMixin(unittest.TestCase):
    def _start(self, advertise: bool, reject_json_mode: bool = False, reject_stream: bool = False,
               split_usage: bool = False, models: list[Any] | None = None) -> _FakeOpenAIServer:
        server = _FakeOpenAIServer(advertise, reject_json_mode, reject_stream, split_usage, models)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}/v1"
        return server


class TestJsonModeCapability(_ServerMixin):
    """response_format is a wire guarantee only where the server says it can."""

    def _provider(self, base: str) -> OpenAICompatProvider:
        # One provider per test: the capability probe and the rejection memory
        # are cached on the instance, which is exactly what these tests assert.
        return OpenAICompatProvider("test-key", base)

    def _complete(self, provider: OpenAICompatProvider) -> str:
        import asyncio

        return asyncio.run(provider.complete("sys", "user", "m1", 0.0, 64))

    def test_a_capable_server_gets_response_format_and_the_probe_is_cached(self) -> None:
        server = self._start(advertise=True)
        provider = self._provider(self.base)

        out = self._complete(provider)
        # Second call, deliberately unasserted: it must reuse the cached probe.
        self._complete(provider)

        self.assertEqual(out, "ok")
        self.assertEqual(server.models_requests, 1, "the capability probe must run once, not per call")
        self.assertTrue(all("response_format" in b for b in server.post_bodies))
        self.assertEqual(server.post_bodies[0]["response_format"], {"type": "json_object"})

    def test_a_server_without_the_capability_is_never_sent_the_field(self) -> None:
        server = self._start(advertise=False)
        provider = self._provider(self.base)

        out = self._complete(provider)

        self.assertEqual(out, "ok")
        self.assertTrue(server.post_bodies)
        self.assertFalse(any("response_format" in b for b in server.post_bodies))

    def test_a_server_that_rejects_the_field_is_retried_without_it(self) -> None:
        """A 400 on response_format must not fail the call — it is a capability
        the probe got wrong, and the fix is to stop sending the field."""
        server = self._start(advertise=True, reject_json_mode=True)
        provider = self._provider(self.base)

        out = self._complete(provider)
        out2 = self._complete(provider)

        self.assertEqual(out, "ok")
        self.assertEqual(out2, "ok")
        self.assertEqual(len(server.post_bodies), 3, "first call: reject + plain retry; second: plain only")
        self.assertIn("response_format", server.post_bodies[0])
        self.assertNotIn("response_format", server.post_bodies[1])
        self.assertNotIn("response_format", server.post_bodies[2])


    def test_an_odd_entry_in_the_models_list_does_not_hide_a_capable_model_after_it(self) -> None:
        """The probe reads every entry, and one that is shaped differently is skipped, not fatal.

        `isinstance(caps, dict) and caps.get("json_schema") or caps.get("json_object")` parses as
        `(a and b) or c`, so the guard did not cover the second `.get`: a model whose `capabilities` is a list
        raised, the probe's blanket `except` turned that into "not supported", and every entry after it was never read.
        """
        for odd in ({"id": "list-caps", "capabilities": ["streaming"]}, {"id": "no-caps"}, "a-bare-string", None):
            with self.subTest(odd=odd):
                server = self._start(
                    advertise=False, models=[odd, {"id": "m1", "capabilities": {"json_object": True}}],
                )
                provider = self._provider(self.base)

                self.assertEqual(self._complete(provider), "ok")

                self.assertTrue(server.post_bodies)
                self.assertTrue(all("response_format" in b for b in server.post_bodies), server.post_bodies)

    def test_a_schema_capability_alone_is_enough(self) -> None:
        server = self._start(advertise=False, models=[{"id": "m1", "capabilities": {"json_schema": True}}])
        provider = self._provider(self.base)

        self.assertEqual(self._complete(provider), "ok")

        self.assertTrue(all("response_format" in b for b in server.post_bodies), server.post_bodies)

    def test_no_model_that_advertises_it_means_the_field_is_not_sent(self) -> None:
        server = self._start(
            advertise=False, models=[{"id": "a", "capabilities": ["x"]}, {"id": "b", "capabilities": {"vision": True}}],
        )
        provider = self._provider(self.base)

        self.assertEqual(self._complete(provider), "ok")

        self.assertFalse(any("response_format" in b for b in server.post_bodies))


class TestOpenAIStreaming(_ServerMixin):
    """With on_delta attached the provider streams SSE and assembles the text."""

    def _run(self, provider: OpenAICompatProvider) -> tuple[str, list[str]]:
        import asyncio
        seen: list[str] = []
        provider.on_delta = seen.append
        out = asyncio.run(provider.complete("sys", "user", "m1", 0.0, 64))
        return out, seen

    def test_streamed_text_assembles_in_order_and_reports_usage(self) -> None:
        server = self._start(advertise=False)
        provider = OpenAICompatProvider("test-key", self.base)
        usage: list[dict[str, Any]] = []
        provider.usage_sink = usage.append

        out, seen = self._run(provider)

        self.assertEqual(out, "hello")
        self.assertEqual(seen, ["hel", "hello"], "each callback carries the full text so far")
        self.assertEqual(usage and usage[0], {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})
        self.assertTrue(all(b.get("stream") is True for b in server.post_bodies))

    def test_usage_on_a_separate_chunk_survives_the_finish_merge(self) -> None:
        """Usage and finish_reason often arrive on different SSE chunks. Over-
        writing the accumulator with the finish chunk dropped the usage block,
        so the usage report saw nothing at all."""
        self._start(advertise=False, split_usage=True)
        provider = OpenAICompatProvider("test-key", self.base)
        usage: list[dict[str, Any]] = []
        provider.usage_sink = usage.append

        out, _seen = self._run(provider)

        self.assertEqual(out, "hello")
        self.assertEqual(usage and usage[0], {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})

    def test_a_server_that_rejects_streaming_falls_back_to_blocking(self) -> None:
        server = self._start(advertise=False, reject_stream=True)
        provider = OpenAICompatProvider("test-key", self.base)

        out, seen = self._run(provider)

        self.assertEqual(out, "ok")
        self.assertEqual(seen, [], "the blocking fallback produces no partial callbacks")
        self.assertEqual(len(server.post_bodies), 2, "streaming 400, then plain")
        self.assertFalse(server.post_bodies[1].get("stream"))
        self.assertIsNotNone(provider.on_delta, "the caller's callback must survive the fallback")


class _FakeOllamaServer(ThreadingHTTPServer):
    """NDJSON-streaming stand-in for /api/generate."""

    def __init__(self) -> None:
        self.post_bodies: list[dict[str, Any]] = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                server = cast(_FakeOllamaServer, self.server)
                length = int(self.headers.get("content-length", 0))
                server.post_bodies.append(json.loads(self.rfile.read(length)))
                if not server.post_bodies[-1].get("stream"):
                    body = json.dumps({"response": '{"files": []}', "prompt_eval_count": 9, "eval_count": 4}).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("content-type", "application/x-ndjson")
                self.end_headers()
                for piece, done in (('{"fi', False), ('les": []}', True)):
                    body = json.dumps(
                        {"response": piece, "done": done, "prompt_eval_count": 9, "eval_count": 4}
                        if done else {"response": piece, "done": False}
                    ).encode()
                    self.wfile.write(body + b"\n")
            def log_message(self, *args: Any) -> None:
                pass
        super().__init__(("127.0.0.1", 0), Handler)


class TestOllamaStreaming(unittest.TestCase):
    def _complete(
        self, attach_delta: bool,
    ) -> tuple[OllamaProvider, _FakeOllamaServer, list[str], str]:
        import asyncio
        from engine.providers import OllamaProvider
        server = _FakeOllamaServer()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        provider = OllamaProvider(f"http://127.0.0.1:{server.server_address[1]}")
        seen: list[str] = []
        if attach_delta:
            provider.on_delta = seen.append
        out = asyncio.run(provider.complete("sys", "user", "m1", 0.0, 64))
        return provider, server, seen, out

    def test_ndjson_stream_assembles_and_reports_usage(self) -> None:
        provider, server, seen, out = self._complete(True)
        self.assertEqual(out, '{"files": []}')
        self.assertEqual(seen, ['{"fi', '{"files": []}'])
        self.assertEqual(server.post_bodies[0].get("stream"), True)

    def test_without_on_delta_the_request_stays_blocking(self) -> None:
        provider, server, seen, out = self._complete(False)
        self.assertEqual(out, '{"files": []}')
        self.assertEqual(seen, [])
        self.assertIs(server.post_bodies[0].get("stream"), False)

    def test_a_dead_endpoint_raises_provider_unreachable_when_streaming(self) -> None:
        """A refused connection must not escape as a raw httpx exception.

        post_json normalizes transport failures for non-streaming calls, but the
        streaming branch called client.stream() bare: a dead endpoint raised raw
        ConnectError, which is neither a ProviderError nor a FALLBACK_TRIGGER,
        so it bypassed the fallback machinery, crashed the step gather, and
        surfaced as internal_error with role=None. The engine-side fix wraps the
        stream; this test pins the normalization on both providers' streaming
        paths (the ollama one here, the openai_compat one below).
        """
        import asyncio
        from engine.providers import OllamaProvider, ProviderError

        # Port 1 on loopback: nothing listens, connection refused immediately.
        provider = OllamaProvider("http://127.0.0.1:1")
        provider.on_delta = lambda _text: None  # force the streaming branch
        with self.assertRaises(ProviderError) as ctx:
            asyncio.run(provider.complete("sys", "user", "m1", 0.0, 64))
        self.assertEqual(ctx.exception.code, "provider_unreachable")

    def test_openai_compat_streaming_dead_endpoint_raises_provider_unreachable(self) -> None:
        """Same normalization for the openai_compat SSE stream."""
        import asyncio
        from engine.providers import OpenAICompatProvider, ProviderError

        provider = OpenAICompatProvider("test-key", "http://127.0.0.1:1/v1")
        provider.on_delta = lambda _text: None  # force the streaming branch
        with self.assertRaises(ProviderError) as ctx:
            asyncio.run(provider.complete("sys", "user", "m1", 0.0, 64))
        self.assertEqual(ctx.exception.code, "provider_unreachable")


class TestGoogleKeyHeader(unittest.TestCase):
    """The API key must travel in the x-goog-api-key header, never in the URL:
    a query-string credential lands in proxy and server access logs."""

    def test_key_travels_in_the_header_not_the_url(self) -> None:
        import asyncio
        from engine.providers import GoogleProvider
        seen: dict[str, Any] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", 0))
                self.rfile.read(length)
                seen["path"] = self.path
                seen["api_key_header"] = self.headers.get("x-goog-api-key")
                body = json.dumps(
                    {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:  # silence the test log
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        provider = GoogleProvider("secret-key", f"http://127.0.0.1:{server.server_address[1]}")
        out = asyncio.run(provider.complete("sys", "user", "gemini-pro", 0.0, 64))

        self.assertEqual(out, "hi")
        self.assertEqual(seen["api_key_header"], "secret-key")
        self.assertNotIn("key=", seen["path"], "the key must not ride in the URL query")



class TestOllamaNumCtx(unittest.TestCase):
    """`num_ctx` reaches the wire, and nothing else invents it.

    Ollama sizes its context window from its own default (4096) unless a
    request says otherwise, and *silently truncates* longer prompts — the
    librarian's evidence pack and the design contract simply do not fit, and
    the failure is a degraded plan rather than an error. The fix is a per-role
    `num_ctx` that has to survive the whole trip: config → call site → request
    body. Each test below is one leg of that trip.
    """

    def _make(self) -> tuple[OllamaProvider, Any]:
        import threading

        from engine.providers import OllamaProvider

        class _Server(ThreadingHTTPServer):
            post_bodies: list[dict[str, Any]] = []
            daemon_threads = True

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server's spelling
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                _Server.post_bodies.append(body)
                reply = json.dumps({"response": "{}", "done": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args: Any) -> None:
                pass

        server = _Server(("127.0.0.1", 0), Handler)
        # The Handler closes over the class attribute; reset it here rather
        # than shadowing with an instance attribute the reader would miss.
        _Server.post_bodies = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return OllamaProvider(f"http://127.0.0.1:{server.server_address[1]}"), server

    def test_num_ctx_reaches_the_generate_options_when_set(self) -> None:
        import asyncio

        provider, server = self._make()
        asyncio.run(provider.complete("sys", "user", "m1", 0.0, 64, num_ctx=32768))
        options = server.post_bodies[0]["options"]
        self.assertEqual(options.get("num_ctx"), 32768)

    def test_num_ctx_absent_keeps_ollamas_default(self) -> None:
        # None must *omit* the field, not send a guessed value: choosing a
        # window on the user's behalf is how a role with a small prompt
        # suddenly pays 32k of KV cache for nothing.
        import asyncio

        provider, server = self._make()
        asyncio.run(provider.complete("sys", "user", "m1", 0.0, 64))
        self.assertNotIn("num_ctx", server.post_bodies[0]["options"])

    def test_num_ctx_reaches_the_streaming_generate_too(self) -> None:
        import asyncio

        provider, server = self._make()
        provider.on_delta = lambda _text: None
        asyncio.run(provider.complete("sys", "user", "m1", 0.0, 64, num_ctx=8192))
        self.assertEqual(server.post_bodies[0]["options"].get("num_ctx"), 8192)

    def test_num_ctx_reaches_the_chat_options_for_tool_calls(self) -> None:
        import asyncio

        provider, server = self._make()
        asyncio.run(
            provider.complete_with_tools(
                "sys", [{"role": "user", "content": "hi"}], [], "m1", 0.0, 64, num_ctx=16384,
            )
        )
        self.assertEqual(server.post_bodies[0]["options"].get("num_ctx"), 16384)

    def test_other_providers_accept_and_ignore_num_ctx(self) -> None:
        # The keyword is on the base signature so callers can pass it
        # uniformly; a provider with no equivalent field must accept it and
        # say nothing on the wire.
        from engine.providers import OpenAICompatProvider

        provider = OpenAICompatProvider("k", "http://127.0.0.1:1")  # never contacted
        # Not awaited to completion — the point is the signature accepts it.
        coroutine = provider.complete("s", "u", "m", 0.0, 8, num_ctx=4096)
        coroutine.close()


class TestOllamaKeepAlive(TestOllamaNumCtx):
    """`keep_alive` reaches the wire as a *sibling* of `options`, not inside it.

    Inherits the local Ollama server from the `num_ctx` class rather than
    repeating it: the two fields travel the same trip and the server is the
    fixture, not the subject.

    The placement is the whole test. `keep_alive` is a top-level member of an
    Ollama request, not an entry in its `options` dict — and nesting it there is
    *accepted but ignored*, which is the worst possible failure: the setting
    saves, the UI shows it, and the model is reloaded exactly as before, with
    nothing anywhere reporting a problem.
    """

    def test_keep_alive_is_a_top_level_field_not_an_option(self) -> None:
        import asyncio

        provider, server = self._make()
        asyncio.run(provider.complete("sys", "user", "m1", 0.0, 64, keep_alive="30m"))
        body = server.post_bodies[0]
        self.assertEqual(body.get("keep_alive"), "30m")
        self.assertNotIn(
            "keep_alive", body["options"],
            "nested under options it is silently ignored, so this would look set",
        )

    def test_keep_alive_absent_means_the_field_is_omitted_entirely(self) -> None:
        # Omission, not a default. Sending "5m" on the user's behalf would make
        # a role they configured to unload promptly hold its model in memory,
        # and nothing in the UI would say the engine had overridden them.
        import asyncio

        provider, server = self._make()
        asyncio.run(provider.complete("sys", "user", "m1", 0.0, 64))
        self.assertNotIn("keep_alive", server.post_bodies[0])

    def test_keep_alive_reaches_the_streaming_generate_too(self) -> None:
        import asyncio

        provider, server = self._make()
        provider.on_delta = lambda _text: None
        asyncio.run(provider.complete("sys", "user", "m1", 0.0, 64, keep_alive="1h"))
        self.assertEqual(server.post_bodies[0].get("keep_alive"), "1h")

    def test_keep_alive_reaches_the_chat_path_for_tool_calls(self) -> None:
        import asyncio

        provider, server = self._make()
        asyncio.run(
            provider.complete_with_tools(
                "sys", [{"role": "user", "content": "hi"}], [], "m1", 0.0, 64,
                keep_alive="10m",
            )
        )
        self.assertEqual(server.post_bodies[0].get("keep_alive"), "10m")

    def test_both_fields_travel_together_and_keep_their_own_places(self) -> None:
        # The two are configured side by side in the UI, so a save that sets
        # both has to produce a body where neither has been flattened into the
        # other's slot.
        import asyncio

        provider, server = self._make()
        asyncio.run(
            provider.complete("sys", "user", "m1", 0.0, 64, num_ctx=32768, keep_alive="30m")
        )
        body = server.post_bodies[0]
        self.assertEqual(body["options"]["num_ctx"], 32768)
        self.assertEqual(body["keep_alive"], "30m")

    def test_other_providers_accept_and_ignore_keep_alive(self) -> None:
        from engine.providers import OpenAICompatProvider

        provider = OpenAICompatProvider("k", "http://127.0.0.1:1")  # never contacted
        coroutine = provider.complete("s", "u", "m", 0.0, 8, keep_alive="30m")
        coroutine.close()


if __name__ == "__main__":
    unittest.main()

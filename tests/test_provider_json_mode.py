from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from engine.providers import OpenAICompatProvider


def openai_reply(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class _FakeOpenAIServer(ThreadingHTTPServer):
    """Loopback stand-in for an OpenAI-compatible endpoint.

    Configurable on two axes the JSON-mode logic depends on: whether /v1/models
    advertises structured-output capability, and whether POSTs carrying
    response_format are accepted or rejected with a 400.
    """

    def __init__(self, advertise: bool, reject_json_mode: bool, reject_stream: bool = False):
        self.advertise = advertise
        self.reject_json_mode = reject_json_mode
        self.reject_stream = reject_stream
        self.post_bodies: list[dict] = []
        self.models_requests = 0
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.endswith("/models"):
                    self.server.models_requests += 1
                    data = {"data": [{"id": "m1"}]}
                    if self.server.advertise:
                        data["data"][0]["capabilities"] = {"json_object": True}
                    body = json.dumps(data).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                self.server.post_bodies.append(json.loads(self.rfile.read(length)))
                if self.server.post_bodies[-1].get("stream"):
                    if self.server.reject_stream:
                        self.send_error(400, "streaming is not supported")
                        return
                    # SSE: two content chunks, then usage on the final chunk.
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    for chunk in (
                        {"choices": [{"delta": {"content": "hel"}}]},
                        {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
                    ):
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                if self.server.reject_json_mode and "response_format" in self.server.post_bodies[-1]:
                    self.send_error(400, "response_format is not supported")
                    return
                body = json.dumps(openai_reply("ok")).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # silence the test log
                pass

        super().__init__(("127.0.0.1", 0), Handler)


class _ServerMixin(unittest.TestCase):
    def _start(self, advertise: bool, reject_json_mode: bool = False, reject_stream: bool = False) -> _FakeOpenAIServer:
        server = _FakeOpenAIServer(advertise, reject_json_mode, reject_stream)
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

    def test_a_capable_server_gets_response_format_and_the_probe_is_cached(self):
        server = self._start(advertise=True)
        provider = self._provider(self.base)

        out = self._complete(provider)
        out2 = self._complete(provider)

        self.assertEqual(out, "ok")
        self.assertEqual(server.models_requests, 1, "the capability probe must run once, not per call")
        self.assertTrue(all("response_format" in b for b in server.post_bodies))
        self.assertEqual(server.post_bodies[0]["response_format"], {"type": "json_object"})

    def test_a_server_without_the_capability_is_never_sent_the_field(self):
        server = self._start(advertise=False)
        provider = self._provider(self.base)

        out = self._complete(provider)

        self.assertEqual(out, "ok")
        self.assertTrue(server.post_bodies)
        self.assertFalse(any("response_format" in b for b in server.post_bodies))

    def test_a_server_that_rejects_the_field_is_retried_without_it(self):
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


class TestOpenAIStreaming(_ServerMixin):
    """With on_delta attached the provider streams SSE and assembles the text."""

    def _run(self, provider: OpenAICompatProvider):
        import asyncio
        seen: list[str] = []
        provider.on_delta = seen.append
        out = asyncio.run(provider.complete("sys", "user", "m1", 0.0, 64))
        return out, seen

    def test_streamed_text_assembles_in_order_and_reports_usage(self):
        server = self._start(advertise=False)
        provider = OpenAICompatProvider("test-key", self.base)
        usage: list[dict] = []
        provider.usage_sink = usage.append

        out, seen = self._run(provider)

        self.assertEqual(out, "hello")
        self.assertEqual(seen, ["hel", "hello"], "each callback carries the full text so far")
        self.assertEqual(usage and usage[0], {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7})
        self.assertTrue(all(b.get("stream") is True for b in server.post_bodies))

    def test_a_server_that_rejects_streaming_falls_back_to_blocking(self):
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

    def __init__(self):
        self.post_bodies: list[dict] = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                self.server.post_bodies.append(json.loads(self.rfile.read(length)))
                if not self.server.post_bodies[-1].get("stream"):
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
            def log_message(self, *args):
                pass
        super().__init__(("127.0.0.1", 0), Handler)


class TestOllamaStreaming(unittest.TestCase):
    def _complete(self, attach_delta: bool):
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

    def test_ndjson_stream_assembles_and_reports_usage(self):
        provider, server, seen, out = self._complete(True)
        self.assertEqual(out, '{"files": []}')
        self.assertEqual(seen, ['{"fi', '{"files": []}'])
        self.assertEqual(server.post_bodies[0].get("stream"), True)

    def test_without_on_delta_the_request_stays_blocking(self):
        provider, server, seen, out = self._complete(False)
        self.assertEqual(out, '{"files": []}')
        self.assertEqual(seen, [])
        self.assertIs(server.post_bodies[0].get("stream"), False)



if __name__ == "__main__":
    unittest.main()

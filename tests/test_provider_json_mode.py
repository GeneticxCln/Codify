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

    def __init__(self, advertise: bool, reject_json_mode: bool):
        self.advertise = advertise
        self.reject_json_mode = reject_json_mode
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
    def _start(self, advertise: bool, reject_json_mode: bool = False) -> _FakeOpenAIServer:
        server = _FakeOpenAIServer(advertise, reject_json_mode)
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


if __name__ == "__main__":
    unittest.main()

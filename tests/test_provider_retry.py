"""A provider's transient failure is retried, and its reason is not thrown away (audit of 2026-09-29, H5).

Any HTTP status of 400 or more became a terminal `provider_http` reading `"<label> <status>"`. A 429 or a
529 ("overloaded") failed the step, and with one provider configured, the goal. And the response body — the
only place a provider says *why*: "credit balance is too low", "model not found", "context length exceeded" —
was dropped, so a person saw `anthropic 400` and nothing to act on.

The rules, each pinned below:

* **What is retried**: 408, 425, 429 and the 5xx family (500, 502, 503, 504, 529), and a connection that was
  refused or reset before any answer. Never a 400/401/403/404/422 — asking again cannot change those — and
  never a read *timeout*, because the server may still be generating and a second request doubles the cost.
* **How**: at most three attempts, exponential backoff with jitter, and `Retry-After` (seconds or an HTTP date)
  honoured — up to a cap; a provider that asks for longer than the cap is reported, not waited for.
* **Streams**: retried until the first byte of the body. After that the deltas are already on a person's
  screen, so a reset is reported as an interruption (with how much arrived) and never silently re-run.
* **The reason**: the provider's own message, in the error, with anything that looks like a credential removed.

The transport is a `httpx.MockTransport` (the code under test takes the client), plus real sockets where the
behaviour *is* the socket: a connection reset mid-stream.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

import httpx

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.providers import (
    OpenAICompatProvider,
    ProviderError,
    error_detail,
    open_stream,
    parse_retry_after,
    post_json,
)

OK = {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


class Waits:
    """Stand-in for the backoff sleep: records what it was asked to wait, and waits for nothing."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class RetryCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.waits = Waits()
        for target, value in (("engine.providers._sleep", self.waits), ("engine.providers._jitter", lambda: 1.0)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def client(self, script: list[Any]) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
        """A client whose transport plays `script`: a `Response`, or an exception to raise, per request."""
        seen: list[httpx.Request] = []
        steps = list(script)

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            step = steps.pop(0) if len(steps) > 1 else steps[0]
            if isinstance(step, Exception):
                raise step
            assert isinstance(step, httpx.Response)
            return step

        return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen

    async def post(self, script: list[Any]) -> tuple[Any, list[httpx.Request]]:
        client, seen = self.client(script)
        async with client:
            data = await post_json(client, "http://provider.test/v1/chat", label="openai_compat", json={"x": 1})
        return data, seen

    async def refused(self, script: list[Any]) -> tuple[ProviderError, list[httpx.Request]]:
        client, seen = self.client(script)
        async with client:
            with self.assertRaises(ProviderError) as caught:
                await post_json(client, "http://provider.test/v1/chat", label="openai_compat", json={"x": 1})
        return caught.exception, seen


def response(status: int, body: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
    content = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    return httpx.Response(status, content=content or b"", headers=headers or {})


class TestWhatIsRetried(RetryCase):
    async def test_a_429_that_names_a_delay_is_retried_after_exactly_that_delay(self) -> None:
        data, seen = await self.post([response(429, {"error": "slow down"}, {"Retry-After": "2"}), response(200, OK)])

        self.assertEqual(OK, data)
        self.assertEqual(2, len(seen))
        self.assertEqual([2.0], self.waits.delays)

    async def test_a_529_overloaded_is_retried_on_the_backoff_schedule(self) -> None:
        data, seen = await self.post([response(529, {"type": "error", "error": {"message": "Overloaded"}}), response(200, OK)])

        self.assertEqual(OK, data)
        self.assertEqual(2, len(seen))
        self.assertEqual([1.0], self.waits.delays)

    async def test_every_transient_status_is_retried_once_and_then_succeeds(self) -> None:
        for status in (408, 425, 429, 500, 502, 503, 504, 529):
            with self.subTest(status=status):
                self.waits.delays.clear()
                data, seen = await self.post([response(status, {"error": "x"}), response(200, OK)])
                self.assertEqual(OK, data)
                self.assertEqual(2, len(seen), f"{status} was not retried")

    async def test_a_connection_refused_or_reset_before_any_answer_is_retried(self) -> None:
        for failure in (
            httpx.ConnectError("refused"), httpx.ConnectTimeout("no route"),
            httpx.RemoteProtocolError("server disconnected"), httpx.ReadError("reset by peer"),
        ):
            with self.subTest(failure=type(failure).__name__):
                data, seen = await self.post([failure, response(200, OK)])
                self.assertEqual(OK, data)
                self.assertEqual(2, len(seen))

    async def test_backoff_doubles_and_gives_up_after_three_attempts(self) -> None:
        error, seen = await self.refused([response(503, {"error": "down"})])

        self.assertEqual(3, len(seen), "three attempts: the first and two retries")
        self.assertEqual([1.0, 2.0], self.waits.delays)
        self.assertEqual("provider_http", error.code)
        self.assertIn("503", error.message)
        self.assertIn("after 3 attempts", error.message)


class TestWhatIsNeverRetried(RetryCase):
    async def test_a_client_error_is_final_on_the_first_answer(self) -> None:
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                error, seen = await self.refused([response(status, {"error": {"message": "no"}})])
                self.assertEqual(1, len(seen), f"{status} was retried")
                self.assertEqual([], self.waits.delays)
                self.assertEqual("provider_http", error.code)

    async def test_a_read_timeout_is_not_retried_because_the_server_may_still_be_generating(self) -> None:
        error, seen = await self.refused([httpx.ReadTimeout("no bytes for 120 s")])

        self.assertEqual(1, len(seen))
        self.assertEqual("provider_unreachable", error.code)

    async def test_a_retry_after_beyond_the_cap_is_reported_not_waited_for(self) -> None:
        error, seen = await self.refused([response(429, {"error": "quota"}, {"Retry-After": "900"})])

        self.assertEqual(1, len(seen))
        self.assertEqual([], self.waits.delays)
        self.assertIn("900", error.message, "the wait the provider asked for should be in the message")


class TestRetryAfterFormats(unittest.TestCase):
    def test_seconds(self) -> None:
        self.assertEqual(7.0, parse_retry_after("7"))
        self.assertEqual(0.0, parse_retry_after("0"))
        self.assertEqual(1.5, parse_retry_after("1.5"))

    def test_an_http_date_is_the_time_until_then(self) -> None:
        now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
        later = format_datetime(now + timedelta(seconds=12), usegmt=True)

        self.assertEqual(12.0, parse_retry_after(later, now=now))

    def test_a_date_in_the_past_is_no_wait_at_all(self) -> None:
        now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(0.0, parse_retry_after(format_datetime(now - timedelta(seconds=30), usegmt=True), now=now))

    def test_nonsense_is_no_information(self) -> None:
        for value in (None, "", "soon", "-5", "NaN", "inf"):
            with self.subTest(value=value):
                self.assertIsNone(parse_retry_after(value))


class TestTheReasonIsKept(RetryCase):
    async def test_each_providers_own_wording_reaches_the_message(self) -> None:
        cases = {
            "anthropic": ({"type": "error", "error": {"type": "invalid_request_error",
                                                       "message": "Your credit balance is too low to access the API."}},
                          "credit balance is too low"),
            "openai": ({"error": {"message": "The model `gpt-9` does not exist", "type": "invalid_request_error"}},
                       "does not exist"),
            "google": ({"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}},
                       "API key not valid"),
            "ollama": ({"error": "model 'qwen3:8b' not found, try pulling it first"}, "not found"),
            "plain text": (b"upstream connect error or disconnect/reset before headers", "upstream connect error"),
        }
        for name, (body, needle) in cases.items():
            with self.subTest(provider=name):
                error, _ = await self.refused([response(400, body)])
                self.assertIn("400", error.message)
                self.assertIn(needle, error.message)

    async def test_a_credential_in_the_body_is_removed_before_it_reaches_an_error(self) -> None:
        body = {"error": {"message": "Incorrect API key provided: sk-proj-AbCdEfGhIjKlMnOpQrStUv123456. "
                                     "Authorization: Bearer sk-ant-api03-ZZZZZZZZZZZZZZZZZZZZ; key=AIzaSyA1234567890abcdefghijklmnop"}}

        error, _ = await self.refused([response(401, body)])

        for secret in ("sk-proj-AbCdEfGhIjKlMnOpQrStUv123456", "sk-ant-api03-ZZZZZZZZZZZZZZZZZZZZ", "AIzaSyA1234567890abcdefghijklmnop"):
            self.assertNotIn(secret, error.message)
        self.assertIn("Incorrect API key provided", error.message)

    async def test_the_key_this_request_carried_is_removed_even_when_it_has_no_recognisable_shape(self) -> None:
        # A local server's key looks like nothing in particular; the only way to know it is to know
        # which credential the request itself carried.
        key = "local-server-key-7f3a9c"
        client, _ = self.client([response(401, {"error": f"invalid key {key}"})])
        async with client:
            with self.assertRaises(ProviderError) as caught:
                await post_json(client, "http://provider.test/v1/chat", label="openai_compat",
                                headers={"Authorization": f"Bearer {key}"}, json={})

        self.assertNotIn(key, caught.exception.message)
        self.assertIn("invalid key", caught.exception.message)

    async def test_a_long_or_hostile_body_is_bounded_and_single_line(self) -> None:
        error, _ = await self.refused([response(500, b"x" * 50_000 + b"\nsecond line\n")])

        self.assertLess(len(error.message), 600)
        self.assertNotIn("\n", error.message)

    def test_error_detail_prefers_the_message_field_and_falls_back_to_the_text(self) -> None:
        self.assertEqual("nope", error_detail(json.dumps({"error": {"message": "nope"}})))
        self.assertEqual("nope", error_detail(json.dumps({"message": "nope"})))
        self.assertEqual("nope", error_detail(json.dumps({"detail": "nope"})))
        self.assertEqual("plain words", error_detail("plain words"))
        self.assertEqual("", error_detail(""))


class TestStreams(RetryCase):
    async def open(self, script: list[Any]) -> tuple[list[bytes], list[httpx.Request]]:
        client, seen = self.client(script)
        body: list[bytes] = []
        async with client:
            async with open_stream(client, "http://provider.test/v1/chat", label="openai_compat", json={"stream": True}) as reply:
                async for chunk in reply.aiter_bytes():
                    body.append(chunk)
        return body, seen

    async def test_a_429_before_the_first_byte_is_retried_and_the_stream_then_flows(self) -> None:
        body, seen = await self.open([response(429, {"error": "busy"}, {"Retry-After": "1"}), response(200, b"data: one\n\n")])

        self.assertEqual(b"data: one\n\n", b"".join(body))
        self.assertEqual(2, len(seen))
        self.assertEqual([1.0], self.waits.delays)

    async def test_a_400_on_a_stream_carries_the_providers_reason(self) -> None:
        client, seen = self.client([response(400, {"error": {"message": "context length exceeded"}})])
        async with client:
            with self.assertRaises(ProviderError) as caught:
                async with open_stream(client, "http://provider.test/v1/chat", label="openai_compat", json={}):
                    self.fail("a refused stream must not be entered")

        self.assertEqual(1, len(seen))
        self.assertIn("context length exceeded", caught.exception.message)

    async def test_a_connection_refused_before_the_first_byte_is_retried(self) -> None:
        body, seen = await self.open([httpx.ConnectError("refused"), response(200, b"data: ok\n\n")])

        self.assertEqual(b"data: ok\n\n", b"".join(body))
        self.assertEqual(2, len(seen))


class TestTheLivenessProbeDoesNotWait(RetryCase):
    async def test_a_429_is_reported_at_once_not_retried(self) -> None:
        steps: list[tuple[Any, ...]] = [
            ("json", 429, {"error": {"message": "rate limited"}}, {"Retry-After": "2"}), ("json", 200, OK, {}),
        ]
        with ScriptedServer(steps) as server:
            provider = OpenAICompatProvider("sk-test", server.url)

            ok, message = await provider.test_connection("m")

            self.assertFalse(ok)
            self.assertIn("429", message)
            self.assertIn("rate limited", message)
            self.assertEqual(1, server.requests, "the probe retried; the button would sit for the whole backoff")
        self.assertEqual([], self.waits.delays)

    async def test_the_attempt_limit_does_not_leak_out_of_the_probe(self) -> None:
        steps: list[tuple[Any, ...]] = [("json", 503, {"error": "down"}, {})]
        with ScriptedServer(steps) as server:
            provider = OpenAICompatProvider("sk-test", server.url)

            await provider.test_connection("m")
            self.waits.delays.clear()
            server.requests = 0
            with self.assertRaises(ProviderError):
                await provider.complete("s", "u", "m", 0.0, 8)

            self.assertEqual(3, server.requests, "a real call after a probe lost its retries")


class ScriptedServer:
    """A real HTTP server on loopback whose answers are a script: the point of it is the socket.

    A `Step` is `("json", status, body, headers)`, or `("stream_then_reset", chunks)` — send some SSE
    chunks and then reset the connection, which is what a crashed backend or a dropped link looks like.
    """

    def __init__(self, steps: list[tuple[Any, ...]]) -> None:
        self.steps = list(steps)
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server's own naming
                outer.requests += 1
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                step = outer.steps.pop(0) if len(outer.steps) > 1 else outer.steps[0]
                if step[0] == "json":
                    _, status, body, headers = step
                    payload = json.dumps(body).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if step[0] == "stream_then_reset":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for chunk in step[1]:
                        data = chunk.encode()
                        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                        self.wfile.flush()
                    # SO_LINGER 0 turns the close into a RST: the peer sees a reset, not a clean end.
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    self.connection.close()

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> ScriptedServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


class TestThroughARealProviderOverRealSockets(RetryCase):
    async def test_a_429_then_a_real_reply_completes_the_call(self) -> None:
        steps: list[tuple[Any, ...]] = [
            ("json", 429, {"error": {"message": "rate limited"}}, {"Retry-After": "1"}), ("json", 200, OK, {}),
        ]
        with ScriptedServer(steps) as server:
            provider = OpenAICompatProvider("sk-test", server.url)

            said = await provider.complete("s", "u", "m", 0.0, 8)

            self.assertEqual("hi", said)
            self.assertEqual(2, server.requests)
        self.assertEqual([1.0], self.waits.delays)

    async def test_a_400_names_its_reason_through_the_whole_provider(self) -> None:
        steps: list[tuple[Any, ...]] = [
            ("json", 400, {"error": {"message": "This model's maximum context length is 4096 tokens"}}, {}),
        ]
        with ScriptedServer(steps) as server:
            provider = OpenAICompatProvider("sk-test", server.url)

            with self.assertRaises(ProviderError) as caught:
                await provider.complete("s", "u", "m", 0.0, 8)

            self.assertEqual(1, server.requests)
        self.assertIn("maximum context length", caught.exception.message)

    async def test_a_connection_reset_mid_stream_is_an_interruption_and_is_never_re_run(self) -> None:
        chunk = 'data: {"choices": [{"delta": {"content": "Hel"}}]}\n\n'
        steps: list[tuple[Any, ...]] = [("stream_then_reset", [chunk, chunk])]
        with ScriptedServer(steps) as server:
            provider = OpenAICompatProvider("sk-test", server.url)
            shown: list[str] = []
            provider.on_delta = shown.append

            with self.assertRaises(ProviderError) as caught:
                await provider.complete("s", "u", "m", 0.0, 8)

            self.assertEqual(1, server.requests, "a stream that had begun was silently run again")
        self.assertEqual("provider_unreachable", caught.exception.code)
        self.assertIn("interrupted", caught.exception.message)
        self.assertIn("6 characters", caught.exception.message, "the message should say how much had arrived")
        self.assertEqual([], self.waits.delays)
        self.assertTrue(shown, "the deltas that did arrive were not shown")


if __name__ == "__main__":
    unittest.main()

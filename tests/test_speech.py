"""Voice: dictation and read-aloud through a speech provider, and the microphone recorded by the engine.

PipeWire is not on a test machine, so `pw-record` and `pw-dump` are stand-ins on `PATH`: small scripts that
behave the way the real tools do where it matters (a recording that is finished by SIGTERM, a dump that is
JSON). The provider is an `httpx.MockTransport` that keeps every request it is sent, so a test can say what
went over the wire, and that nothing did.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import asyncio
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
from httpx import ASGITransport

from engine import home, speech
from engine.app import BOOT_TOKEN, app
from engine.db import connect
from engine.executor import ExecutorService
from engine.providers import Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import AgentRegistryService, ApiError, GoalService, SettingsService, WorkspaceService

PW_RECORD = r'''#!/usr/bin/env python3
"""A stand-in for pw-record: writes a WAV when it is told to stop, as the real one does."""
import os, signal, struct, sys, time
path = sys.argv[-1]
log = os.environ.get("STUB_PW_LOG")
if log:
    with open(log, "a") as f:
        f.write(" ".join(sys.argv[1:-1]) + "\n")
if os.environ.get("STUB_PW_RECORD_FAIL"):
    print("pw-record: no such target node", file=sys.stderr)
    sys.exit(1)
pcm = b"" if os.environ.get("STUB_PW_RECORD_SILENT") else b"\x01\x00" * 1600
def finish(*_):
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
                + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
                + b"data" + struct.pack("<I", len(pcm)) + pcm)
    sys.exit(0)
signal.signal(signal.SIGTERM, finish)
signal.signal(signal.SIGINT, finish)
open(path, "wb").close()
while True:
    time.sleep(0.05)
'''

PW_DUMP = r'''#!/usr/bin/env python3
import json, os, sys
if os.environ.get("STUB_PW_DUMP_FAIL"):
    print("pw-dump: failed to connect: Host is down", file=sys.stderr)
    sys.exit(1)
print(json.dumps([
    {"type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
     "metadata": [{"subject": 0, "key": "default.audio.source", "value": {"name": "usb.mic"}}]},
    {"type": "PipeWire:Interface:Node", "info": {"props": {
        "media.class": "Audio/Source", "node.name": "usb.mic", "node.description": "USB Microphone"}}},
    {"type": "PipeWire:Interface:Node", "info": {"props": {
        "media.class": "Audio/Source", "node.name": "builtin.mic", "node.nick": "Built-in"}}},
    {"type": "PipeWire:Interface:Node", "info": {"props": {
        "media.class": "Audio/Sink", "node.name": "speakers", "node.description": "Speakers"}}},
]))
'''

KEY = "sk-speech-test-key-never-echoed"


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class SpeechCase(unittest.IsolatedAsyncioTestCase):
    """The app over a throwaway store, with stand-in PipeWire tools and a recording provider."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "test.db")
        self.keychain = Keychain(secrets_path=self.root / "secrets.json")
        app.state.conn = self.conn
        app.state.keychain = self.keychain
        app.state.registry = AgentRegistryService(self.conn, ProviderFactory(self.keychain), self.keychain)
        app.state.workspaces = WorkspaceService(self.conn)
        app.state.goals = GoalService(self.conn)
        app.state.sandbox = SandboxService()
        app.state.settings = SettingsService(self.conn)
        app.state.executor = ExecutorService(
            app.state.goals, app.state.workspaces, app.state.registry, app.state.sandbox
        )
        app.state.token = BOOT_TOKEN
        self.recorder = speech.Recorder()
        app.state.recorder = self.recorder

        # What the provider was sent, and what it answers.
        self.sent: list[httpx.Request] = []
        self.reply: httpx.Response = httpx.Response(200, json={"text": "hello world"})

        def provider(request: httpx.Request) -> httpx.Response:
            self.sent.append(request)
            return self.reply

        app.state.speech_transport = httpx.MockTransport(provider)

        self.bin = self.root / "bin"
        self.bin.mkdir()
        _executable(self.bin / "pw-record", PW_RECORD)
        _executable(self.bin / "pw-dump", PW_DUMP)
        self.log = self.root / "pw.log"
        env = mock.patch.dict(os.environ, {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "STUB_PW_LOG": str(self.log),
        })
        env.start()
        self.addCleanup(env.stop)

        self.client = httpx.AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        )
        self.headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}

    async def asyncTearDown(self) -> None:
        self.recorder.discard()
        app.state.speech_transport = None
        await self.client.aclose()
        self.conn.close()
        self.temp_dir.cleanup()

    async def configure(self, **settings: Any) -> None:
        r = await self.client.put("/settings/engine", headers=self.headers, json=settings)
        self.assertEqual(200, r.status_code, r.text)

    async def configure_dictation(self, **extra: Any) -> None:
        self.keychain.set_provider_key("openai", KEY)
        await self.configure(stt_provider="openai", stt_model="whisper-1", **extra)

    def recordings(self) -> list[str]:
        folder = home.codify_home() / "dictation"
        return sorted(p.name for p in folder.glob("*")) if folder.exists() else []


class TestVoiceSettings(SpeechCase):
    async def test_the_voice_settings_round_trip_through_the_engine_settings_route(self) -> None:
        await self.configure(
            stt_provider="groq", stt_model="whisper-large-v3", stt_language="en",
            tts_provider="openai", tts_model="tts-1", tts_voice="alloy",
            audio_input="usb.mic", tts_auto_read=True,
        )

        got = (await self.client.get("/settings/engine", headers=self.headers)).json()

        self.assertEqual("groq", got["stt_provider"]["value"])
        self.assertEqual("whisper-large-v3", got["stt_model"]["value"])
        self.assertEqual("alloy", got["tts_voice"]["value"])
        self.assertEqual("usb.mic", got["audio_input"]["value"])
        self.assertEqual(1, got["tts_auto_read"]["value"], "a checkbox's true is a switch's 1")

    async def test_a_speech_provider_must_be_a_provider_slug(self) -> None:
        for key in ("stt_provider", "tts_provider"):
            with self.subTest(key=key):
                r = await self.client.put("/settings/engine", headers=self.headers, json={key: "Not a slug"})
                self.assertEqual(422, r.status_code, r.text)

    async def test_a_speech_server_address_is_an_http_url_or_nothing(self) -> None:
        for key in ("stt_base_url", "tts_base_url"):
            for bad in ("ftp://127.0.0.1/v1", "127.0.0.1:8000", "http://", "file:///etc/passwd"):
                with self.subTest(key=key, value=bad):
                    r = await self.client.put("/settings/engine", headers=self.headers, json={key: bad})
                    self.assertEqual(422, r.status_code, r.text)
            with self.subTest(key=key, value="a server"):
                await self.configure(**{key: " http://127.0.0.1:8000/v1 "})
                got = (await self.client.get("/settings/engine", headers=self.headers)).json()
                self.assertEqual("http://127.0.0.1:8000/v1", got[key]["value"])
                await self.configure(**{key: ""})


class TestResolution(SpeechCase):
    def resolve(self, provider: str, model: str = "m", base_url: str = "") -> speech.SpeechTarget:
        return speech.resolve(app.state.registry, self.keychain, provider, model, what="stt", base_url=base_url)

    def refusal(self, provider: str, model: str = "m", base_url: str = "") -> ApiError:
        with self.assertRaises(ApiError) as caught:
            self.resolve(provider, model, base_url)
        return caught.exception

    def test_a_built_in_provider_brings_its_address_and_its_key(self) -> None:
        self.keychain.set_provider_key("openai", KEY)

        target = self.resolve("openai", "whisper-1")

        self.assertEqual("https://api.openai.com/v1", target.base_url)
        self.assertEqual(KEY, target.api_key)

    def test_a_built_in_provider_that_needs_a_key_and_has_none_says_so(self) -> None:
        self.assertEqual("speech_key_missing", self.refusal("openai").code)

    def test_nothing_chosen_is_not_configured(self) -> None:
        self.assertEqual("stt_not_configured", self.refusal("", "").code)
        self.assertEqual("stt_not_configured", self.refusal("openai", "").code)

    def test_a_custom_provider_borrows_the_address_of_the_role_that_defines_it(self) -> None:
        self.conn.execute(
            "UPDATE agent_configs SET provider = 'localspeech', protocol = 'openai_compat', "
            "base_url = 'http://127.0.0.1:8000/v1' WHERE role = 'scribe'"
        )
        self.conn.commit()

        target = self.resolve("localspeech", "Systran/faster-whisper-small")

        self.assertEqual("http://127.0.0.1:8000/v1", target.base_url)
        self.assertEqual("", target.api_key, "a local speech server needs no key")

    def test_a_custom_provider_no_role_defines_is_refused(self) -> None:
        self.assertEqual("speech_provider_unknown", self.refusal("nowhere").code)

    def test_a_custom_provider_can_be_given_its_own_address_with_no_role_using_it(self) -> None:
        # The fully local case: a speech server on this machine that no agent role has any business with.
        target = self.resolve("localspeech", "Systran/faster-whisper-small", base_url="http://127.0.0.1:8000/v1/")

        self.assertEqual("http://127.0.0.1:8000/v1", target.base_url)
        self.assertEqual("", target.api_key)

    def test_its_own_address_wins_over_a_role_that_also_names_it(self) -> None:
        self.conn.execute(
            "UPDATE agent_configs SET provider = 'localspeech', protocol = 'openai_compat', "
            "base_url = 'http://127.0.0.1:9000/v1' WHERE role = 'scribe'"
        )
        self.conn.commit()

        target = self.resolve("localspeech", base_url="http://127.0.0.1:8000/v1")

        self.assertEqual("http://127.0.0.1:8000/v1", target.base_url)

    def test_a_built_in_provider_keeps_its_own_address_whatever_is_typed(self) -> None:
        # Otherwise a stray address beside "openai" would carry the OpenAI key somewhere else.
        self.keychain.set_provider_key("openai", KEY)

        target = self.resolve("openai", "whisper-1", base_url="https://elsewhere.example/v1")

        self.assertEqual("https://api.openai.com/v1", target.base_url)

    def test_a_key_saved_for_a_custom_speech_server_never_goes_to_it_in_the_clear(self) -> None:
        self.keychain.set_provider_key("lanspeech", KEY)

        self.assertEqual("invalid_base_url", self.refusal("lanspeech", base_url="http://192.168.1.5:8000/v1").code)

    def test_a_provider_that_does_not_speak_the_openai_audio_api_is_refused(self) -> None:
        self.keychain.set_provider_key("anthropic", KEY)
        for provider in ("ollama", "anthropic"):
            with self.subTest(provider=provider):
                self.assertEqual("speech_protocol_unsupported", self.refusal(provider).code)

    def test_a_stored_key_never_meets_a_plain_http_lan_address(self) -> None:
        # However the two got together (a hand-edited row, an older build), the key is not sent.
        self.conn.execute(
            "UPDATE agent_configs SET provider = 'lanspeech', protocol = 'openai_compat', "
            "base_url = 'http://192.168.1.5:8000/v1' WHERE role = 'scribe'"
        )
        self.conn.commit()
        self.keychain.set_provider_key("lanspeech", KEY)

        self.assertEqual("invalid_base_url", self.refusal("lanspeech").code)


class TestReadAloud(SpeechCase):
    async def configure_read_aloud(self) -> None:
        self.keychain.set_provider_key("openai", KEY)
        await self.configure(tts_provider="openai", tts_model="tts-1", tts_voice="alloy")

    async def test_speak_sends_the_text_and_answers_with_the_audio(self) -> None:
        await self.configure_read_aloud()
        self.reply = httpx.Response(200, content=b"RIFF....WAVEfmt fake-audio")

        r = await self.client.post("/audio/speak", headers=self.headers, json={"text": "  Hello there.  "})

        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("audio/wav", r.headers["content-type"])
        self.assertEqual(b"RIFF....WAVEfmt fake-audio", r.content)
        (request,) = self.sent
        self.assertEqual("https://api.openai.com/v1/audio/speech", str(request.url))
        self.assertEqual(f"Bearer {KEY}", request.headers["authorization"])
        self.assertEqual(
            {"model": "tts-1", "voice": "alloy", "input": "Hello there.", "response_format": "wav"},
            json.loads(request.content),
        )
        self.assertNotIn(KEY.encode(), r.content)
        self.assertNotIn(KEY, " ".join(r.headers.values()))

    async def test_a_keyless_local_speech_server_is_sent_no_authorization_header(self) -> None:
        # Some local servers refuse a header they did not ask for, and an empty bearer token is one.
        self.conn.execute(
            "UPDATE agent_configs SET provider = 'localspeech', protocol = 'openai_compat', "
            "base_url = 'http://127.0.0.1:8000/v1' WHERE role = 'scribe'"
        )
        self.conn.commit()
        await self.configure(tts_provider="localspeech", tts_model="kokoro", tts_voice="af_heart")
        self.reply = httpx.Response(200, content=b"RIFF-local")

        r = await self.client.post("/audio/speak", headers=self.headers, json={"text": "hi"})

        self.assertEqual(200, r.status_code, r.text)
        (request,) = self.sent
        self.assertEqual("http://127.0.0.1:8000/v1/audio/speech", str(request.url))
        self.assertNotIn("authorization", request.headers)

    async def test_a_local_speech_server_set_up_in_settings_alone_reads_aloud(self) -> None:
        await self.configure(
            tts_provider="localspeech", tts_model="kokoro", tts_voice="af_heart",
            tts_base_url="http://127.0.0.1:8000/v1",
        )
        self.reply = httpx.Response(200, content=b"RIFF-local")

        status = (await self.client.get("/audio/status", headers=self.headers)).json()
        r = await self.client.post("/audio/speak", headers=self.headers, json={"text": "hi"})

        self.assertTrue(status["read_aloud"]["configured"], status["read_aloud"])
        self.assertEqual(200, r.status_code, r.text)
        (request,) = self.sent
        self.assertEqual("http://127.0.0.1:8000/v1/audio/speech", str(request.url))
        self.assertNotIn("authorization", request.headers)

    async def test_speak_refuses_what_it_cannot_send(self) -> None:
        await self.configure_read_aloud()
        for text, code in (("   ", "text_empty"), ("x" * (speech.MAX_SPEAK_CHARS + 1), "text_too_long")):
            with self.subTest(code=code):
                r = await self.client.post("/audio/speak", headers=self.headers, json={"text": text})
                self.assertEqual(400, r.status_code, r.text)
                self.assertEqual(code, r.json()["code"])
        self.assertEqual([], self.sent)

    async def test_speak_without_a_voice_or_a_provider_is_not_configured(self) -> None:
        r = await self.client.post("/audio/speak", headers=self.headers, json={"text": "hi"})
        self.assertEqual(409, r.status_code)
        self.assertEqual("tts_not_configured", r.json()["code"])

        self.keychain.set_provider_key("openai", KEY)
        await self.configure(tts_provider="openai", tts_model="tts-1")
        r = await self.client.post("/audio/speak", headers=self.headers, json={"text": "hi"})
        self.assertEqual(409, r.status_code)
        self.assertEqual("tts_not_configured", r.json()["code"])
        self.assertEqual([], self.sent)

    async def test_a_provider_refusal_is_a_502_with_its_reason_and_never_the_key(self) -> None:
        await self.configure_read_aloud()
        self.reply = httpx.Response(401, json={"error": {"message": f"Incorrect API key provided: {KEY}"}})

        r = await self.client.post("/audio/speak", headers=self.headers, json={"text": "hi"})

        self.assertEqual(502, r.status_code, r.text)
        self.assertEqual("provider_http", r.json()["code"])
        self.assertIn("Incorrect API key", r.json()["message"])
        self.assertNotIn(KEY, r.text)


class TestDictation(SpeechCase):
    async def test_unconfigured_dictation_refuses_before_anything_is_spawned(self) -> None:
        with mock.patch("engine.speech.subprocess.Popen", side_effect=AssertionError("a process was started")):
            r = await self.client.post("/audio/dictation/start", headers=self.headers)

        self.assertEqual(409, r.status_code, r.text)
        self.assertEqual("stt_not_configured", r.json()["code"])
        self.assertFalse(self.log.exists(), "pw-record ran for a dictation that had nowhere to go")

    async def test_without_pipewire_the_recorder_says_what_to_install(self) -> None:
        await self.configure_dictation()
        with mock.patch.dict(os.environ, {"PATH": str(self.root / "empty")}):
            r = await self.client.post("/audio/dictation/start", headers=self.headers)
            status = (await self.client.get("/audio/status", headers=self.headers)).json()

        self.assertEqual(503, r.status_code, r.text)
        self.assertEqual("recorder_unavailable", r.json()["code"])
        self.assertIn("pipewire", r.json()["message"])
        self.assertFalse(status["recorder"]["available"])

    async def test_a_dictation_records_stops_and_comes_back_as_text(self) -> None:
        await self.configure_dictation(stt_language="en", audio_input="usb.mic")

        started = await self.client.post("/audio/dictation/start", headers=self.headers)
        again = await self.client.post("/audio/dictation/start", headers=self.headers)
        during = (await self.client.get("/audio/status", headers=self.headers)).json()
        stopped = await self.client.post("/audio/dictation/stop", headers=self.headers)

        self.assertEqual(200, started.status_code, started.text)
        self.assertEqual(speech.MAX_DICTATION_S, started.json()["max_seconds"])
        self.assertEqual(409, again.status_code)
        self.assertEqual("already_recording", again.json()["code"])
        self.assertTrue(during["recorder"]["recording"])
        self.assertEqual(200, stopped.status_code, stopped.text)
        self.assertEqual("hello world", stopped.json()["text"])
        # What the recorder was asked for, and what the provider was sent.
        self.assertIn("--rate 16000 --channels 1 --format s16 --target usb.mic", self.log.read_text())
        (request,) = self.sent
        self.assertEqual("https://api.openai.com/v1/audio/transcriptions", str(request.url))
        self.assertEqual(f"Bearer {KEY}", request.headers["authorization"])
        body = request.content
        self.assertIn(b'name="model"\r\n\r\nwhisper-1', body)
        self.assertIn(b'name="language"\r\n\r\nen', body)
        self.assertIn(b"RIFF", body, "the recording itself went to the provider")
        self.assertEqual([], self.recordings(), "the recording outlived its transcription")
        self.assertFalse((await self.client.get("/audio/status", headers=self.headers)).json()["recorder"]["recording"])

    async def test_a_cancelled_dictation_is_deleted_and_sent_nowhere(self) -> None:
        await self.configure_dictation()

        await self.client.post("/audio/dictation/start", headers=self.headers)
        r = await self.client.post("/audio/dictation/cancel", headers=self.headers)
        stop = await self.client.post("/audio/dictation/stop", headers=self.headers)

        self.assertEqual({"cancelled": True}, r.json())
        self.assertEqual(409, stop.status_code, "there is nothing left to stop")
        self.assertEqual([], self.sent)
        self.assertEqual([], self.recordings())

    async def test_the_cap_ends_a_recording_nobody_stopped(self) -> None:
        await self.configure_dictation()
        self.recorder.max_seconds = 0.6

        await self.client.post("/audio/dictation/start", headers=self.headers)
        deadline = time.monotonic() + 10
        while self.recorder.recording() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        recording = self.recorder.recording()
        stopped = await self.client.post("/audio/dictation/stop", headers=self.headers)

        self.assertFalse(recording, "the recording ran past its cap")
        self.assertEqual(200, stopped.status_code, stopped.text)
        self.assertEqual("hello world", stopped.json()["text"], "what was recorded up to the cap is kept")
        self.assertEqual([], self.recordings())

    async def test_a_recorder_that_cannot_open_the_mic_says_why(self) -> None:
        await self.configure_dictation()
        with mock.patch.dict(os.environ, {"STUB_PW_RECORD_FAIL": "1"}):
            r = await self.client.post("/audio/dictation/start", headers=self.headers)

        self.assertEqual(503, r.status_code, r.text)
        self.assertEqual("recorder_failed", r.json()["code"])
        self.assertIn("no such target node", r.json()["message"])
        self.assertEqual([], self.recordings())

    async def test_silence_is_not_sent(self) -> None:
        await self.configure_dictation()
        with mock.patch.dict(os.environ, {"STUB_PW_RECORD_SILENT": "1"}):
            await self.client.post("/audio/dictation/start", headers=self.headers)
            r = await self.client.post("/audio/dictation/stop", headers=self.headers)

        self.assertEqual(422, r.status_code, r.text)
        self.assertEqual("no_audio", r.json()["code"])
        self.assertEqual([], self.sent)
        self.assertEqual([], self.recordings())

    async def test_a_stopping_engine_closes_the_mic_and_deletes_the_recording(self) -> None:
        await self.configure_dictation()
        await self.client.post("/audio/dictation/start", headers=self.headers)

        self.recorder.discard()

        self.assertFalse(self.recorder.recording())
        self.assertEqual([], self.recordings())

    async def test_the_recordings_folder_is_private(self) -> None:
        await self.configure_dictation()
        await self.client.post("/audio/dictation/start", headers=self.headers)

        mode = stat.S_IMODE((home.codify_home() / "dictation").stat().st_mode)

        self.assertEqual(0o700, mode)


class TestInputs(SpeechCase):
    def test_the_sources_are_read_from_a_pipewire_dump_with_the_default_marked(self) -> None:
        listed = speech.inputs()

        self.assertTrue(listed["available"])
        self.assertEqual(
            [
                {"name": "usb.mic", "description": "USB Microphone", "default": True},
                {"name": "builtin.mic", "description": "Built-in", "default": False},
            ],
            listed["inputs"],
        )

    async def test_the_route_says_why_when_pipewire_cannot_be_asked(self) -> None:
        with mock.patch.dict(os.environ, {"STUB_PW_DUMP_FAIL": "1"}):
            failing = (await self.client.get("/audio/inputs", headers=self.headers)).json()
        with mock.patch.dict(os.environ, {"PATH": str(self.root / "empty")}):
            missing = (await self.client.get("/audio/inputs", headers=self.headers)).json()

        self.assertFalse(failing["available"])
        self.assertIn("Host is down", failing["reason"])
        self.assertFalse(missing["available"])
        self.assertIn("pipewire", missing["reason"])

    def test_a_dump_that_is_not_json_lists_nothing(self) -> None:
        self.assertEqual([], speech.parse_inputs("not json"))
        self.assertEqual([], speech.parse_inputs('{"a": 1}'))


if __name__ == "__main__":
    unittest.main()

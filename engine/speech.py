"""Voice: dictation (speech to text) and read-aloud (text to speech) through a speech provider.

Codify does not ship a speech model, for the same reason it does not ship a language model: the
provider knows its models. A speech target is a provider slug and a model id from the Audio tab,
resolved the way every other target is: a built-in from the catalogue, a custom slug from the role
row that defines it. It is spoken to over the OpenAI audio API (`/audio/transcriptions`,
`/audio/speech`), which a local server (speaches, LocalAI) and the hosted ones that offer speech
(OpenAI, Groq) all answer. A provider that speaks another protocol is refused with a reason rather
than kept on a list of providers believed to have audio, a list that would go stale the day one
added it.

The microphone is recorded here, in the engine, by PipeWire's `pw-record`, and not in the webview.
The desktop webview (WebKitGTK through wry) neither enables media capture nor answers a permission
request, and granting the microphone to a webview that sits beside an in-app browser is a door
nobody needs open. The engine already owns every process it starts (the spawn guard), so a
recording is one more guarded process with a hard time limit, its file in a private directory,
deleted on every road out.

Nothing is stored: the recording exists between start and stop, is sent once to the configured
provider, and is deleted; a synthesized answer goes straight back to the caller.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from engine import home
from engine.models import BUILTIN_PROVIDERS
from engine.providers import Keychain, ProviderError, key_destination_problem, post_bytes, post_json
from engine.services import AgentRegistryService, ApiError, SettingsService, custom_provider_address
from engine.spawn_guard import guarded_argv, guarded_env

# The longest dictation. A recording nobody stops (a closed window, a forgotten button) ends here,
# and two minutes of 16 kHz mono is under 4 MB, inside every provider's upload limit.
MAX_DICTATION_S = 120
# The longest text read aloud in one request: the OpenAI speech API's own limit, which the
# compatible servers follow. Longer answers are cut by the caller, not refused mid-sentence here.
MAX_SPEAK_CHARS = 4096
# How long a stopped recorder gets to finish its file before its group is killed.
STOP_GRACE_S = 2.0
# How soon a recorder that cannot start (no PipeWire daemon, a device that vanished) says so.
START_CHECK_S = 0.3
SPEECH_TIMEOUT_S = 120.0
INPUTS_TIMEOUT_S = 5.0
# The only protocol speech is spoken in.
SPEECH_PROTOCOL = "openai_compat"
# A WAV header with no samples behind it.
_EMPTY_WAV_BYTES = 44


@dataclass(frozen=True)
class SpeechTarget:
    """Where a dictation or a read-aloud request goes, with the credential it carries (may be empty)."""

    provider: str
    model: str
    base_url: str
    api_key: str


def resolve(
    registry: AgentRegistryService, keychain: Keychain, provider: str, model: str, *, what: str,
    base_url: str = "",
) -> SpeechTarget:
    """The speech target for a provider slug and model, or an `ApiError` saying why there is none.

    `what` is `stt` or `tts`, and names the setting a refusal points at. `base_url` is the speech
    server's own address (`stt_base_url` / `tts_base_url`), which a *custom* provider uses before
    any role's: a local speech server is nothing an agent role has to know about. A built-in
    provider ignores it, so an address typed beside "openai" can never carry the OpenAI key away.
    """
    provider, model, base_url = provider.strip(), model.strip(), base_url.strip()
    label = "dictation" if what == "stt" else "read-aloud"
    if not provider or not model:
        raise ApiError(
            409, f"{what}_not_configured",
            f"{label} has no provider and model yet: choose them in Settings → Audio",
        )
    builtin = BUILTIN_PROVIDERS.get(provider)
    if builtin is not None:
        protocol, base_url = str(builtin["protocol"]), str(builtin["base_url"])
        api_key = keychain.get_provider_key(provider)
        if builtin.get("needs_key") and not api_key:
            raise ApiError(
                409, "speech_key_missing",
                f"{provider} needs an API key for {label}: save one in Settings → Provider Keys",
            )
    elif base_url:
        protocol, api_key = SPEECH_PROTOCOL, keychain.get_provider_key(provider)
    else:
        found = custom_provider_address(provider, registry.list_configs())
        if found is None:
            raise ApiError(
                409, "speech_provider_unknown",
                f"the custom provider {provider!r} has no address: give its base URL in Settings → Audio",
            )
        protocol, base_url, api_key_ref = found
        api_key = keychain.get(api_key_ref) or keychain.get_provider_key(provider)
    if protocol != SPEECH_PROTOCOL:
        raise ApiError(
            409, "speech_protocol_unsupported",
            f"{provider} speaks the {protocol} protocol; {label} uses the OpenAI audio API, so choose a "
            "provider (or a local speech server) that answers it",
        )
    if api_key:
        problem = key_destination_problem(base_url)
        if problem is not None:
            raise ApiError(409, "invalid_base_url", problem)
    return SpeechTarget(provider=provider, model=model, base_url=base_url.rstrip("/"), api_key=api_key)


def _headers(target: SpeechTarget) -> dict[str, str]:
    # A local speech server needs no key, and an empty bearer token is a refusal some servers give
    # for a header they did not ask for.
    return {"Authorization": f"Bearer {target.api_key}"} if target.api_key else {}


async def transcribe(
    target: SpeechTarget, wav: bytes, language: str = "",
    *, transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """The text a recording says, from the provider's `/audio/transcriptions`."""
    data = {"model": target.model, "response_format": "json"}
    if language.strip():
        data["language"] = language.strip()
    async with httpx.AsyncClient(timeout=SPEECH_TIMEOUT_S, transport=transport) as client:
        reply = await post_json(
            client, f"{target.base_url}/audio/transcriptions",
            label=f"{target.provider} transcription",
            headers=_headers(target), data=data,
            files={"file": ("dictation.wav", wav, "audio/wav")},
        )
    text = reply.get("text") if isinstance(reply, dict) else None
    if not isinstance(text, str):
        raise ProviderError(
            "provider_bad_response", f"{target.provider} transcription answered without a `text` field",
        )
    return text.strip()


async def synthesize(
    target: SpeechTarget, voice: str, text: str,
    *, transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    """Spoken audio (WAV) for `text`, from the provider's `/audio/speech`."""
    async with httpx.AsyncClient(timeout=SPEECH_TIMEOUT_S, transport=transport) as client:
        audio = await post_bytes(
            client, f"{target.base_url}/audio/speech",
            label=f"{target.provider} speech",
            headers=_headers(target),
            # WAV, not MP3: the webview plays it through GStreamer, and an MP3 decoder is a plugin a
            # machine may not have, where PCM is not.
            json={"model": target.model, "voice": voice, "input": text, "response_format": "wav"},
        )
    if not audio:
        raise ProviderError("provider_bad_response", f"{target.provider} speech answered with no audio")
    return audio


def configured(
    settings: SettingsService, registry: AgentRegistryService, keychain: Keychain, what: str
) -> dict[str, Any]:
    """Whether dictation (`stt`) or read-aloud (`tts`) can run now, and if not, the reason."""
    provider, model = settings.get_str(f"{what}_provider"), settings.get_str(f"{what}_model")
    out: dict[str, Any] = {"provider": provider, "model": model, "configured": False, "reason": None}
    try:
        resolve(registry, keychain, provider, model, what=what, base_url=settings.get_str(f"{what}_base_url"))
    except ApiError as refusal:
        out["reason"] = refusal.message
        return out
    if what == "tts" and not settings.get_str("tts_voice"):
        out["reason"] = "read-aloud has no voice yet: name one in Settings → Audio"
        return out
    out["configured"] = True
    return out


def _spawn(argv: list[str], *, stdout: int) -> subprocess.Popen[bytes]:
    """Start one PipeWire tool under the guard, in a session of its own, like the folder picker."""
    return subprocess.Popen(  # noqa: S603 — argv is a fixed PipeWire tool resolved by shutil.which, wrapped by guarded_argv right here; no shell
        guarded_argv(argv),
        # PipeWire is found through XDG_RUNTIME_DIR and the session's own variables, so the
        # environment passes through; the guard adds only the engine's pid.
        env=guarded_env(dict(os.environ)),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=subprocess.PIPE,
        # The caller's half of the guard contract (see spawn_guard.py): the guard leads the
        # session, and so the group, that a stop or a timeout signals.
        start_new_session=True,
    )


def _kill_group(pid: int, sig: int) -> None:
    # Imported here: the sandbox is the one place a group kill is written, with its refusal to
    # signal a group the engine does not lead.
    from engine.sandbox import SandboxService

    SandboxService._kill_group(pid, sig=sig)


def inputs() -> dict[str, Any]:
    """The microphones PipeWire knows, from `pw-dump`: `{available, reason, inputs}`."""
    exe = shutil.which("pw-dump")
    if not exe:
        return {
            "available": False,
            "reason": "pw-dump was not found: voice input needs PipeWire's tools (the pipewire package)",
            "inputs": [],
        }
    try:
        proc = _spawn([exe], stdout=subprocess.PIPE)
    except OSError as exc:
        return {"available": False, "reason": f"pw-dump could not start: {exc.strerror or exc}", "inputs": []}
    try:
        out, err = proc.communicate(timeout=INPUTS_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid, signal.SIGKILL)
        proc.communicate()
        return {"available": False, "reason": f"pw-dump did not answer within {INPUTS_TIMEOUT_S:g} s", "inputs": []}
    if proc.returncode != 0:
        reason = " ".join(err.decode("utf-8", "replace").split())[-300:] or f"pw-dump exited {proc.returncode}"
        return {"available": False, "reason": reason, "inputs": []}
    return {"available": True, "reason": None, "inputs": parse_inputs(out.decode("utf-8", "replace"))}


def parse_inputs(dump: str) -> list[dict[str, Any]]:
    """The audio sources in a `pw-dump`, each with whether it is the session's default."""
    try:
        objects = json.loads(dump)
    except ValueError:
        return []
    if not isinstance(objects, list):
        return []
    default = ""
    sources: list[dict[str, Any]] = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type", "")
        if kind == "PipeWire:Interface:Metadata":
            for entry in obj.get("metadata") or []:
                if isinstance(entry, dict) and entry.get("key") == "default.audio.source":
                    value = entry.get("value")
                    if isinstance(value, dict) and isinstance(value.get("name"), str):
                        default = value["name"]
        elif kind == "PipeWire:Interface:Node":
            props = (obj.get("info") or {}).get("props") or {}
            if str(props.get("media.class", "")).startswith("Audio/Source") and props.get("node.name"):
                sources.append({
                    "name": str(props["node.name"]),
                    "description": str(props.get("node.description") or props.get("node.nick") or props["node.name"]),
                })
    for source in sources:
        source["default"] = source["name"] == default
    return sources


class Recorder:
    """One dictation at a time, recorded by `pw-record` into a private file, ended by stop or by the cap."""

    def __init__(self, max_seconds: float = MAX_DICTATION_S) -> None:
        self.max_seconds = max_seconds
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._path: Path | None = None
        self._started_at = 0.0
        self._timer: threading.Timer | None = None
        # True once the engine itself ended the recording (a stop, a cancel, the cap), so an exit
        # status that is the answer to our own signal is not mistaken for the recorder failing.
        self._ended_by_us = False

    @staticmethod
    def available() -> tuple[bool, str | None]:
        if shutil.which("pw-record"):
            return True, None
        return False, "pw-record was not found: voice input needs PipeWire's tools (the pipewire package)"

    def recording(self) -> bool:
        with self._lock:
            return self._proc is not None and not self._ended_by_us

    def start(self, target: str = "") -> dict[str, Any]:
        with self._lock:
            if self._proc is not None and not self._ended_by_us:
                raise ApiError(409, "already_recording", "a dictation is already being recorded")
            self._discard_locked()
            exe = shutil.which("pw-record")
            if not exe:
                raise ApiError(503, "recorder_unavailable", self.available()[1] or "pw-record was not found")
            folder = home.codify_home() / "dictation"
            home.ensure_private_dir(folder)
            path = folder / f"{uuid.uuid4().hex}.wav"
            argv = [exe, "--rate", "16000", "--channels", "1", "--format", "s16"]
            if target.strip():
                argv += ["--target", target.strip()]
            argv.append(str(path))
            try:
                proc = _spawn(argv, stdout=subprocess.DEVNULL)
            except OSError as exc:
                raise ApiError(503, "recorder_failed", f"pw-record could not start: {exc.strerror or exc}") from exc
            try:
                proc.wait(timeout=START_CHECK_S)
            except subprocess.TimeoutExpired:
                pass
            else:
                err = proc.communicate()[1].decode("utf-8", "replace")
                path.unlink(missing_ok=True)
                reason = " ".join(err.split())[-300:] or f"pw-record exited {proc.returncode}"
                raise ApiError(503, "recorder_failed", f"the microphone could not be opened: {reason}")
            self._proc, self._path, self._ended_by_us = proc, path, False
            self._started_at = time.monotonic()
            self._timer = threading.Timer(self.max_seconds, self._end_by_cap, args=(proc,))
            self._timer.daemon = True
            self._timer.start()
            return {"recording": True, "max_seconds": self.max_seconds}

    def stop(self) -> tuple[bytes, float]:
        """The recorded WAV and how many seconds it ran; the file is gone when this returns."""
        with self._lock:
            proc, path = self._proc, self._path
            if proc is None or path is None:
                raise ApiError(409, "not_recording", "there is no dictation to stop")
            ended_by_us = self._ended_by_us
            self._end_locked(proc)
            seconds = min(time.monotonic() - self._started_at, float(self.max_seconds))
            self._proc, self._path = None, None
        try:
            stderr = proc.communicate()[1].decode("utf-8", "replace")
            if not ended_by_us and proc.returncode not in (0, -signal.SIGTERM, -signal.SIGKILL):
                reason = " ".join(stderr.split())[-300:] or f"pw-record exited {proc.returncode}"
                raise ApiError(503, "recorder_failed", f"the recording stopped on its own: {reason}")
            data = path.read_bytes() if path.exists() else b""
        finally:
            path.unlink(missing_ok=True)
        if len(data) <= _EMPTY_WAV_BYTES:
            raise ApiError(422, "no_audio", "nothing was recorded: is a microphone connected and unmuted?")
        return data, seconds

    def cancel(self) -> bool:
        """End and delete a recording without sending it anywhere. True if there was one."""
        with self._lock:
            had = self._proc is not None
            self._discard_locked()
            return had

    def discard(self) -> None:
        """The engine is stopping: end any recording and delete its file."""
        with self._lock:
            self._discard_locked()

    def _end_by_cap(self, proc: subprocess.Popen[bytes]) -> None:
        with self._lock:
            if self._proc is proc and not self._ended_by_us:
                self._end_locked(proc)

    def _end_locked(self, proc: subprocess.Popen[bytes]) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if proc.poll() is None:
            self._ended_by_us = True
            # TERM, not INT: the guard answers a TERM from a live engine by forwarding nothing and
            # letting the command finish, and pw-record answers it by closing the file properly.
            _kill_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                _kill_group(proc.pid, signal.SIGKILL)
                proc.wait()

    def _discard_locked(self) -> None:
        proc, path = self._proc, self._path
        if proc is not None:
            self._end_locked(proc)
            proc.communicate()
        if path is not None:
            path.unlink(missing_ok=True)
        self._proc, self._path, self._ended_by_us = None, None, False

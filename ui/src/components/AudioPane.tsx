import React, { useCallback, useEffect, useId, useMemo, useState } from "react";
import { AlertCircle, CheckCircle2, Loader2, Mic, Play, Square, Volume2 } from "lucide-react";

import {
  fetchProviders,
  getAudioInputs,
  getAudioStatus,
  getEngineSettings,
  saveEngineSettings,
  speak,
  startDictation,
  stopDictation,
} from "../api";
import { readRejection } from "../rejection.ts";
import { playSpeech, stopPlayback } from "../speech.ts";
import type {
  AudioInputs,
  AudioStatus,
  EngineSettings,
  ModelOption,
  ProviderModelStatus,
  SpeechReadiness,
} from "../types";
import { ModelSelect } from "./ModelSelect";
import { ProviderSelect } from "./ProviderSelect";
import { Button } from "./ui/Button";
import { Toggle } from "./ui/Toggle";

interface AudioPaneProps {
  /** Discovered models, so the model fields browse real ids like a role's do. */
  models?: ModelOption[];
  /** Per-provider discovery outcome, so a provider that answered nothing says why. */
  providerStatus?: ProviderModelStatus[];
  onRefreshModels?: () => void;
  refreshingModels?: boolean;
}

/** What the pane edits: the voice keys of the engine settings, as typed. */
interface VoiceDraft {
  stt_provider: string;
  stt_model: string;
  stt_language: string;
  tts_provider: string;
  tts_model: string;
  tts_voice: string;
  audio_input: string;
  stt_base_url: string;
  tts_base_url: string;
  tts_auto_read: boolean;
}

const EMPTY: VoiceDraft = {
  stt_provider: "",
  stt_model: "",
  stt_language: "",
  tts_provider: "",
  tts_model: "",
  tts_voice: "",
  audio_input: "",
  stt_base_url: "",
  tts_base_url: "",
  tts_auto_read: false,
};

const SAMPLE = "This is how Codify will read its answers aloud.";

function fromSettings(s: EngineSettings): VoiceDraft | null {
  // An engine that predates voice answers without these keys; editing them would be a save the
  // engine refuses, so the pane says so instead.
  if (!s.stt_provider || !s.tts_provider) return null;
  return {
    stt_provider: s.stt_provider.value,
    stt_model: s.stt_model?.value ?? "",
    stt_language: s.stt_language?.value ?? "",
    tts_provider: s.tts_provider.value,
    tts_model: s.tts_model?.value ?? "",
    tts_voice: s.tts_voice?.value ?? "",
    audio_input: s.audio_input?.value ?? "",
    stt_base_url: s.stt_base_url?.value ?? "",
    tts_base_url: s.tts_base_url?.value ?? "",
    tts_auto_read: (s.tts_auto_read?.value ?? 0) === 1,
  };
}

const inputClass =
  "w-full bg-codify-bg border border-codify-border rounded-lg px-3 py-2 text-xs text-codify-primary " +
  "placeholder-codify-muted focus:outline-none focus:border-codify-accent";

/**
 * Where a custom speech provider lives: a local speech server, typically. Only for a custom slug,
 * because the engine ignores the address for a built-in one (an address typed beside "openai"
 * must not be able to carry its key somewhere else), and a field that does nothing is a trap.
 */
const ServerAddress: React.FC<{ value: string; onChange: (url: string) => void }> = ({ value, onChange }) => (
  <label className="flex flex-col gap-1.5 text-xs text-codify-secondary font-medium">
    Server address
    <input
      className={inputClass}
      value={value}
      placeholder="http://127.0.0.1:8000/v1"
      onChange={(e) => onChange(e.target.value)}
    />
  </label>
);

/** One feature's readiness, in the engine's own words. */
const Readiness: React.FC<{ what: string; state: SpeechReadiness | null }> = ({ what, state }) => {
  if (!state) return null;
  return state.configured ? (
    <p className="flex items-center gap-1.5 text-2xs text-codify-success">
      <CheckCircle2 className="w-3.5 h-3.5" />
      {what} is ready: {state.provider} · {state.model}
    </p>
  ) : (
    <p className="flex items-start gap-1.5 text-2xs text-codify-warning" role="status">
      <AlertCircle className="w-3.5 h-3.5 flex-shrink-0 mt-px" />
      <span>{state.reason}</span>
    </p>
  );
};

/**
 * Settings → Audio: the microphone, dictation, and reading answers aloud.
 *
 * Everything here is an engine setting (`PUT /settings/engine`), because the engine is what records
 * and what calls the speech providers; this pane owns no audio state of its own. The status lines
 * are the engine's answer to "can this run now, and if not why", read back after every save, so the
 * pane never says "ready" about something the engine would refuse.
 */
export const AudioPane: React.FC<AudioPaneProps> = ({
  models = [],
  providerStatus = [],
  onRefreshModels,
  refreshingModels,
}) => {
  const [builtins, setBuiltins] = useState<string[] | null>(null);
  const [saved, setSaved] = useState<VoiceDraft | null>(null);
  const [draft, setDraft] = useState<VoiceDraft>(EMPTY);
  const [unsupported, setUnsupported] = useState(false);
  const [status, setStatus] = useState<AudioStatus | null>(null);
  const [inputs, setInputs] = useState<AudioInputs | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [trial, setTrial] = useState<{ phase: "idle" | "recording" | "transcribing"; text?: string; error?: string }>({
    phase: "idle",
  });
  const [sample, setSample] = useState<{ busy: boolean; error?: string }>({ busy: false });
  const micField = useId();

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await getAudioStatus());
    } catch (err) {
      setLoadError(readRejection(err, "Could not read the audio status."));
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getEngineSettings(), fetchProviders(), getAudioInputs(), getAudioStatus()])
      .then(([settings, catalog, mics, audio]) => {
        if (cancelled) return;
        const voice = fromSettings(settings);
        setUnsupported(voice === null);
        setSaved(voice);
        setDraft(voice ?? EMPTY);
        setBuiltins(catalog.builtins.map((b) => b.slug));
        setInputs(mics);
        setStatus(audio);
      })
      .catch((err) => {
        if (!cancelled) setLoadError(readRejection(err, "Could not load the audio settings."));
      });
    return () => {
      cancelled = true;
      stopPlayback();
    };
  }, []);

  const dirty = useMemo(
    () => saved !== null && (Object.keys(EMPTY) as Array<keyof VoiceDraft>).some((k) => draft[k] !== saved[k]),
    [draft, saved],
  );

  const edit = (patch: Partial<VoiceDraft>) => {
    setDraft((d) => ({ ...d, ...patch }));
    setMsg(null);
  };

  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      await saveEngineSettings({ ...draft });
      setSaved(draft);
      await refreshStatus();
      setMsg({ ok: true, text: "Saved." });
    } catch (err) {
      setMsg({ ok: false, text: readRejection(err, "Could not save the audio settings.") });
    } finally {
      setSaving(false);
    }
  };

  const tryDictation = async () => {
    if (trial.phase === "recording") {
      setTrial({ phase: "transcribing" });
      try {
        const { text } = await stopDictation();
        setTrial({ phase: "idle", text: text || "(no speech was recognised)" });
      } catch (err) {
        setTrial({ phase: "idle", error: readRejection(err, "Could not transcribe.") });
      }
      return;
    }
    try {
      await startDictation();
      setTrial({ phase: "recording" });
    } catch (err) {
      setTrial({ phase: "idle", error: readRejection(err, "Could not start recording.") });
    }
  };

  const playSample = async () => {
    setSample({ busy: true });
    try {
      await playSpeech("audio-settings-sample", SAMPLE, speak);
      setSample({ busy: false });
    } catch (err) {
      setSample({ busy: false, error: readRejection(err, "Could not play the sample.") });
    }
  };

  const modelsFor = (provider: string) => models.filter((m) => m.provider === provider);
  const isCustom = (provider: string) => provider !== "" && !(builtins ?? []).includes(provider);
  const discoveryFor = (provider: string) => providerStatus.find((s) => s.provider === provider);

  if (loadError) {
    return (
      <div className="flex items-start gap-2 p-3 bg-codify-danger/20 border border-codify-danger/60 rounded-xl text-xs text-codify-danger-ink">
        <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
        <span>{loadError}</span>
      </div>
    );
  }
  if (builtins === null) {
    return <p className="text-xs text-codify-muted">Loading the audio settings…</p>;
  }
  if (unsupported) {
    return (
      <p className="text-xs text-codify-warning">
        This engine has no voice settings. Update Codify to use dictation and read-aloud.
      </p>
    );
  }

  const section = "flex flex-col gap-3 bg-codify-bg border border-codify-border rounded-xl p-3.5";
  const heading = "flex items-center gap-2 text-xs font-semibold text-codify-secondary";

  return (
    <div className="space-y-4">
      <p className="text-xs text-codify-muted leading-relaxed">
        Speech goes to the providers you choose here: a local speech server on this machine (speaches,
        LocalAI) or a hosted one (OpenAI, Groq) with its key under Provider Keys. Any provider that
        speaks the OpenAI audio API works; Codify records the microphone itself, only while you dictate,
        and keeps nothing.
      </p>

      <section className={section} aria-label="Microphone">
        <div className={heading}>
          <Mic className="w-3.5 h-3.5 text-codify-accent" /> Microphone
        </div>
        {inputs && !inputs.available ? (
          <p className="text-2xs text-codify-warning" role="status">{inputs.reason}</p>
        ) : (
          <div className="flex flex-col gap-1.5">
            {/* A label of its own, not a wrapping one: wrapped, the select's options would be read as its name. */}
            <label htmlFor={micField} className="text-xs text-codify-secondary font-medium">
              Input
            </label>
            <select
              id={micField}
              className={inputClass}
              value={draft.audio_input}
              onChange={(e) => edit({ audio_input: e.target.value })}
            >
              <option value="">Default microphone</option>
              {(inputs?.inputs ?? []).map((mic) => (
                <option key={mic.name} value={mic.name}>
                  {mic.description}
                  {mic.default ? " (default)" : ""}
                </option>
              ))}
            </select>
          </div>
        )}
      </section>

      <section className={section} aria-label="Dictation">
        <div className={heading}>
          <Mic className="w-3.5 h-3.5 text-codify-accent" /> Dictation — speech to text in the prompt
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <ProviderSelect value={draft.stt_provider} onChange={(p) => edit({ stt_provider: p })} builtins={builtins} />
          <ModelSelect
            provider={draft.stt_provider}
            value={draft.stt_model}
            onChange={(m) => edit({ stt_model: m })}
            options={modelsFor(draft.stt_provider)}
            discovery={discoveryFor(draft.stt_provider)}
            onRefresh={onRefreshModels}
            refreshing={refreshingModels}
          />
        </div>
        {isCustom(draft.stt_provider) && (
          <ServerAddress value={draft.stt_base_url} onChange={(url) => edit({ stt_base_url: url })} />
        )}
        <label className="flex flex-col gap-1.5 text-xs text-codify-secondary font-medium">
          Language (optional)
          <input
            className={inputClass}
            value={draft.stt_language}
            placeholder="Detected from the speech when empty, e.g. en"
            onChange={(e) => edit({ stt_language: e.target.value })}
          />
        </label>
        <Readiness what="Dictation" state={status?.dictation ?? null} />
        <div className="flex flex-wrap items-center gap-2">
          <Button
            tone="subtle"
            size="sm"
            onClick={tryDictation}
            disabled={dirty || trial.phase === "transcribing" || !status?.dictation.configured}
            title={dirty ? "Save first: dictation uses the saved settings" : undefined}
          >
            {trial.phase === "recording" ? (
              <>
                <Square className="w-3 h-3 fill-current" /> Stop and transcribe
              </>
            ) : trial.phase === "transcribing" ? (
              <>
                <Loader2 className="w-3.5 h-3.5 animate-spin" /> Transcribing…
              </>
            ) : (
              <>
                <Mic className="w-3.5 h-3.5" /> Try dictation
              </>
            )}
          </Button>
          {trial.text && <span className="text-xs text-codify-primary">“{trial.text}”</span>}
          {trial.error && <span className="text-xs text-codify-danger" role="alert">{trial.error}</span>}
        </div>
      </section>

      <section className={section} aria-label="Read aloud">
        <div className={heading}>
          <Volume2 className="w-3.5 h-3.5 text-codify-accent" /> Read aloud — answers as speech
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <ProviderSelect value={draft.tts_provider} onChange={(p) => edit({ tts_provider: p })} builtins={builtins} />
          <ModelSelect
            provider={draft.tts_provider}
            value={draft.tts_model}
            onChange={(m) => edit({ tts_model: m })}
            options={modelsFor(draft.tts_provider)}
            discovery={discoveryFor(draft.tts_provider)}
            onRefresh={onRefreshModels}
            refreshing={refreshingModels}
          />
        </div>
        {isCustom(draft.tts_provider) && (
          <ServerAddress value={draft.tts_base_url} onChange={(url) => edit({ tts_base_url: url })} />
        )}
        <label className="flex flex-col gap-1.5 text-xs text-codify-secondary font-medium">
          Voice
          <input
            className={inputClass}
            value={draft.tts_voice}
            placeholder="The provider's own voice name"
            onChange={(e) => edit({ tts_voice: e.target.value })}
          />
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <Toggle
            armed={draft.tts_auto_read}
            onClick={() => edit({ tts_auto_read: !draft.tts_auto_read })}
            aria-pressed={draft.tts_auto_read}
          >
            <Volume2 className="w-3.5 h-3.5" />
            <span>Read each answer aloud as it arrives</span>
          </Toggle>
        </div>
        <Readiness what="Read-aloud" state={status?.read_aloud ?? null} />
        <div className="flex flex-wrap items-center gap-2">
          <Button
            tone="subtle"
            size="sm"
            onClick={playSample}
            disabled={dirty || sample.busy || !status?.read_aloud.configured}
            title={dirty ? "Save first: read-aloud uses the saved settings" : undefined}
          >
            {sample.busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
            Play sample
          </Button>
          {sample.error && <span className="text-xs text-codify-danger" role="alert">{sample.error}</span>}
        </div>
      </section>

      <div className="flex items-center gap-3">
        <Button tone="primary" size="md" onClick={save} disabled={!dirty || saving}>
          {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
          Save audio settings
        </Button>
        {msg && (
          <span className={`text-xs ${msg.ok ? "text-codify-success" : "text-codify-danger"}`} role="status">
            {msg.text}
          </span>
        )}
      </div>
    </div>
  );
};

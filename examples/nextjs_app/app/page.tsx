"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { startVoiceSession, type VoiceSessionHandle, type TranscriptRole } from "../lib/voice-client";

type State = "idle" | "connecting" | "live" | "ended" | "error";

interface TranscriptLine {
  role: TranscriptRole;
  text: string;
  isFinal: boolean;
  at: string;
}

interface ToolCall {
  callId: string;
  name: string;
  args: Record<string, unknown>;
  result?: unknown;
  at: string;
}

interface ToolInfo {
  name: string;
  description?: string;
}

interface ProviderInfo {
  id: string;
  model: string;
  tools?: ToolInfo[];
}

const BACKEND = process.env.NEXT_PUBLIC_VOICE_AGENT_BACKEND ?? "http://localhost:5050";

const PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI",
  xai: "xAI Grok",
};

// Preferred picker order. The first id present in the backend response is
// selected by default.
const PROVIDER_ORDER = ["xai", "openai"];

function sortProviders(list: ProviderInfo[]): ProviderInfo[] {
  const rank = (id: string) => {
    const i = PROVIDER_ORDER.indexOf(id);
    return i === -1 ? PROVIDER_ORDER.length : i;
  };
  return [...list].sort((a, b) => rank(a.id) - rank(b.id));
}

const MIC_STORAGE_KEY = "voiceagentpy.audioInputDeviceId";

export default function Page() {
  const [state, setState] = useState<State>("idle");
  const [lines, setLines] = useState<TranscriptLine[]>([]);
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [selectedProvider, setSelectedProvider] = useState<string | null>(null);
  const [audioInputs, setAudioInputs] = useState<MediaDeviceInfo[]>([]);
  const [selectedMic, setSelectedMic] = useState<string>("");
  const sessionRef = useRef<VoiceSessionHandle | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`${BACKEND}/providers`);
        if (!r.ok) return;
        const data = (await r.json()) as { providers: ProviderInfo[] };
        if (cancelled) return;
        const ordered = sortProviders(data.providers);
        setProviders(ordered);
        if (ordered.length > 0) {
          setSelectedProvider(ordered[0].id);
        }
      } catch {
        // Backend not reachable yet — picker stays empty; first connect attempt will surface the error.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Enumerate audio inputs. Labels are empty until the user grants mic
  // permission — once a session has started, labels appear and the dropdown
  // re-populates. devicechange covers plug/unplug.
  useEffect(() => {
    if (typeof navigator === "undefined" || !navigator.mediaDevices?.enumerateDevices) {
      return;
    }
    let cancelled = false;
    const refresh = async () => {
      try {
        const devices = await navigator.mediaDevices.enumerateDevices();
        if (cancelled) return;
        const inputs = devices.filter((d) => d.kind === "audioinput");
        setAudioInputs(inputs);
        setSelectedMic((current) => {
          if (current && inputs.some((d) => d.deviceId === current)) return current;
          const stored = typeof window !== "undefined" ? localStorage.getItem(MIC_STORAGE_KEY) : null;
          if (stored && inputs.some((d) => d.deviceId === stored)) return stored;
          return ""; // empty string = browser default
        });
      } catch {
        // ignore — selector just stays empty
      }
    };
    refresh();
    navigator.mediaDevices.addEventListener("devicechange", refresh);
    return () => {
      cancelled = true;
      navigator.mediaDevices.removeEventListener("devicechange", refresh);
    };
  }, [state]); // re-run after state transitions (esp. once mic permission is granted)

  const onMicChange = useCallback((deviceId: string) => {
    setSelectedMic(deviceId);
    if (typeof window !== "undefined") {
      if (deviceId) localStorage.setItem(MIC_STORAGE_KEY, deviceId);
      else localStorage.removeItem(MIC_STORAGE_KEY);
    }
  }, []);

  const appendTranscript = useCallback(
    (text: string, role: TranscriptRole, isFinal: boolean) => {
      setLines((prev) => {
        if (prev.length > 0) {
          const last = prev[prev.length - 1];
          if (last.role === role && !last.isFinal) {
            const updated = [...prev];
            updated[updated.length - 1] = isFinal
              ? { ...last, text, isFinal: true }
              : { ...last, text: last.text + text };
            return updated;
          }
        }
        return [...prev, { role, text, isFinal, at: new Date().toLocaleTimeString() }];
      });
    },
    [],
  );

  const handleStart = useCallback(async () => {
    setErrorMsg(null);
    setLines([]);
    setToolCalls([]);
    setState("connecting");
    try {
      const handle = await startVoiceSession({
        backendUrl: BACKEND,
        provider: selectedProvider ?? undefined,
        audioInputDeviceId: selectedMic || undefined,
        onState: (s, info) => {
          if (s === "error") {
            const detail =
              info && typeof info === "object" && "detail" in info
                ? String((info as { detail: unknown }).detail)
                : null;
            setErrorMsg(detail || (typeof info === "string" ? info : JSON.stringify(info)));
          }
          setState(s);
        },
        onTranscript: appendTranscript,
        onToolCall: (name, args, callId) =>
          setToolCalls((prev) => [
            ...prev,
            { callId, name, args, at: new Date().toLocaleTimeString() },
          ]),
        onToolResult: (_name, result, callId) =>
          setToolCalls((prev) =>
            prev.map((tc) => (tc.callId === callId ? { ...tc, result } : tc)),
          ),
      });
      sessionRef.current = handle;
    } catch (e) {
      setErrorMsg(e instanceof Error ? e.message : String(e));
      setState("error");
    }
  }, [appendTranscript, selectedProvider, selectedMic]);

  const handleStop = useCallback(async () => {
    const h = sessionRef.current;
    sessionRef.current = null;
    if (h) await h.stop();
    setState("ended");
  }, []);

  const inSession = state === "live" || state === "connecting";

  return (
    <main className="container">
      <h1 className="title">Voice Agent</h1>
      <p className="subtitle">
        Press <strong>Say hello</strong> to start a realtime conversation with the agent. Audio
        streams directly from your browser to the model.
      </p>

      {providers.length > 0 && (
        <div className="provider-picker">
          {providers.map((p) => (
            <label
              key={p.id}
              className={`provider-option ${selectedProvider === p.id ? "selected" : ""}`}
            >
              <input
                type="radio"
                name="provider"
                value={p.id}
                checked={selectedProvider === p.id}
                disabled={inSession}
                onChange={() => setSelectedProvider(p.id)}
              />
              <span className="provider-name">{PROVIDER_LABELS[p.id] ?? p.id}</span>
              <span className="provider-model">{p.model}</span>
            </label>
          ))}
          {providers.length === 1 && (
            <span className="provider-hint">
              Set <code>OPENAI_API_KEY</code> and <code>XAI_API_KEY</code> in the backend to switch.
            </span>
          )}
        </div>
      )}

      {audioInputs.length > 0 && (
        <label className="mic-picker">
          <span className="mic-picker-label">Microphone</span>
          <select
            className="mic-picker-select"
            value={selectedMic}
            disabled={inSession}
            onChange={(e) => onMicChange(e.target.value)}
          >
            <option value="">System default</option>
            {audioInputs.map((d, i) => (
              <option key={d.deviceId || i} value={d.deviceId}>
                {d.label || `Microphone ${i + 1}`}
              </option>
            ))}
          </select>
        </label>
      )}

      <div className="hero">
        {!inSession ? (
          <button className="btn btn-primary" onClick={handleStart} disabled={providers.length === 0}>
            {state === "ended" || state === "error" ? "Start again" : "Say hello"}
          </button>
        ) : (
          <button className="btn btn-danger" onClick={handleStop}>
            End call
          </button>
        )}
        <span
          className={`status ${
            state === "live"
              ? "live"
              : state === "error"
              ? "error"
              : state === "connecting"
              ? "connecting"
              : ""
          }`}
        >
          {state === "idle" && "Ready"}
          {state === "connecting" && "Connecting"}
          {state === "live" && "Live"}
          {state === "ended" && "Ended"}
          {state === "error" && (errorMsg ?? "Error")}
        </span>
      </div>

      {(() => {
        const tools =
          providers.find((p) => p.id === selectedProvider)?.tools ?? [];
        return tools.length > 0 ? (
          <section className="attached-tools" aria-label="Available tools">
            <div className="attached-tools-label">Tools available to the agent</div>
            <ul className="attached-tools-list">
              {tools.map((t) => (
                <li key={t.name} className="attached-tool">
                  <code>{t.name}</code>
                  {t.description ? <span> — {t.description}</span> : null}
                </li>
              ))}
            </ul>
          </section>
        ) : null;
      })()}

      <section className="transcript">
        {lines.length === 0 ? (
          <p className="placeholder">Transcript will appear here.</p>
        ) : (
          lines.map((l, i) => (
            <div key={i} className={`bubble ${l.role}`}>
              <span className="role">{l.role}</span>
              <span>{l.text}</span>
              <span className="bubble-at">· {l.at}</span>
            </div>
          ))
        )}
      </section>

      {toolCalls.length > 0 && (
        <section className="tools">
          <div>Tool calls</div>
          {toolCalls.map((tc) => (
            <div key={tc.callId} className="tool-call">
              <div>
                <strong>{tc.name}</strong>({JSON.stringify(tc.args)}) <em>· {tc.at}</em>
              </div>
              {tc.result !== undefined && (
                <div className="tool-result">→ {JSON.stringify(tc.result)}</div>
              )}
            </div>
          ))}
        </section>
      )}
    </main>
  );
}

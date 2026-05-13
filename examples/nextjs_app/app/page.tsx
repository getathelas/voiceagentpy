"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { startVoiceSession, type VoiceSessionHandle, type TranscriptRole } from "../lib/voice-client";

type State = "idle" | "connecting" | "live" | "ended" | "error";

interface TranscriptLine {
  role: TranscriptRole;
  text: string;
  isFinal: boolean;
}

interface ToolCall {
  name: string;
  args: Record<string, unknown>;
  at: string;
}

interface ProviderInfo {
  id: string;
  model: string;
}

const BACKEND = process.env.NEXT_PUBLIC_VOICE_AGENT_BACKEND ?? "http://localhost:5050";

const PROVIDER_LABELS: Record<string, string> = {
  openai: "OpenAI",
  xai: "xAI Grok",
};

export default function Page() {
  const [state, setState] = useState<State>("idle");
  const [lines, setLines] = useState<TranscriptLine[]>([]);
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [selectedProvider, setSelectedProvider] = useState<string | null>(null);
  const sessionRef = useRef<VoiceSessionHandle | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`${BACKEND}/providers`);
        if (!r.ok) return;
        const data = (await r.json()) as { providers: ProviderInfo[] };
        if (cancelled) return;
        setProviders(data.providers);
        if (data.providers.length > 0) {
          setSelectedProvider(data.providers[0].id);
        }
      } catch {
        // Backend not reachable yet — picker stays empty; first connect attempt will surface the error.
      }
    })();
    return () => {
      cancelled = true;
    };
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
        return [...prev, { role, text, isFinal }];
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
        onState: (s, info) => {
          if (s === "error") {
            setErrorMsg(typeof info === "string" ? info : JSON.stringify(info));
          }
          setState(s);
        },
        onTranscript: appendTranscript,
        onToolCall: (name, args) =>
          setToolCalls((prev) => [...prev, { name, args, at: new Date().toLocaleTimeString() }]),
      });
      sessionRef.current = handle;
    } catch (e) {
      setErrorMsg(e instanceof Error ? e.message : String(e));
      setState("error");
    }
  }, [appendTranscript, selectedProvider]);

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

      {providers.length > 1 && (
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
        </div>
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
        <span className={`status ${state === "live" ? "live" : state === "error" ? "error" : ""}`}>
          {state === "idle" && "Ready"}
          {state === "connecting" && "Connecting…"}
          {state === "live" && "Live"}
          {state === "ended" && "Ended"}
          {state === "error" && (errorMsg ?? "Error")}
        </span>
      </div>

      <section className="transcript">
        {lines.length === 0 ? (
          <p className="placeholder">Transcript will appear here.</p>
        ) : (
          lines.map((l, i) => (
            <div key={i} className={`bubble ${l.role}`}>
              <span className="role">{l.role}</span>
              <span>{l.text}</span>
            </div>
          ))
        )}
      </section>

      {toolCalls.length > 0 && (
        <section className="tools">
          <div>Tool calls</div>
          {toolCalls.map((tc, i) => (
            <div key={i} className="tool-call">
              <strong>{tc.name}</strong> {JSON.stringify(tc.args)} <em>· {tc.at}</em>
            </div>
          ))}
        </section>
      )}
    </main>
  );
}

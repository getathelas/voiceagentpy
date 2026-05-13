"use client";

import { useCallback, useRef, useState } from "react";
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

const BACKEND = process.env.NEXT_PUBLIC_VOICE_AGENT_BACKEND ?? "http://localhost:5050";

export default function Page() {
  const [state, setState] = useState<State>("idle");
  const [lines, setLines] = useState<TranscriptLine[]>([]);
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const sessionRef = useRef<VoiceSessionHandle | null>(null);

  const appendTranscript = useCallback(
    (text: string, role: TranscriptRole, isFinal: boolean) => {
      setLines((prev) => {
        if (!isFinal && prev.length > 0) {
          const last = prev[prev.length - 1];
          if (last.role === role && !last.isFinal) {
            const updated = [...prev];
            updated[updated.length - 1] = { ...last, text: last.text + text };
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
  }, [appendTranscript]);

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

      <div className="hero">
        {!inSession ? (
          <button className="btn btn-primary" onClick={handleStart}>
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

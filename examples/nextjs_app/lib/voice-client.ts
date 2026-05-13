/**
 * Browser-side voice client.
 *
 * Flow:
 *   1. POST {backend}/sessions -> { id, url, client_secret, ... }
 *   2. getUserMedia({ audio: true }) -> attach mic to RTCPeerConnection
 *   3. createOffer -> POST SDP to provider {url} with Bearer client_secret
 *   4. setRemoteDescription(answer)
 *   5. Open WebSocket to {backend}/sessions/{id}/control for tool relay & events
 */

export type TranscriptRole = "user" | "assistant";

export interface VoiceSessionHandle {
  sessionId: string;
  stop: () => Promise<void>;
}

export interface StartVoiceSessionOpts {
  backendUrl: string;
  provider?: string;
  metadata?: Record<string, unknown>;
  onTranscript?: (text: string, role: TranscriptRole, isFinal: boolean) => void;
  onToolCall?: (name: string, args: Record<string, unknown>, callId: string) => void;
  onToolResult?: (name: string, result: unknown, callId: string) => void;
  onState?: (state: "connecting" | "live" | "ended" | "error", info?: unknown) => void;
  onEvent?: (event: unknown) => void;
}

interface SessionPayload {
  id: string;
  provider: string;
  model: string;
  url: string;
  client_secret: string;
  expires_at: string;
  transport: string;
  [k: string]: unknown;
}

export async function startVoiceSession(
  opts: StartVoiceSessionOpts,
): Promise<VoiceSessionHandle> {
  const { backendUrl, provider, metadata, onTranscript, onToolCall, onToolResult, onState, onEvent } = opts;

  onState?.("connecting");

  // 1. Mint a session on the backend.
  const sessionResp = await fetch(`${backendUrl}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider, metadata: metadata ?? {} }),
  });
  if (!sessionResp.ok) {
    onState?.("error", { stage: "mint-session", status: sessionResp.status });
    throw new Error(`Failed to mint session: ${sessionResp.status}`);
  }
  const session = (await sessionResp.json()) as SessionPayload;

  // 2. Open control WS first so we don't miss events.
  const wsUrl = backendUrl.replace(/^http/, "ws") + `/sessions/${session.id}/control`;
  const controlSocket = new WebSocket(wsUrl);

  // 3. WebRTC peer connection.
  const pc = new RTCPeerConnection();
  const remoteAudio = ensureAudioElement();

  pc.ontrack = (e) => {
    remoteAudio.srcObject = e.streams[0];
    void remoteAudio.play().catch(() => {});
  };

  // Realtime providers expose a data channel named "oai-events" (OpenAI).
  // We open one and listen for tool-call events; the WS control channel is
  // for the *backend* to do tool execution, so we forward calls there.
  const dataChannel = pc.createDataChannel("oai-events");

  // Track whether a model response is in flight. We cannot inject a
  // function_call_output and trigger a new response until the response that
  // contained the function call has fully completed (response.done) —
  // otherwise OpenAI either drops the new response.create or the model
  // continues its own stalling narrative ("I'm still checking...").
  let responseActive = false;
  const pendingToolResults: Array<{ name: string; call_id: string; result: unknown }> = [];

  const injectToolResult = (r: { name: string; call_id: string; result: unknown }) => {
    onToolResult?.(r.name, r.result, r.call_id);
    sendWhenOpen(
      dataChannel,
      JSON.stringify({
        type: "conversation.item.create",
        item: {
          type: "function_call_output",
          call_id: r.call_id,
          output: JSON.stringify(r.result ?? {}),
        },
      }),
    );
    // Nudge the model: speak the result directly instead of restating
    // "I'm checking..." filler the previous response left in history.
    sendWhenOpen(
      dataChannel,
      JSON.stringify({
        type: "response.create",
        response: {
          instructions:
            "Use the function result above to answer the user's request directly and concisely. Speak the actual data you received. Do not say you are still checking, looking up, or waiting.",
        },
      }),
    );
  };

  dataChannel.onmessage = (msg) => {
    let parsed: any;
    try {
      parsed = JSON.parse(msg.data);
    } catch {
      return;
    }
    if (typeof window !== "undefined") {
      // Light debug — comment this out when noise gets bothersome.
      console.debug("[voice-client] dc <-", parsed.type);
    }
    if (parsed.type === "response.created") {
      responseActive = true;
    } else if (parsed.type === "response.done") {
      responseActive = false;
      while (pendingToolResults.length > 0) {
        injectToolResult(pendingToolResults.shift()!);
      }
    }
    onEvent?.(parsed);
    relayProviderEventToBackend(parsed, controlSocket, onTranscript, onToolCall);
  };

  const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
  for (const track of mic.getAudioTracks()) {
    pc.addTrack(track, mic);
  }

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);

  // 4. Send the SDP offer to the provider.
  const sdpResp = await fetch(session.url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${session.client_secret}`,
      "Content-Type": "application/sdp",
    },
    body: offer.sdp ?? "",
  });
  if (!sdpResp.ok) {
    onState?.("error", { stage: "sdp-exchange", status: sdpResp.status });
    throw new Error(`Provider SDP exchange failed: ${sdpResp.status}`);
  }
  const answerSdp = await sdpResp.text();
  await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });

  // 5. Handle backend -> client messages (tool results to forward to provider).
  controlSocket.onmessage = (ev) => {
    let parsed: any;
    try {
      parsed = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (parsed?.type === "tool.result") {
      const result = {
        name: parsed.name ?? "",
        call_id: parsed.call_id ?? "",
        result: parsed.result,
      };
      if (responseActive) {
        // Buffer; flushed when response.done arrives.
        pendingToolResults.push(result);
      } else {
        injectToolResult(result);
      }
    }
  };

  controlSocket.onopen = () => onState?.("live");
  controlSocket.onclose = () => onState?.("ended");
  controlSocket.onerror = (e) => onState?.("error", e);

  const stop = async () => {
    try {
      mic.getTracks().forEach((t) => t.stop());
    } catch {}
    try {
      dataChannel.close();
    } catch {}
    try {
      pc.close();
    } catch {}
    try {
      controlSocket.close();
    } catch {}
    try {
      await fetch(`${backendUrl}/sessions/${session.id}/end`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "client_disconnect" }),
        keepalive: true,
      });
    } catch {}
    onState?.("ended");
  };

  return { sessionId: session.id, stop };
}

function relayProviderEventToBackend(
  evt: any,
  ws: WebSocket,
  onTranscript: StartVoiceSessionOpts["onTranscript"],
  onToolCall: StartVoiceSessionOpts["onToolCall"],
) {
  const t: string = evt?.type ?? "";

  if (t === "response.audio_transcript.delta") {
    onTranscript?.(evt.delta ?? "", "assistant", false);
    sendWhenOpen(ws, JSON.stringify({ type: "transcript.delta", role: "assistant", text: evt.delta }));
    return;
  }
  if (t === "response.audio_transcript.done") {
    onTranscript?.(evt.transcript ?? "", "assistant", true);
    sendWhenOpen(ws, JSON.stringify({ type: "transcript.final", role: "assistant", text: evt.transcript }));
    return;
  }
  if (t === "conversation.item.input_audio_transcription.completed") {
    onTranscript?.(evt.transcript ?? "", "user", true);
    sendWhenOpen(ws, JSON.stringify({ type: "transcript.final", role: "user", text: evt.transcript }));
    return;
  }
  if (t === "response.function_call_arguments.done") {
    let parsedArgs: Record<string, unknown> = {};
    try {
      parsedArgs = evt.arguments ? JSON.parse(evt.arguments) : {};
    } catch {}
    onToolCall?.(evt.name ?? "", parsedArgs, evt.call_id ?? "");
    sendWhenOpen(
      ws,
      JSON.stringify({
        type: "tool.call",
        name: evt.name,
        call_id: evt.call_id,
        arguments: evt.arguments,
      }),
    );
    return;
  }
  if (t === "error") {
    sendWhenOpen(ws, JSON.stringify({ type: "error", data: evt }));
    return;
  }
}

function sendWhenOpen(channel: WebSocket | RTCDataChannel, data: string) {
  if ((channel as any).readyState === 1 /* OPEN */) {
    channel.send(data);
    return;
  }
  const onOpen = () => {
    channel.send(data);
    channel.removeEventListener("open", onOpen as any);
  };
  channel.addEventListener("open", onOpen as any);
}

function ensureAudioElement(): HTMLAudioElement {
  let el = document.getElementById("voiceagentpy-remote-audio") as HTMLAudioElement | null;
  if (!el) {
    el = document.createElement("audio");
    el.id = "voiceagentpy-remote-audio";
    el.autoplay = true;
    el.style.display = "none";
    document.body.appendChild(el);
  }
  return el;
}

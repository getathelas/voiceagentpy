/**
 * Browser-side voice client.
 *
 * Two transports, picked by the backend's session response:
 *
 *   transport === "webrtc"     → OpenAI-style: POST SDP to provider, exchange answer.
 *   transport === "websocket"  → xAI-style: open WS, stream PCM16 base64 frames.
 *
 * Either way, a control WebSocket to the backend handles tool relay.
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

const PCM_SAMPLE_RATE = 24000;

export async function startVoiceSession(
  opts: StartVoiceSessionOpts,
): Promise<VoiceSessionHandle> {
  const { backendUrl, provider, metadata } = opts;

  opts.onState?.("connecting");

  const sessionResp = await fetch(`${backendUrl}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider, metadata: metadata ?? {} }),
  });
  if (!sessionResp.ok) {
    opts.onState?.("error", { stage: "mint-session", status: sessionResp.status });
    throw new Error(`Failed to mint session: ${sessionResp.status}`);
  }
  const session = (await sessionResp.json()) as SessionPayload;

  const wsUrl = backendUrl.replace(/^http/, "ws") + `/sessions/${session.id}/control`;
  const controlSocket = new WebSocket(wsUrl);

  if (session.transport === "websocket") {
    return startWebSocketTransport(session, controlSocket, opts, backendUrl);
  }
  return startWebRtcTransport(session, controlSocket, opts, backendUrl);
}

// ---------- WebRTC transport (OpenAI) ----------

async function startWebRtcTransport(
  session: SessionPayload,
  controlSocket: WebSocket,
  opts: StartVoiceSessionOpts,
  backendUrl: string,
): Promise<VoiceSessionHandle> {
  const { onTranscript, onToolCall, onToolResult, onState, onEvent } = opts;

  const pc = new RTCPeerConnection();
  const remoteAudio = ensureAudioElement();

  pc.ontrack = (e) => {
    remoteAudio.srcObject = e.streams[0];
    void remoteAudio.play().catch(() => {});
  };

  const dataChannel = pc.createDataChannel("oai-events");

  // Tool-result injection must wait for the response that contained the
  // function call to fully end (response.done) — otherwise the model
  // continues its filler narrative instead of speaking the tool data.
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

  controlSocket.onmessage = (ev) => {
    let parsed: any;
    try {
      parsed = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (parsed?.type === "tool.result") {
      const r = {
        name: parsed.name ?? "",
        call_id: parsed.call_id ?? "",
        result: parsed.result,
      };
      if (responseActive) {
        pendingToolResults.push(r);
      } else {
        injectToolResult(r);
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

// ---------- WebSocket transport (xAI Grok) ----------

async function startWebSocketTransport(
  session: SessionPayload,
  controlSocket: WebSocket,
  opts: StartVoiceSessionOpts,
  backendUrl: string,
): Promise<VoiceSessionHandle> {
  const { onTranscript, onToolCall, onToolResult, onState, onEvent } = opts;

  // xAI auth: pass token as a sec-websocket-protocol subprotocol because
  // browsers can't set Authorization headers on a WebSocket.
  const providerSocket = new WebSocket(session.url, [
    `xai-client-secret.${session.client_secret}`,
  ]);
  providerSocket.binaryType = "arraybuffer";

  const audioCtx = new AudioContext({ sampleRate: PCM_SAMPLE_RATE });
  let mic: MediaStream | null = null;
  let micSource: MediaStreamAudioSourceNode | null = null;
  let processor: ScriptProcessorNode | null = null;
  const playbackQueue = new PcmPlaybackQueue(audioCtx);

  providerSocket.onopen = async () => {
    try {
      mic = await navigator.mediaDevices.getUserMedia({ audio: true });
      micSource = audioCtx.createMediaStreamSource(mic);
      processor = audioCtx.createScriptProcessor(4096, 1, 1);
      processor.onaudioprocess = (ev) => {
        if (providerSocket.readyState !== WebSocket.OPEN) return;
        const input = ev.inputBuffer.getChannelData(0);
        const pcm16 = floatToPcm16(input);
        const b64 = arrayBufferToBase64(pcm16.buffer);
        providerSocket.send(
          JSON.stringify({ type: "input_audio_buffer.append", audio: b64 }),
        );
      };
      micSource.connect(processor);
      // ScriptProcessor only fires onaudioprocess while connected to a destination,
      // even though we don't actually want to hear the mic — use a muted sink.
      const muteSink = audioCtx.createGain();
      muteSink.gain.value = 0;
      processor.connect(muteSink);
      muteSink.connect(audioCtx.destination);
    } catch (err) {
      onState?.("error", { stage: "mic", err: String(err) });
    }
  };

  providerSocket.onmessage = (ev) => {
    let parsed: any;
    try {
      parsed = JSON.parse(typeof ev.data === "string" ? ev.data : "");
    } catch {
      return;
    }
    onEvent?.(parsed);

    const t: string = parsed?.type ?? "";
    if (t === "response.output_audio.delta" || t === "response.audio.delta") {
      const audio = parsed.delta ?? parsed.audio;
      if (typeof audio === "string") {
        playbackQueue.enqueueBase64Pcm16(audio);
      }
      return;
    }
    relayProviderEventToBackend(parsed, controlSocket, onTranscript, onToolCall);
  };

  providerSocket.onerror = (e) => onState?.("error", { stage: "provider-ws", err: e });
  providerSocket.onclose = () => onState?.("ended");

  controlSocket.onmessage = (ev) => {
    let parsed: any;
    try {
      parsed = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (parsed?.type === "tool.result") {
      onToolResult?.(parsed.name ?? "", parsed.result, parsed.call_id ?? "");
      const out = {
        type: "conversation.item.create",
        item: {
          type: "function_call_output",
          call_id: parsed.call_id,
          output: JSON.stringify(parsed.result ?? {}),
        },
      };
      sendWhenOpen(providerSocket, JSON.stringify(out));
      sendWhenOpen(providerSocket, JSON.stringify({ type: "response.create" }));
    }
  };

  controlSocket.onopen = () => onState?.("live");
  controlSocket.onclose = () => {
    if (providerSocket.readyState <= WebSocket.OPEN) providerSocket.close();
    onState?.("ended");
  };
  controlSocket.onerror = (e) => onState?.("error", e);

  const stop = async () => {
    try {
      processor?.disconnect();
    } catch {}
    try {
      micSource?.disconnect();
    } catch {}
    try {
      mic?.getTracks().forEach((t) => t.stop());
    } catch {}
    try {
      playbackQueue.stop();
    } catch {}
    try {
      await audioCtx.close();
    } catch {}
    try {
      providerSocket.close();
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

// ---------- Shared event relay ----------

function relayProviderEventToBackend(
  evt: any,
  ws: WebSocket,
  onTranscript: StartVoiceSessionOpts["onTranscript"],
  onToolCall: StartVoiceSessionOpts["onToolCall"],
) {
  const t: string = evt?.type ?? "";

  // Assistant transcript — OpenAI uses response.audio_transcript.*, xAI uses response.output_audio_transcript.*.
  if (t === "response.audio_transcript.delta" || t === "response.output_audio_transcript.delta") {
    onTranscript?.(evt.delta ?? "", "assistant", false);
    sendWhenOpen(ws, JSON.stringify({ type: "transcript.delta", role: "assistant", text: evt.delta }));
    return;
  }
  if (t === "response.audio_transcript.done" || t === "response.output_audio_transcript.done") {
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

// ---------- Audio helpers ----------

function floatToPcm16(input: Float32Array): Int16Array {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function arrayBufferToBase64(buf: ArrayBufferLike): string {
  const bytes = new Uint8Array(buf as ArrayBuffer);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function base64ToInt16(b64: string): Int16Array {
  const binary = atob(b64);
  const len = binary.length;
  const buf = new ArrayBuffer(len);
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < len; i++) bytes[i] = binary.charCodeAt(i);
  return new Int16Array(buf);
}

class PcmPlaybackQueue {
  private ctx: AudioContext;
  private nextStartTime = 0;
  private active: AudioBufferSourceNode[] = [];

  constructor(ctx: AudioContext) {
    this.ctx = ctx;
  }

  enqueueBase64Pcm16(b64: string) {
    const pcm = base64ToInt16(b64);
    if (pcm.length === 0) return;
    const float = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) float[i] = pcm[i] / 0x8000;
    const buffer = this.ctx.createBuffer(1, float.length, PCM_SAMPLE_RATE);
    buffer.copyToChannel(float, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.ctx.destination);
    const startAt = Math.max(this.nextStartTime, this.ctx.currentTime);
    src.start(startAt);
    this.nextStartTime = startAt + buffer.duration;
    this.active.push(src);
    src.onended = () => {
      this.active = this.active.filter((s) => s !== src);
    };
  }

  stop() {
    for (const src of this.active) {
      try {
        src.stop();
      } catch {}
    }
    this.active = [];
    this.nextStartTime = 0;
  }
}

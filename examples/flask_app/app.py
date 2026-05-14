"""Flask example for voiceagentpy.

Builds one VoiceAgent per provider whose API key is set (OPENAI_API_KEY ->
gpt-realtime, XAI_API_KEY -> grok-voice-latest). The frontend picks which to use
per session via the POST /sessions body; GET /providers lists what's
available.
"""

import json
import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sock import Sock

from voiceagentpy import VoiceAgent, mock_tool_response
from tools import TOOL_DEFINITIONS, TOOL_HANDLERS

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voiceagentpy.example")


PROVIDER_MODELS = {"openai": "gpt-realtime", "xai": "grok-voice-latest"}
PROVIDER_ENV_KEYS = {"openai": "OPENAI_API_KEY", "xai": "XAI_API_KEY"}


def build_agent(model: str) -> VoiceAgent:
    return VoiceAgent(
        model=model,
        instructions=(
            "You are a helpful support voice agent. Keep responses short.\n\n"
            "You have tools to look up user accounts and check the time. "
            "When the user asks something tool-relevant:\n"
            "  1. Call the tool immediately. Do NOT speak filler like "
            "'let me check', 'one moment', 'I'll look that up' — stay silent.\n"
            "  2. When the tool result is added to the conversation, speak "
            "the actual data from it.\n\n"
            "Never say you are 'still working on it', 'checking', 'looking up', "
            "or 'waiting'. If a tool result is in the conversation, its values "
            "are authoritative — verbalize them. Never claim something wasn't "
            "found when the result shows otherwise."
        ),
        voice="friendly-support",
        tools=TOOL_DEFINITIONS,
        tool_handlers=TOOL_HANDLERS,           # realistic mocks for known tools
        default_tool_handler=mock_tool_response,  # generic fallback for anything else
        event_handler=lambda e: log.info("event %s", e["type"]),
        finish_handler=lambda s: log.info("finished %s (%dms)", s["session_id"], s["duration_ms"]),
        turn_detection={"type": "server_vad"},
        input_audio_transcription={"model": "whisper-1"},  # so user speech appears in transcript
    )


agents: dict[str, VoiceAgent] = {
    pid: build_agent(model)
    for pid, model in PROVIDER_MODELS.items()
    if os.environ.get(PROVIDER_ENV_KEYS[pid])
}
if not agents:
    raise RuntimeError("Set OPENAI_API_KEY and/or XAI_API_KEY before starting.")
log.info("configured providers: %s", list(agents))

sessions: dict[str, VoiceAgent] = {}

app = Flask(__name__)
CORS(app, origins=os.environ.get("VOICE_AGENT_ALLOWED_ORIGINS", "http://localhost:3000").split(","))
sock = Sock(app)


@app.get("/providers")
def providers():
    return {"providers": [{"id": p, "model": PROVIDER_MODELS[p]} for p in agents]}


@app.post("/sessions")
def create_session():
    body = request.get_json(silent=True) or {}
    provider = body.get("provider") or next(iter(agents))
    if provider not in agents:
        return jsonify({"error": f"provider '{provider}' not configured"}), 400
    try:
        result = agents[provider].connect(transport="browser", metadata=body.get("metadata") or {})
    except Exception as e:
        # Surface upstream provider errors as JSON so the browser shows
        # the real cause instead of a CORS-shaped 500.
        log.exception("mint failed for provider=%s", provider)
        return jsonify({"error": str(e), "provider": provider}), 502
    sessions[result.id] = agents[provider]
    return result.to_dict()


@app.post("/sessions/<session_id>/end")
def end_session(session_id):
    agent = sessions.pop(session_id, None)
    if agent:
        agent.end_session(session_id)
    return {"ok": True}


@sock.route("/sessions/<session_id>/control")
def control(ws, session_id):
    agent = sessions.get(session_id)
    if agent is None:
        ws.send(json.dumps({"type": "error", "data": "unknown session"}))
        return
    ws.send(json.dumps({"type": "ready", "session_id": session_id}))
    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            response = agent.ingest_event(session_id, json.loads(raw))
            if response:
                ws.send(json.dumps(response))
    finally:
        sessions.pop(session_id, None)
        agent.end_session(session_id, reason="client_disconnect")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5050")), debug=True, use_reloader=False)

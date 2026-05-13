"""Example Flask backend for the voiceagentpy framework.

Builds one VoiceAgent per provider whose API key is set (OPENAI_API_KEY for
gpt-realtime, XAI_API_KEY for grok-voice). The frontend picks which provider
to use per session via the POST /sessions body and discovers what's
available via GET /providers.

Run:
    pip install -r requirements.txt
    export OPENAI_API_KEY=sk-...    # optional
    export XAI_API_KEY=xai-...      # optional
    python app.py
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sock import Sock

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from voiceagentpy import VoiceAgent

from tools import TOOL_DEFINITIONS, TOOL_HANDLERS


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("voiceagentpy.example")


INSTRUCTIONS = (
    "You are a helpful support voice agent. "
    "Keep responses short and conversational. "
    "Greet the user warmly when they say hello."
)


PROVIDER_MODELS: dict[str, str] = {
    "openai": "gpt-realtime",
    "xai": "grok-voice",
}


def _build_agent(provider: str) -> VoiceAgent:
    return VoiceAgent(
        model=PROVIDER_MODELS[provider],
        instructions=INSTRUCTIONS,
        voice="friendly-support",
        tools=TOOL_DEFINITIONS,
        tool_handlers=TOOL_HANDLERS,
        event_handler=lambda e: log.info("event: %s", e),
        finish_handler=lambda s: log.info(
            "finished session %s (%dms)", s["session_id"], s["duration_ms"]
        ),
        turn_detection={"type": "server_vad"},
    )


def create_app() -> Flask:
    app = Flask(__name__)
    sock = Sock(app)

    allowed = os.environ.get("VOICE_AGENT_ALLOWED_ORIGINS", "http://localhost:3000")
    CORS(app, resources={r"/*": {"origins": [o.strip() for o in allowed.split(",")]}})

    agents: dict[str, VoiceAgent] = {}
    if os.environ.get("OPENAI_API_KEY"):
        agents["openai"] = _build_agent("openai")
    if os.environ.get("XAI_API_KEY"):
        agents["xai"] = _build_agent("xai")

    if not agents:
        raise RuntimeError(
            "No provider API keys set. Set OPENAI_API_KEY and/or XAI_API_KEY."
        )

    log.info("configured providers: %s", list(agents.keys()))

    session_to_agent: dict[str, VoiceAgent] = {}

    @app.get("/health")
    def health() -> Any:
        return {"ok": True}

    @app.get("/providers")
    def providers() -> Any:
        return jsonify(
            {
                "providers": [
                    {"id": pid, "model": PROVIDER_MODELS[pid]} for pid in agents.keys()
                ]
            }
        )

    @app.post("/sessions")
    def create_session() -> Any:
        body = request.get_json(silent=True) or {}
        provider = (body.get("provider") if isinstance(body, dict) else None) or next(
            iter(agents.keys())
        )
        agent = agents.get(provider)
        if agent is None:
            return (
                jsonify({"error": f"provider '{provider}' is not configured on this server"}),
                400,
            )
        metadata = body.get("metadata") if isinstance(body, dict) else None
        if not isinstance(metadata, dict):
            metadata = {}
        result = agent.connect(transport="browser", metadata=metadata)
        session_to_agent[result.id] = agent
        return jsonify({**result.to_dict(), "provider_id": provider})

    @app.post("/sessions/<session_id>/end")
    def end_session(session_id: str) -> Any:
        body = request.get_json(silent=True) or {}
        reason = body.get("reason", "client_disconnect") if isinstance(body, dict) else "client_disconnect"
        agent = session_to_agent.pop(session_id, None)
        if agent is not None:
            agent.end_session(session_id, reason=reason)
        return jsonify({"ok": True})

    @sock.route("/sessions/<session_id>/control")
    def control(ws, session_id: str):  # noqa: ANN001
        log.info("control WS open session_id=%s", session_id)
        agent = session_to_agent.get(session_id)
        if agent is None or agent.get_session(session_id) is None:
            ws.send(json.dumps({"type": "error", "data": {"message": "unknown session"}}))
            return
        try:
            ws.send(json.dumps({"type": "ready", "session_id": session_id}))
            while True:
                raw = ws.receive()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    ws.send(json.dumps({"type": "error", "data": {"message": "invalid json"}}))
                    continue
                response = agent.ingest_event(session_id, msg)
                if response is not None:
                    ws.send(json.dumps(response))
        except Exception:  # noqa: BLE001
            log.exception("control WS error session_id=%s", session_id)
        finally:
            session_to_agent.pop(session_id, None)
            agent.end_session(session_id, reason="client_disconnect")
            log.info("control WS closed session_id=%s", session_id)

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", "5050"))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)

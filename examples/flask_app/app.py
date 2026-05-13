"""Example Flask backend for the voiceagentpy framework.

Run:
    pip install -e '..[flask]' python-dotenv
    export OPENAI_API_KEY=sk-...
    python app.py
"""

from __future__ import annotations

import logging
import os

from flask import Flask
from flask_cors import CORS

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from voiceagentpy import VoiceAgent

from tools import TOOL_DEFINITIONS, TOOL_HANDLERS


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("voiceagentpy.example")


def create_app() -> Flask:
    app = Flask(__name__)

    allowed = os.environ.get("VOICE_AGENT_ALLOWED_ORIGINS", "http://localhost:3000")
    CORS(app, resources={r"/*": {"origins": [o.strip() for o in allowed.split(",")]}})

    agent = VoiceAgent(
        model=os.environ.get("VOICE_AGENT_MODEL", "gpt-realtime"),
        instructions=(
            "You are a helpful support voice agent. "
            "Keep responses short and conversational. "
            "Greet the user warmly when they say hello."
        ),
        voice="friendly-support",
        tools=TOOL_DEFINITIONS,
        tool_handlers=TOOL_HANDLERS,
        event_handler=lambda e: log.info("event: %s", e),
        finish_handler=lambda s: log.info("finished session %s (%dms)", s["session_id"], s["duration_ms"]),
        turn_detection={"type": "server_vad"},
    )

    app.register_blueprint(agent.flask_blueprint())

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)

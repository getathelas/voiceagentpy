"""Flask blueprint that wires a VoiceAgent to HTTP + WebSocket routes.

Requires `flask` and `flask-sock`. Install via `pip install voiceagentpy[flask]`.

Exposes:
  POST /sessions               -> mint an ephemeral session
  WS   /sessions/<id>/control  -> control channel: tool calls + events relay
  POST /sessions/<id>/end      -> explicit end-of-session hook
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

try:
    from flask import Blueprint, jsonify, request
    from flask_sock import Sock
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "Flask extension requires the `flask` extra. Install with "
        "`pip install voiceagentpy[flask]`."
    ) from e


if TYPE_CHECKING:
    from .agent import VoiceAgent


logger = logging.getLogger(__name__)


def build_blueprint(agent: "VoiceAgent", *, name: str = "voiceagentpy") -> Blueprint:
    bp = Blueprint(name, __name__)
    sock = Sock()

    @bp.record_once
    def _init(setup_state):
        sock.init_app(setup_state.app)

    @bp.post("/sessions")
    def create_session():
        body = request.get_json(silent=True) or {}
        metadata = body.get("metadata") if isinstance(body, dict) else None
        if metadata is None and isinstance(body, dict):
            # Allow top-level keys as metadata for convenience.
            metadata = {k: v for k, v in body.items() if k != "metadata"}
        result = agent.connect(transport="browser", metadata=metadata or {})
        return jsonify(result.to_dict())

    @bp.post("/sessions/<session_id>/end")
    def end_session(session_id: str):
        body = request.get_json(silent=True) or {}
        reason = body.get("reason", "client_disconnect")
        agent.end_session(session_id, reason=reason)
        return jsonify({"ok": True})

    @sock.route("/sessions/<session_id>/control")
    def control(ws, session_id: str):  # noqa: ANN001
        logger.info("control WS open session_id=%s", session_id)
        session = agent.get_session(session_id)
        if session is None:
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
            logger.exception("control WS error session_id=%s", session_id)
        finally:
            agent.end_session(session_id, reason="client_disconnect")
            logger.info("control WS closed session_id=%s", session_id)

    return bp

"""FastAPI telephony routes — TestClient, no real provider/Twilio network."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from voiceagentpy import VoiceAgent
from voiceagentpy.fastapi_ext import build_fastapi_app


class FakeProvider:
    name = "fake"

    def supported_models(self):
        return ["grok-voice-latest"]

    def normalize_voice(self, v):
        return v

    def mint_session(self, cfg, sid, metadata=None):
        raise AssertionError("telephony must not mint")


class FakeConn:
    """Stands in for a RealtimeConnection in the media WS test."""

    def __init__(self):
        self.audio: list[bytes] = []
        self.connected = False
        self.closed = False

    async def connect(self):
        self.connected = True

    async def send_audio(self, pcm16):
        self.audio.append(pcm16)

    async def send_tool_result(self, call_id, result):
        pass

    async def events(self):
        await asyncio.Event().wait()  # stay open until torn down
        yield  # pragma: no cover

    async def close(self):
        self.closed = True


def _agent():
    return VoiceAgent(model="grok-voice-latest", provider=FakeProvider())


def test_inbound_returns_connect_stream_twiml_and_creates_session():
    agent = _agent()
    app = build_fastapi_app(
        agent, public_base_url="https://h.ngrok.app", validate_signature=False
    )
    client = TestClient(app)
    r = client.post(
        "/twilio/voice",
        data={"From": "+1444", "To": "+1555", "CallSid": "CA1"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert "<Connect><Stream" in r.text
    assert "wss://h.ngrok.app/twilio/media/" in r.text
    # a session was created and tracked
    assert len(agent._sessions) == 1
    # the caller is attributed on the session so finish summaries are useful
    sess = next(iter(agent._sessions.values()))
    assert sess.metadata == {
        "from": "+1444",
        "to": "+1555",
        "call_sid": "CA1",
        "direction": "inbound",
    }
    assert sess.summary()["metadata"]["from"] == "+1444"


def test_outbound_twiml_callback():
    app = build_fastapi_app(_agent(), public_base_url="https://h.ngrok.app")
    client = TestClient(app)
    r = client.get("/twilio/voice/sess_42")
    assert r.status_code == 200
    assert 'url="wss://h.ngrok.app/twilio/media/sess_42"' in r.text


def test_inbound_signature_enforced():
    agent = _agent()
    app = build_fastapi_app(
        agent, public_base_url="https://h.ngrok.app", validate_signature=True
    )
    client = TestClient(app)
    # no signature -> 403
    bad = client.post("/twilio/voice", data={"From": "+1", "To": "+2"})
    assert bad.status_code == 403

    # correct signature -> 200 (env auth token is empty here; sign with "")
    import os

    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    params = {"From": "+1", "To": "+2"}
    url = "https://h.ngrok.app/twilio/voice"
    payload = url + "".join(k + params[k] for k in sorted(params))
    sig = base64.b64encode(
        hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()
    ok = client.post(
        "/twilio/voice", data=params, headers={"X-Twilio-Signature": sig}
    )
    assert ok.status_code == 200


def test_control_plane_http_mirror():
    agent = _agent()
    # create a tracked telephony session to ingest into
    res = agent.connect(
        transport="twilio", call_details={"public_base_url": "https://h.app"}
    )
    app = build_fastapi_app(agent, public_base_url="https://h.app")
    client = TestClient(app)

    agent.tool_handlers["lookup"] = lambda **kw: {"found": kw}
    r = client.post(
        f"/sessions/{res.id}/events",
        json={"type": "tool.call", "name": "lookup", "call_id": "c1",
              "arguments": {"q": 9}},
    )
    assert r.status_code == 200
    assert r.json() == {
        "type": "tool.result",
        "call_id": "c1",
        "name": "lookup",
        "result": {"found": {"q": 9}},
    }
    e = client.post(f"/sessions/{res.id}/end", json={"reason": "caller_hangup"})
    assert e.json() == {"ok": True}
    assert agent.get_session(res.id) is None  # ended


def test_media_ws_bridges_twilio_audio_to_provider():
    agent = _agent()
    conn = FakeConn()
    app = build_fastapi_app(
        agent,
        public_base_url="https://h.app",
        connection_factory=lambda sid: conn,
    )
    client = TestClient(app)
    mulaw = base64.b64encode(bytes([0xFF] * 160)).decode()
    with client.websocket_connect("/twilio/media/sess_ws") as ws:
        ws.send_text(json.dumps({"event": "start", "streamSid": "MZ1",
                                 "start": {"streamSid": "MZ1"}}))
        ws.send_text(json.dumps({"event": "media", "media": {"payload": mulaw}}))
        ws.send_text(json.dumps({"event": "stop"}))
    # bridge connected to the provider and forwarded the caller frame
    assert conn.connected is True
    assert conn.closed is True
    assert len(conn.audio) == 1 and len(conn.audio[0]) == 160 * 2 * 3

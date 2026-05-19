"""agent.call() + Twilio REST + TwilioTransport — no network."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from voiceagentpy import VoiceAgent
from voiceagentpy.session import SessionCredentials
from voiceagentpy.transports import build_transport
from voiceagentpy.transports.twilio import TwilioTransport, build_stream_twiml
from voiceagentpy.telephony.twilio_rest import (
    TwilioConfig,
    place_call,
    resolve_twilio_config,
)


class FakeProvider:
    name = "fake"

    def supported_models(self):
        return ["grok-voice-latest"]

    def normalize_voice(self, v):
        return v

    def mint_session(self, cfg, sid, metadata=None):
        raise AssertionError("call() must not mint an ephemeral key")


class FakeResp:
    def __init__(self, status_code=201, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class FakeHTTP:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def post(self, url, data=None, auth=None):
        self.calls.append({"url": url, "data": data, "auth": auth})
        return self._resp


# --- twilio_rest -----------------------------------------------------------

def test_resolve_config_from_env(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC1")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+1444")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://x.ngrok.app/")
    cfg = resolve_twilio_config()
    assert cfg == TwilioConfig("AC1", "tok", "+1444", "https://x.ngrok.app")


def test_resolve_config_overrides_and_missing(monkeypatch):
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_FROM_NUMBER", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="Twilio config missing"):
        resolve_twilio_config()
    cfg = resolve_twilio_config(
        {
            "account_sid": "AC2",
            "auth_token": "t2",
            "from": "+1999",
            "public_base_url": "https://y.app",
        }
    )
    assert cfg.account_sid == "AC2" and cfg.from_number == "+1999"


def test_place_call_builds_request_and_parses():
    http = FakeHTTP(FakeResp(201, {"sid": "CA123", "status": "queued"}))
    cfg = TwilioConfig("AC1", "tok", "+1444", "https://x.app")
    out = place_call(
        config=cfg, to="+15551234567", voice_url="https://x.app/twilio/voice/s1", http=http
    )
    assert out == {"sid": "CA123", "status": "queued"}
    call = http.calls[0]
    assert call["url"].endswith("/Accounts/AC1/Calls.json")
    assert call["data"] == {
        "To": "+15551234567",
        "From": "+1444",
        "Url": "https://x.app/twilio/voice/s1",
    }
    assert call["auth"] == ("AC1", "tok")


def test_place_call_raises_on_error():
    http = FakeHTTP(FakeResp(401, text="bad creds"))
    with pytest.raises(RuntimeError, match="401"):
        place_call(
            config=TwilioConfig("AC1", "x", "+1", "https://x"),
            to="+1",
            voice_url="https://x/v",
            http=http,
        )


# --- TwilioTransport -------------------------------------------------------

def _creds(sid="sess_abc"):
    return SessionCredentials(
        id=sid,
        provider="xai",
        model="grok-voice-latest",
        url="",
        client_secret="",
        expires_at=datetime.now(timezone.utc),
    )


def test_transport_registered():
    assert isinstance(build_transport("twilio"), TwilioTransport)


def test_transport_prepare_with_public_base_url():
    payload = TwilioTransport().prepare(
        _creds("sess_abc"), {"public_base_url": "https://h.ngrok.app/"}
    )
    assert payload["media_ws_url"] == "wss://h.ngrok.app/twilio/media/sess_abc"
    assert payload["voice_url"] == "https://h.ngrok.app/twilio/voice/sess_abc"
    assert "<Connect><Stream" in payload["twiml"]
    assert 'value="sess_abc"' in payload["twiml"]


def test_transport_prepare_without_base_is_relative():
    payload = TwilioTransport().prepare(_creds("s1"), None)
    assert payload["media_path"] == "/twilio/media/s1"
    assert "twiml" not in payload  # can't build absolute wss without a host


def test_twiml_escapes_attributes():
    twiml = build_stream_twiml("wss://h/x?a=1&b=2", "s&1")
    assert "&amp;" in twiml and "&amp;" in twiml.split("session_id")[0]


# --- agent.call() ----------------------------------------------------------

def test_agent_call_places_outbound_and_tracks_session():
    events = []
    agent = VoiceAgent(
        model="grok-voice-latest",
        provider=FakeProvider(),
        event_handler=events.append,
    )
    http = FakeHTTP(FakeResp(201, {"sid": "CA999", "status": "queued"}))
    res = agent.call(
        call_details={
            "to": "+14085987929",
            "from": "+15139515830",
            "account_sid": "AC1",
            "auth_token": "tok",
            "public_base_url": "https://h.ngrok.app",
            "_http": http,
        }
    )
    assert res.call_sid == "CA999"
    assert res.status == "queued"
    assert res.id.startswith("sess_")
    assert res.extra["voice_url"] == f"https://h.ngrok.app/twilio/voice/{res.id}"
    assert res.extra["from"] == "+15139515830"
    # session is tracked and a session.started (outbound) event fired
    assert agent.get_session(res.id) is not None
    started = [e for e in events if e["type"] == "session.started"]
    assert started and started[0]["data"]["direction"] == "outbound"
    # Twilio got the right dial
    assert http.calls[0]["data"]["To"] == "+14085987929"
    assert http.calls[0]["data"]["Url"] == res.extra["voice_url"]


def test_agent_call_requires_to():
    agent = VoiceAgent(model="grok-voice-latest", provider=FakeProvider())
    with pytest.raises(ValueError, match="must include 'to'"):
        agent.call(call_details={"from": "+1"})


def test_agent_call_rejects_non_twilio_transport():
    agent = VoiceAgent(model="grok-voice-latest", provider=FakeProvider())
    with pytest.raises(ValueError, match="telephony-only"):
        agent.call(transport="browser", call_details={"to": "+1"})

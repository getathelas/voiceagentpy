"""Smoke tests that exercise the public surface without hitting any network."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

import pytest

from voiceagentpy import VoiceAgent, VoiceClient, SessionCredentials, register_provider
from voiceagentpy.providers.base import AgentConfig


class FakeProvider:
    name = "fake"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or "fake-key"
        self.calls: list[AgentConfig] = []

    def supported_models(self) -> list[str]:
        return ["fake-voice"]

    def normalize_voice(self, voice: str | None) -> str | None:
        return voice

    def mint_session(
        self,
        agent_config: AgentConfig,
        session_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> SessionCredentials:
        self.calls.append(agent_config)
        return SessionCredentials(
            id=session_id,
            provider=self.name,
            model=agent_config.model,
            url="https://example.test/realtime?model=" + agent_config.model,
            client_secret="ek_fake_secret",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            extra={"transport": "webrtc"},
        )


def _make_agent(**overrides: Any) -> tuple[VoiceAgent, FakeProvider]:
    provider = FakeProvider()
    kwargs = dict(
        model="fake-voice",
        instructions="be terse",
        voice="friendly-support",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup_user",
                    "description": "x",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_handlers={"lookup_user": lambda **kw: {"ok": True, "args": kw}},
        provider=provider,
    )
    kwargs.update(overrides)
    return VoiceAgent(**kwargs), provider


def test_connect_returns_credentials():
    agent, fake = _make_agent()
    result = agent.connect(transport="browser")
    assert result.provider == "fake"
    assert result.client_secret == "ek_fake_secret"
    assert result.transport == "browser"
    assert fake.calls and fake.calls[0].instructions == "be terse"


def test_event_handler_fires_on_session_start():
    events: list[dict[str, Any]] = []
    agent, _ = _make_agent(event_handler=events.append)
    agent.connect()
    types = [e["type"] for e in events]
    assert "session.started" in types


def test_finish_handler_fires_with_summary():
    summaries: list[dict[str, Any]] = []
    agent, _ = _make_agent(finish_handler=summaries.append)
    result = agent.connect()
    agent.end_session(result.id, reason="test_end")
    assert summaries and summaries[0]["session_id"] == result.id
    assert summaries[0]["ended_reason"] == "test_end"


def test_tool_call_executes_and_records():
    events: list[dict[str, Any]] = []
    agent, _ = _make_agent(event_handler=events.append)
    result = agent.connect()
    response = agent.ingest_event(
        result.id,
        {"type": "tool.call", "name": "lookup_user", "call_id": "c_1", "arguments": {"phone": "+1"}},
    )
    assert response is not None
    assert response["type"] == "tool.result"
    assert response["result"]["ok"] is True
    assert response["result"]["args"]["phone"] == "+1"

    session = agent.get_session(result.id)
    assert session is not None
    assert session.tool_calls and session.tool_calls[0]["name"] == "lookup_user"

    tool_types = [e["type"] for e in events]
    assert "tool.called" in tool_types
    assert "tool.completed" in tool_types


def test_tool_call_with_string_arguments():
    agent, _ = _make_agent()
    result = agent.connect()
    response = agent.ingest_event(
        result.id,
        {
            "type": "response.function_call_arguments.done",
            "name": "lookup_user",
            "call_id": "c_2",
            "arguments": '{"phone": "+15551234"}',
        },
    )
    assert response is not None
    assert response["result"]["args"]["phone"] == "+15551234"


def test_transcript_final_event_appended_to_session():
    agent, _ = _make_agent()
    result = agent.connect()
    agent.ingest_event(
        result.id,
        {"type": "transcript.final", "role": "user", "text": "hello"},
    )
    session = agent.get_session(result.id)
    assert session is not None
    assert session.transcript == [{"role": "user", "text": "hello"}]


def test_unknown_model_raises():
    with pytest.raises(ValueError):
        VoiceAgent(model="who-knows-what")


def test_register_provider_then_resolve():
    register_provider("fake", FakeProvider)
    agent = VoiceAgent(model="fake-voice", provider="fake")
    assert agent.provider.name == "fake"


def test_per_session_on_event_overrides_agent_handler():
    agent_events: list[dict[str, Any]] = []
    session_events: list[dict[str, Any]] = []
    agent, _ = _make_agent(event_handler=agent_events.append)
    result = agent.connect(onEvent=session_events.append)
    agent.ingest_event(
        result.id,
        {"type": "transcript.final", "role": "user", "text": "hi"},
    )
    types = [e["type"] for e in session_events]
    assert "session.started" in types
    assert "transcript.final" in types
    assert agent_events == []


def test_per_session_on_finish_overrides_agent_handler():
    agent_summaries: list[dict[str, Any]] = []
    session_summaries: list[dict[str, Any]] = []
    agent, _ = _make_agent(finish_handler=agent_summaries.append)
    result = agent.connect(onFinish=session_summaries.append)
    agent.end_session(result.id, reason="test_end")
    assert len(session_summaries) == 1
    assert session_summaries[0]["session_id"] == result.id
    assert session_summaries[0]["ended_reason"] == "test_end"
    assert agent_summaries == []


def test_voice_client_chat_completions_shape():
    register_provider("fake", FakeProvider)
    client = VoiceClient(provider="fake")
    resp = client.chat.completions.create(
        model="fake-voice",
        messages=[{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}],
        tools=None,
    )
    assert resp.client_secret == "ek_fake_secret"
    assert resp.provider == "fake"

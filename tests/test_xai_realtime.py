"""xAI server-side realtime connection — no network (fake WebSocket)."""

from __future__ import annotations

import base64
import json

from voiceagentpy.providers.base import AgentConfig, RealtimeBridgeProvider
from voiceagentpy.providers.xai_grok import XAIGrokProvider, XAIRealtimeConnection


class FakeWS:
    def __init__(self, incoming: list[str]) -> None:
        self.sent: list[str] = []
        self._incoming = list(incoming)
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)


def _conn(incoming=None, session_config=None):
    ws = FakeWS(incoming or [])

    async def connector():
        return ws

    c = XAIRealtimeConnection(
        api_key="k",
        ws_url="wss://api.x.ai/v1/realtime?model=grok-voice-latest",
        session_config=session_config or {"voice": "ara"},
        connector=connector,
    )
    return c, ws


async def test_connect_sends_session_update():
    c, ws = _conn(session_config={"voice": "ara", "instructions": "hi"})
    await c.connect()
    assert json.loads(ws.sent[0]) == {
        "type": "session.update",
        "session": {"voice": "ara", "instructions": "hi"},
    }


async def test_send_audio_is_base64_append():
    c, ws = _conn()
    await c.connect()
    await c.send_audio(b"\x01\x02\x03\x04")
    msg = json.loads(ws.sent[-1])
    assert msg["type"] == "input_audio_buffer.append"
    assert base64.b64decode(msg["audio"]) == b"\x01\x02\x03\x04"


async def test_send_tool_result_emits_output_then_response():
    c, ws = _conn()
    await c.connect()
    await c.send_tool_result("call_1", {"ok": True})
    out = json.loads(ws.sent[-2])
    assert out["type"] == "conversation.item.create"
    assert out["item"]["call_id"] == "call_1"
    assert json.loads(out["item"]["output"]) == {"ok": True}
    assert json.loads(ws.sent[-1]) == {"type": "response.create"}


async def test_events_normalizes_audio_transcript_and_tool_call():
    incoming = [
        json.dumps({"type": "response.output_audio.delta",
                    "delta": base64.b64encode(b"PCMDATA").decode()}),
        json.dumps({"type": "response.audio_transcript.delta", "delta": "Hel"}),
        json.dumps({"type": "response.audio_transcript.done", "transcript": "Hello"}),
        json.dumps({"type": "input_audio_buffer.speech_started"}),
        json.dumps({"type": "response.function_call_arguments.done",
                    "name": "lookup", "call_id": "c1", "arguments": "{\"q\":1}"}),
        "not-json",
        json.dumps({"type": "some.unknown.event"}),
    ]
    c, _ = _conn(incoming)
    await c.connect()
    got = [e async for e in c.events()]

    assert got[0] == {"type": "audio.delta", "pcm16": b"PCMDATA"}
    assert got[1] == {"type": "transcript.delta", "text": "Hel", "role": "assistant"}
    assert got[2] == {"type": "transcript.final", "text": "Hello", "role": "assistant"}
    assert got[3] == {"type": "input.speech_started"}
    assert got[4] == {"type": "tool.call", "name": "lookup",
                      "call_id": "c1", "arguments": "{\"q\":1}"}
    # bad json + unknown event are silently dropped
    assert len(got) == 5


async def test_close_is_idempotent_and_closes_ws():
    c, ws = _conn()
    await c.connect()
    await c.close()
    await c.close()
    assert ws.closed is True


def test_provider_implements_bridge_protocol_and_shares_session_config():
    p = XAIGrokProvider(api_key="k")
    assert isinstance(p, RealtimeBridgeProvider)
    cfg = AgentConfig(
        model="grok-voice-latest", instructions="be nice", voice="friendly-support",
        tools=None, temperature=None, turn_detection=None,
        input_audio_transcription=None, modalities=None, extra={},
    )
    conn = p.open_realtime(cfg, "sess_1")
    assert isinstance(conn, XAIRealtimeConnection)
    sc = p.build_session_config(cfg)
    assert sc["voice"] == "ara"  # friendly-support -> ara
    assert sc["audio"]["output"]["format"]["rate"] == 24000

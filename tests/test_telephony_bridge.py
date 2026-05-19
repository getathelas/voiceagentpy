"""Media bridge + control plane — no network, all fakes."""

from __future__ import annotations

import asyncio
import base64
import json


from voiceagentpy.telephony.audio import mulaw8k_to_pcm16_24k
from voiceagentpy.telephony.bridge import MediaBridge
from voiceagentpy.telephony.control_plane import ControlPlane, InProcessControlPlane


async def _nosleep(_: float) -> None:
    return None


class FakeTwilioWS:
    def __init__(self, script: list, release: asyncio.Event | None = None) -> None:
        self._script = list(script)
        self._release = release
        self.sent: list[dict] = []

    async def receive_text(self) -> str:
        if self._script:
            item = self._script.pop(0)
            if item is None:  # block until released, then hang up
                assert self._release is not None
                await self._release.wait()
                return json.dumps({"event": "stop"})
            return json.dumps(item)
        await asyncio.Event().wait()  # idle forever (cancelled on shutdown)

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))


class FakeProvider:
    def __init__(self, events: list[dict], stay_open: bool = False) -> None:
        self._events = events
        self._stay_open = stay_open
        self.audio: list[bytes] = []
        self.tool_results: list[tuple] = []
        self.connected = False
        self.closed = False
        self.drained = asyncio.Event()

    async def connect(self) -> None:
        self.connected = True

    async def send_audio(self, pcm16: bytes) -> None:
        self.audio.append(pcm16)

    async def send_tool_result(self, call_id: str, result) -> None:
        self.tool_results.append((call_id, result))

    async def events(self):
        for e in self._events:
            yield e
        self.drained.set()
        if self._stay_open:
            await asyncio.Event().wait()  # real streams stay open until teardown

    async def close(self) -> None:
        self.closed = True


class FakeCP:
    def __init__(self) -> None:
        self.ingested: list[dict] = []
        self.ended: tuple | None = None

    async def ingest(self, sid: str, ev: dict):
        self.ingested.append(ev)
        if ev.get("type") == "tool.call":
            return {"type": "tool.result", "call_id": ev["call_id"], "result": {"ok": True}}
        return None

    async def end(self, sid: str, reason: str) -> None:
        self.ended = (sid, reason)


def _bridge(twilio, provider, cp):
    return MediaBridge(
        session_id="sess_x",
        twilio_ws=twilio,
        provider=provider,
        control_plane=cp,
        sleep=_nosleep,
    )


async def test_full_run_lifecycle_caller_audio_and_tool_relay():
    mulaw_in = bytes([0xFF, 0x7F] * 80)  # 160 μ-law bytes (20 ms)
    provider = FakeProvider(
        [
            {"type": "audio.delta", "pcm16": b"\x00\x01" * 480},
            {"type": "transcript.final", "text": "hello", "role": "user"},
            {"type": "tool.call", "name": "lookup", "call_id": "c1", "arguments": "{}"},
        ],
        stay_open=True,
    )
    tw = FakeTwilioWS(
        [
            {"event": "start", "streamSid": "MZ1", "start": {"streamSid": "MZ1"}},
            {"event": "media", "media": {"payload": base64.b64encode(mulaw_in).decode()}},
            None,  # block until provider drained, then 'stop'
        ],
        release=provider.drained,
    )
    cp = FakeCP()
    await asyncio.wait_for(_bridge(tw, provider, cp).run(), timeout=2.0)

    assert provider.connected and provider.closed
    # caller audio forwarded, transcoded μ-law 8k -> PCM16 24k
    assert provider.audio == [mulaw8k_to_pcm16_24k(mulaw_in)]
    # control plane saw transcript + tool call, but NOT raw audio
    types = [e["type"] for e in cp.ingested]
    assert types == ["transcript.final", "tool.call"]
    # tool.result routed back to the model
    assert provider.tool_results == [("c1", {"ok": True})]
    # lifecycle closed out with the hangup reason
    assert cp.ended == ("sess_x", "caller_hangup")


async def test_provider_to_twilio_queues_audio_frames():
    provider = FakeProvider([{"type": "audio.delta", "pcm16": b"\x10\x20" * 480}])
    cp = FakeCP()
    b = _bridge(FakeTwilioWS([]), provider, cp)
    await b._provider_to_twilio()
    frames = []
    while not b._out_q.empty():
        frames.append(b._out_q.get_nowait())
    assert frames, "expected μ-law frames queued for Twilio"
    assert all(len(f) == 160 for f in frames)


async def test_barge_in_drains_queue_and_sends_clear():
    provider = FakeProvider([{"type": "input.speech_started"}])
    tw = FakeTwilioWS([])
    b = _bridge(tw, provider, FakeCP())
    b._stream_sid = "MZ1"
    await b._out_q.put(b"x" * 160)
    await b._out_q.put(b"y" * 160)
    await b._provider_to_twilio()
    assert b._out_q.empty()  # buffered audio dropped
    assert {"event": "clear", "streamSid": "MZ1"} in tw.sent


async def test_twilio_sender_emits_media_messages():
    tw = FakeTwilioWS([])
    b = _bridge(tw, FakeProvider([]), FakeCP())
    b._stream_sid = "MZ1"
    await b._out_q.put(b"AAAA")
    await b._out_q.put(b"BBBB")
    task = asyncio.create_task(b._twilio_sender())
    for _ in range(20):
        await asyncio.sleep(0)
    b._shutdown("done")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    media = [m for m in tw.sent if m.get("event") == "media"]
    payloads = [base64.b64decode(m["media"]["payload"]) for m in media]
    assert payloads == [b"AAAA", b"BBBB"]


async def test_inprocess_control_plane_delegates_to_agent():
    class FakeAgent:
        def __init__(self):
            self.calls = []

        def ingest_event(self, sid, ev):
            self.calls.append(("ingest", sid, ev))
            return {"type": "tool.result", "call_id": "c", "result": 1}

        def end_session(self, sid, reason):
            self.calls.append(("end", sid, reason))

    agent = FakeAgent()
    cp = InProcessControlPlane(agent)
    assert isinstance(cp, ControlPlane)
    out = await cp.ingest("s1", {"type": "tool.call"})
    assert out == {"type": "tool.result", "call_id": "c", "result": 1}
    await cp.end("s1", "caller_hangup")
    assert agent.calls == [
        ("ingest", "s1", {"type": "tool.call"}),
        ("end", "s1", "caller_hangup"),
    ]

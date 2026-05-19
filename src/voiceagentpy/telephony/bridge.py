"""The media bridge: one Twilio Media Stream <-> one provider realtime socket.

Per call, three coroutines run concurrently:

  * ``_twilio_to_provider`` — read Twilio frames; caller audio (μ-law 8k) is
    transcoded to PCM16 24k and pushed to the provider.
  * ``_provider_to_twilio`` — read provider events; model audio is transcoded
    back to μ-law 8k and queued for Twilio; transcripts/tool-calls/errors go
    to the ControlPlane (a returned ``tool.result`` is sent back to the model);
    barge-in flushes the outbound queue and tells Twilio to ``clear``.
  * ``_twilio_sender`` — paces queued frames out at 20 ms so the buffer stays
    small (small buffer == snappy barge-in).

Shutdown is whoever-finishes-first: Twilio ``stop``/disconnect or the provider
socket closing tears the whole bridge down and notifies the control plane.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Awaitable, Callable, Protocol

from ..providers.base import RealtimeConnection
from .audio import iter_frames, mulaw8k_to_pcm16_24k, pcm16_24k_to_mulaw8k
from .control_plane import ControlPlane

logger = logging.getLogger(__name__)

# 20 ms per Twilio frame (8 kHz μ-law, 160 bytes).
_FRAME_INTERVAL = 0.02


class TwilioWebSocket(Protocol):
    """The slice of a WebSocket the bridge needs. Starlette/FastAPI's
    `WebSocket` already satisfies this (`receive_text`, `send_text`)."""

    async def receive_text(self) -> str: ...

    async def send_text(self, data: str) -> None: ...


class MediaBridge:
    def __init__(
        self,
        *,
        session_id: str,
        twilio_ws: TwilioWebSocket,
        provider: RealtimeConnection,
        control_plane: ControlPlane,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.session_id = session_id
        self._tw = twilio_ws
        self._provider = provider
        self._cp = control_plane
        self._sleep = sleep
        self._stream_sid: str | None = None
        self._out_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._reason = "caller_hangup"

    async def run(self) -> None:
        await self._provider.connect()
        tasks = [
            asyncio.create_task(self._twilio_to_provider(), name="tw->prov"),
            asyncio.create_task(self._provider_to_twilio(), name="prov->tw"),
            asyncio.create_task(self._twilio_sender(), name="tw-sender"),
        ]
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._provider.close()
            await self._cp.end(self.session_id, self._reason)

    def _shutdown(self, reason: str) -> None:
        if not self._stop.is_set():
            self._reason = reason
            self._stop.set()

    # ----- Twilio -> provider --------------------------------------------------

    async def _twilio_to_provider(self) -> None:
        try:
            while not self._stop.is_set():
                msg = json.loads(await self._tw.receive_text())
                event = msg.get("event")
                if event == "start":
                    start = msg.get("start") or {}
                    self._stream_sid = msg.get("streamSid") or start.get("streamSid")
                    logger.info(
                        "twilio stream start sid=%s session=%s",
                        self._stream_sid,
                        self.session_id,
                    )
                elif event == "media":
                    payload = (msg.get("media") or {}).get("payload")
                    if payload:
                        mulaw = base64.b64decode(payload)
                        await self._provider.send_audio(mulaw8k_to_pcm16_24k(mulaw))
                elif event == "stop":
                    self._shutdown("caller_hangup")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.info("twilio receive ended: %r", e)
            self._shutdown("twilio_disconnect")

    # ----- provider -> Twilio --------------------------------------------------

    async def _provider_to_twilio(self) -> None:
        try:
            async for evt in self._provider.events():
                kind = evt.get("type")
                if kind == "audio.delta":
                    mulaw = pcm16_24k_to_mulaw8k(evt["pcm16"])
                    for frame in iter_frames(mulaw):
                        await self._out_q.put(frame)
                elif kind == "input.speech_started":
                    # Barge-in: drop everything we haven't sent and tell Twilio
                    # to discard what it has buffered.
                    self._drain_output()
                    await self._send_clear()
                elif kind == "audio.done":
                    continue
                else:
                    resp = await self._cp.ingest(self.session_id, evt)
                    if resp and resp.get("type") == "tool.result":
                        await self._provider.send_tool_result(
                            resp.get("call_id", ""), resp.get("result")
                        )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.info("provider events ended: %r", e)
        finally:
            self._shutdown("provider_disconnect")

    # ----- paced sender --------------------------------------------------------

    async def _twilio_sender(self) -> None:
        try:
            while not self._stop.is_set():
                frame = await self._out_q.get()
                if self._stream_sid is None:
                    continue
                await self._tw.send_text(
                    json.dumps(
                        {
                            "event": "media",
                            "streamSid": self._stream_sid,
                            "media": {
                                "payload": base64.b64encode(frame).decode("ascii")
                            },
                        }
                    )
                )
                await self._sleep(_FRAME_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.info("twilio sender ended: %r", e)
            self._shutdown("twilio_disconnect")

    def _drain_output(self) -> None:
        try:
            while True:
                self._out_q.get_nowait()
        except asyncio.QueueEmpty:
            pass

    async def _send_clear(self) -> None:
        if not self._stream_sid:
            return
        try:
            await self._tw.send_text(
                json.dumps({"event": "clear", "streamSid": self._stream_sid})
            )
        except Exception:  # noqa: BLE001
            pass

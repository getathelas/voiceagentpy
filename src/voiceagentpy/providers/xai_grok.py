"""xAI Grok Voice provider.

Mints an ephemeral session for xAI's voice-agent endpoint. We mirror the same
shape we use for OpenAI: server mints a short-lived key, client connects direct.

NOTE: xAI's voice-agent API surface is evolving; the endpoint and field names
below reflect their public docs at the time of writing. If they change, only
this file needs to be updated.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, AsyncIterator

import httpx

from ..session import SessionCredentials
from .base import AgentConfig


logger = logging.getLogger(__name__)


XAI_SESSIONS_URL = "https://api.x.ai/v1/realtime/client_secrets"
XAI_REALTIME_WS_URL = "wss://api.x.ai/v1/realtime"
DEFAULT_TOKEN_TTL_SECONDS = 300

_VOICE_MAP: dict[str, str] = {
    "friendly-support": "ara",
    "calm-narrator": "ranger",
    "energetic": "neo",
    "neutral": "ara",
    "warm": "celeste",
}


class XAIGrokProvider:
    name = "xai"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = XAI_SESSIONS_URL,
        realtime_ws_url: str = XAI_REALTIME_WS_URL,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("XAI_API_KEY")
        if not self.api_key:
            raise ValueError(
                "xAI Grok provider requires an API key. Set XAI_API_KEY or pass api_key=..."
            )
        self._base_url = base_url
        self._realtime_ws_url = realtime_ws_url
        self._http = http_client or httpx.Client(timeout=30.0)

    def supported_models(self) -> list[str]:
        return ["grok-voice-latest", "grok-voice-think-fast-1.0", "grok-voice-fast-1.0"]

    def normalize_voice(self, voice: str | None) -> str | None:
        if voice is None:
            return None
        return _VOICE_MAP.get(voice, voice)

    def mint_session(
        self,
        agent_config: AgentConfig,
        session_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> SessionCredentials:
        # Per xAI docs, the mint endpoint's `session` field only accepts `model`.
        # The rest of the config is delivered via a `session.update` message that
        # the client sends right after the WebSocket opens — we expose it in
        # `extra.session_config` for the frontend.
        body: dict[str, Any] = {
            "expires_after": {"seconds": DEFAULT_TOKEN_TTL_SECONDS},
            "session": {"model": agent_config.model},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = self._http.post(self._base_url, json=body, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"xAI realtime session mint failed ({resp.status_code}): {resp.text}"
            )
        data = resp.json()

        client_secret = data.get("value") or data.get("client_secret") or data.get("token")
        if isinstance(client_secret, dict):
            client_secret = client_secret.get("value")
        if not client_secret:
            raise RuntimeError(
                f"xAI did not return an ephemeral token. Response: {data!r}"
            )

        expires_at_ts = data.get("expires_at")
        if expires_at_ts:
            expires_at = datetime.fromtimestamp(int(expires_at_ts), tz=timezone.utc)
        else:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_TOKEN_TTL_SECONDS)

        ws_url = f"{self._realtime_ws_url}?model={agent_config.model}"
        session_config = self.build_session_config(agent_config)

        return SessionCredentials(
            id=session_id,
            provider=self.name,
            model=agent_config.model,
            url=ws_url,
            client_secret=client_secret,
            expires_at=expires_at,
            extra={
                "transport": "websocket",
                "auth_header": "Authorization",
                "auth_scheme": "Bearer",
                "session_config": session_config,
            },
        )

    def build_session_config(self, agent_config: AgentConfig) -> dict[str, Any]:
        """The `session.update` payload xAI expects after the WS opens.

        Shared by `mint_session` (browser sends it itself) and `open_realtime`
        (the bridge sends it). Single source of truth for instructions/voice/
        tools/turn-detection/audio format."""
        cfg: dict[str, Any] = {}
        if agent_config.instructions:
            cfg["instructions"] = agent_config.instructions
        voice = self.normalize_voice(agent_config.voice)
        if voice:
            cfg["voice"] = voice
        if agent_config.tools:
            cfg["tools"] = agent_config.tools
        if agent_config.temperature is not None:
            cfg["temperature"] = agent_config.temperature
        if agent_config.turn_detection is not None:
            cfg["turn_detection"] = agent_config.turn_detection
        # PCM16 @ 24 kHz both ways. The telephony bridge transcodes Twilio's
        # μ-law 8 kHz to/from this; the browser captures at this rate directly.
        cfg["audio"] = {
            "input": {"format": {"type": "audio/pcm", "rate": 24000}},
            "output": {"format": {"type": "audio/pcm", "rate": 24000}},
        }
        if agent_config.extra:
            cfg.update(agent_config.extra)
        return cfg

    def open_realtime(
        self, agent_config: AgentConfig, session_id: str
    ) -> "XAIRealtimeConnection":
        """Open a server-side realtime connection for the telephony bridge.

        Unlike `mint_session`, this does NOT mint an ephemeral key — server to
        server we authenticate the WebSocket with the main API key directly
        (the ephemeral-key / subprotocol dance only exists because browsers
        can't set Authorization headers)."""
        return XAIRealtimeConnection(
            api_key=self.api_key,
            ws_url=f"{self._realtime_ws_url}?model={agent_config.model}",
            session_config=self.build_session_config(agent_config),
        )


class XAIRealtimeConnection:
    """Server-side xAI realtime WebSocket, normalized for the media bridge.

    Implements the `RealtimeConnection` protocol. xAI's realtime schema is
    still evolving (see module docstring); the wire event names below are the
    integration seam — if xAI changes them, this is the only place to touch.
    """

    # Inbound (caller) audio buffer append.
    _APPEND = "input_audio_buffer.append"

    def __init__(
        self,
        *,
        api_key: str,
        ws_url: str,
        session_config: dict[str, Any],
        connector: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._ws_url = ws_url
        self._session_config = session_config
        # Injectable for tests; defaults to the `websockets` library lazily so
        # the core package doesn't hard-require it.
        self._connector = connector
        self._ws: Any = None

    async def connect(self) -> None:
        if self._connector is None:
            try:
                import websockets  # noqa: PLC0415
            except ImportError as e:  # pragma: no cover
                raise ImportError(
                    "The telephony bridge needs the `websockets` library. "
                    "Install with `pip install voiceagentpy[fastapi]`."
                ) from e
            self._connector = lambda: websockets.connect(
                self._ws_url,
                additional_headers={"Authorization": f"Bearer {self._api_key}"},
                max_size=None,
            )
        self._ws = await self._connector()
        await self._send({"type": "session.update", "session": self._session_config})
        logger.info("xAI realtime WS connected: %s", self._ws_url)

    async def _send(self, msg: dict[str, Any]) -> None:
        await self._ws.send(json.dumps(msg))

    async def send_audio(self, pcm16: bytes) -> None:
        await self._send(
            {"type": self._APPEND, "audio": base64.b64encode(pcm16).decode("ascii")}
        )

    async def send_tool_result(self, call_id: str, result: Any) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result if isinstance(result, str) else json.dumps(result),
                },
            }
        )
        await self._send({"type": "response.create"})

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield bridge-normalized events. Audio -> {"type":"audio.delta",
        "pcm16": bytes}; barge-in -> {"type":"input.speech_started"}; the rest
        shaped for VoiceAgent.ingest_event."""
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            kind = msg.get("type", "")

            if kind in ("response.output_audio.delta", "response.audio.delta"):
                b64 = msg.get("delta") or msg.get("audio") or ""
                if b64:
                    yield {"type": "audio.delta", "pcm16": base64.b64decode(b64)}
            elif kind in ("response.output_audio.done", "response.audio.done"):
                yield {"type": "audio.done"}
            elif kind == "input_audio_buffer.speech_started":
                yield {"type": "input.speech_started"}
            elif kind in ("response.audio_transcript.delta",):
                yield {"type": "transcript.delta", "text": msg.get("delta", ""),
                       "role": "assistant"}
            elif kind in ("response.audio_transcript.done",):
                yield {"type": "transcript.final", "text": msg.get("transcript", ""),
                       "role": "assistant"}
            elif kind == "conversation.item.input_audio_transcription.completed":
                yield {"type": "transcript.final", "text": msg.get("transcript", ""),
                       "role": "user"}
            elif kind == "response.function_call_arguments.done":
                yield {
                    "type": "tool.call",
                    "name": msg.get("name", ""),
                    "call_id": msg.get("call_id") or msg.get("id", ""),
                    "arguments": msg.get("arguments", "{}"),
                }
            elif kind == "error":
                yield {"type": "error", "data": msg}
            # Unknown events are dropped on purpose — xAI emits many lifecycle
            # messages the bridge doesn't act on.

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

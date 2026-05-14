"""xAI Grok Voice provider.

Mints an ephemeral session for xAI's voice-agent endpoint. We mirror the same
shape we use for OpenAI: server mints a short-lived key, client connects direct.

NOTE: xAI's voice-agent API surface is evolving; the endpoint and field names
below reflect their public docs at the time of writing. If they change, only
this file needs to be updated.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

from ..session import SessionCredentials
from .base import AgentConfig, Provider


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

        session_config: dict[str, Any] = {}
        if agent_config.instructions:
            session_config["instructions"] = agent_config.instructions
        voice = self.normalize_voice(agent_config.voice)
        if voice:
            session_config["voice"] = voice
        if agent_config.tools:
            session_config["tools"] = agent_config.tools
        if agent_config.temperature is not None:
            session_config["temperature"] = agent_config.temperature
        if agent_config.turn_detection is not None:
            session_config["turn_detection"] = agent_config.turn_detection
        # Tell xAI to encode PCM at the rate the browser is capturing at.
        session_config["audio"] = {
            "input": {"format": {"type": "audio/pcm", "rate": 24000}},
            "output": {"format": {"type": "audio/pcm", "rate": 24000}},
        }
        if agent_config.extra:
            session_config.update(agent_config.extra)

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

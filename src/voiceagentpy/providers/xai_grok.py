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


XAI_SESSIONS_URL = "https://api.x.ai/v1/voice/sessions"
XAI_WEBRTC_URL = "https://api.x.ai/v1/voice/connect"

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
        webrtc_url: str = XAI_WEBRTC_URL,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("XAI_API_KEY")
        if not self.api_key:
            raise ValueError(
                "xAI Grok provider requires an API key. Set XAI_API_KEY or pass api_key=..."
            )
        self._base_url = base_url
        self._webrtc_url = webrtc_url
        self._http = http_client or httpx.Client(timeout=30.0)

    def supported_models(self) -> list[str]:
        return ["grok-voice", "grok-voice-1"]

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
        body: dict[str, Any] = {"model": agent_config.model}
        if agent_config.instructions:
            body["instructions"] = agent_config.instructions
        voice = self.normalize_voice(agent_config.voice)
        if voice:
            body["voice"] = voice
        if agent_config.tools:
            body["tools"] = agent_config.tools
        if agent_config.temperature is not None:
            body["temperature"] = agent_config.temperature
        if agent_config.turn_detection is not None:
            body["turn_detection"] = agent_config.turn_detection
        if agent_config.extra:
            body.update(agent_config.extra)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = self._http.post(self._base_url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        client_secret = (
            data.get("client_secret")
            or (data.get("session") or {}).get("client_secret")
            or data.get("token")
        )
        if isinstance(client_secret, dict):
            client_secret = client_secret.get("value")
        if not client_secret:
            raise RuntimeError(
                f"xAI did not return a client_secret. Response: {data!r}"
            )

        expires_at_ts = data.get("expires_at")
        if expires_at_ts:
            expires_at = datetime.fromtimestamp(int(expires_at_ts), tz=timezone.utc)
        else:
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)

        return SessionCredentials(
            id=session_id,
            provider=self.name,
            model=agent_config.model,
            url=data.get("url") or self._webrtc_url,
            client_secret=client_secret,
            expires_at=expires_at,
            extra={
                "transport": "webrtc",
                "auth_header": "Authorization",
                "auth_scheme": "Bearer",
                "xai_session_id": data.get("id"),
            },
        )

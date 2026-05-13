"""OpenAI Realtime provider.

Mints an ephemeral client secret via the OpenAI Realtime sessions endpoint so the
browser can connect direct to OpenAI over WebRTC. We never proxy audio.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

from ..session import SessionCredentials
from .base import AgentConfig, Provider


REALTIME_SESSIONS_URL = "https://api.openai.com/v1/realtime/sessions"
REALTIME_WEBRTC_URL = "https://api.openai.com/v1/realtime"

# Normalized voice id -> OpenAI voice id.
# OpenAI Realtime voices: alloy, ash, ballad, coral, echo, sage, shimmer, verse.
_VOICE_MAP: dict[str, str] = {
    "friendly-support": "coral",
    "calm-narrator": "sage",
    "energetic": "verse",
    "neutral": "alloy",
    "warm": "shimmer",
}


class OpenAIRealtimeProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = REALTIME_SESSIONS_URL,
        webrtc_url: str = REALTIME_WEBRTC_URL,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError(
                "OpenAI Realtime provider requires an API key. "
                "Set OPENAI_API_KEY or pass api_key=..."
            )
        self._base_url = base_url
        self._webrtc_url = webrtc_url
        self._http = http_client or httpx.Client(timeout=30.0)

    def supported_models(self) -> list[str]:
        return ["gpt-realtime", "gpt-realtime-2", "gpt-4o-realtime-preview"]

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
        body: dict[str, Any] = {
            "model": agent_config.model,
        }
        if agent_config.instructions:
            body["instructions"] = agent_config.instructions
        voice = self.normalize_voice(agent_config.voice)
        if voice:
            body["voice"] = voice
        if agent_config.tools:
            body["tools"] = _to_openai_tools(agent_config.tools)
        if agent_config.temperature is not None:
            body["temperature"] = agent_config.temperature
        if agent_config.turn_detection is not None:
            body["turn_detection"] = agent_config.turn_detection
        if agent_config.input_audio_transcription is not None:
            body["input_audio_transcription"] = agent_config.input_audio_transcription
        if agent_config.modalities:
            body["modalities"] = agent_config.modalities
        if agent_config.extra:
            body.update(agent_config.extra)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "OpenAI-Beta": "realtime=v1",
        }
        resp = self._http.post(self._base_url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        client_secret_obj = data.get("client_secret") or {}
        client_secret = (
            client_secret_obj.get("value")
            if isinstance(client_secret_obj, dict)
            else client_secret_obj
        )
        expires_at_ts = (
            client_secret_obj.get("expires_at")
            if isinstance(client_secret_obj, dict)
            else None
        )
        if expires_at_ts:
            expires_at = datetime.fromtimestamp(int(expires_at_ts), tz=timezone.utc)
        else:
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)

        if not client_secret:
            raise RuntimeError(
                f"OpenAI Realtime did not return a client_secret. Response: {data!r}"
            )

        webrtc_url = f"{self._webrtc_url}?model={agent_config.model}"

        return SessionCredentials(
            id=session_id,
            provider=self.name,
            model=agent_config.model,
            url=webrtc_url,
            client_secret=client_secret,
            expires_at=expires_at,
            extra={
                "transport": "webrtc",
                "auth_header": "Authorization",
                "auth_scheme": "Bearer",
                "openai_session_id": data.get("id"),
            },
        )


def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accept either OpenAI Chat-style ({type:function, function:{...}}) or flat
    realtime-style ({type:function, name, description, parameters}). Realtime
    wants the flat shape — normalize here."""
    flat: list[dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            flat.append(
                {
                    "type": "function",
                    "name": fn.get("name"),
                    "description": fn.get("description"),
                    "parameters": fn.get("parameters", {}),
                }
            )
        else:
            flat.append(t)
    return flat

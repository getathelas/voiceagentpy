"""OpenAI Realtime provider.

Mints an ephemeral client secret via the OpenAI Realtime API so the browser
can connect direct to OpenAI over WebRTC. We never proxy audio.

Targets the GA `/v1/realtime/client_secrets` endpoint and the
gpt-realtime / gpt-realtime-2 session schema:

    {
      "session": {
        "type": "realtime",
        "model": "gpt-realtime-2",
        "instructions": "...",
        "audio": {
          "input":  { "turn_detection": {...}, "transcription": {...}, "format": {...} },
          "output": { "voice": "...",          "format": {...} }
        },
        "tools": [...],
        "tool_choice": "auto",
        "output_modalities": ["audio"]
      }
    }
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

from ..session import SessionCredentials
from .base import AgentConfig


REALTIME_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
# GA WebRTC endpoint is /v1/realtime/calls (not /v1/realtime), and the
# ?model=... query string returns 400 — the model is taken from the
# ephemeral secret's session config we set during minting.
REALTIME_WEBRTC_URL = "https://api.openai.com/v1/realtime/calls"

# Normalized voice id -> OpenAI voice id.
# Realtime voices: alloy, ash, ballad, coral, echo, marin, sage, shimmer, verse.
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
        base_url: str = REALTIME_CLIENT_SECRETS_URL,
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
        session_cfg: dict[str, Any] = {
            "type": "realtime",
            "model": agent_config.model,
        }
        if agent_config.instructions:
            session_cfg["instructions"] = agent_config.instructions
        if agent_config.tools:
            session_cfg["tools"] = _to_openai_tools(agent_config.tools)
        if agent_config.temperature is not None:
            session_cfg["temperature"] = agent_config.temperature
        if agent_config.modalities:
            # New API renamed `modalities` -> `output_modalities`.
            session_cfg["output_modalities"] = agent_config.modalities

        # Audio config is nested in the new schema.
        audio_in: dict[str, Any] = {}
        audio_out: dict[str, Any] = {}
        voice = self.normalize_voice(agent_config.voice)
        if voice:
            audio_out["voice"] = voice
        if agent_config.turn_detection is not None:
            audio_in["turn_detection"] = agent_config.turn_detection
        if agent_config.input_audio_transcription is not None:
            audio_in["transcription"] = agent_config.input_audio_transcription
        audio_cfg: dict[str, Any] = {}
        if audio_in:
            audio_cfg["input"] = audio_in
        if audio_out:
            audio_cfg["output"] = audio_out
        if audio_cfg:
            session_cfg["audio"] = audio_cfg

        if agent_config.extra:
            session_cfg.update(agent_config.extra)

        body = {"session": session_cfg}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = self._http.post(self._base_url, json=body, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"OpenAI Realtime session mint failed ({resp.status_code}): {resp.text}"
            )
        data = resp.json()

        # Response can have the secret under several shapes — be lenient.
        secret_obj = data.get("client_secret") or data
        if isinstance(secret_obj, dict):
            client_secret = (
                secret_obj.get("value")
                or secret_obj.get("secret")
                or data.get("value")
            )
            expires_at_ts = secret_obj.get("expires_at") or data.get("expires_at")
        else:
            client_secret = secret_obj
            expires_at_ts = data.get("expires_at")

        if not client_secret:
            raise RuntimeError(
                f"OpenAI Realtime did not return a client_secret. Response: {data!r}"
            )

        if expires_at_ts:
            expires_at = datetime.fromtimestamp(int(expires_at_ts), tz=timezone.utc)
        else:
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)

        webrtc_url = self._webrtc_url

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
                "openai_session_id": data.get("id") or (
                    data.get("session", {}).get("id") if isinstance(data.get("session"), dict) else None
                ),
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

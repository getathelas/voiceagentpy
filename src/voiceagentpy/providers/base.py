"""Provider interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..session import SessionCredentials


@dataclass
class AgentConfig:
    """Provider-agnostic config passed from VoiceAgent into Provider.mint_session."""

    model: str
    instructions: str | None
    voice: str | None
    tools: list[dict[str, Any]] | None
    temperature: float | None
    turn_detection: dict[str, Any] | None
    input_audio_transcription: dict[str, Any] | None
    modalities: list[str] | None
    extra: dict[str, Any]


@runtime_checkable
class Provider(Protocol):
    name: str

    def supported_models(self) -> list[str]: ...

    def normalize_voice(self, voice: str | None) -> str | None: ...

    def mint_session(
        self,
        agent_config: AgentConfig,
        session_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> SessionCredentials: ...

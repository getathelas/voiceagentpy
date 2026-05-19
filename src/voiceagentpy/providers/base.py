"""Provider interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol, runtime_checkable

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


@runtime_checkable
class RealtimeConnection(Protocol):
    """A live, server-side realtime session the telephony bridge pumps audio
    through. Distinct from `mint_session` (which hands a browser an ephemeral
    key to connect *itself*) — here Python holds the socket.

    `events()` yields normalized dicts. Audio is surfaced as
    ``{"type": "audio.delta", "pcm16": <bytes>}`` (PCM16 LE 24 kHz). Barge-in
    as ``{"type": "input.speech_started"}``. Everything else is shaped so it
    can be passed straight to ``VoiceAgent.ingest_event`` (transcripts,
    ``tool.call``, ``error``).
    """

    async def connect(self) -> None: ...

    async def send_audio(self, pcm16: bytes) -> None:
        """Send caller audio to the model. `pcm16` is PCM16 LE mono 24 kHz."""
        ...

    async def send_tool_result(self, call_id: str, result: Any) -> None: ...

    def events(self) -> AsyncIterator[dict[str, Any]]: ...

    async def close(self) -> None: ...


@runtime_checkable
class RealtimeBridgeProvider(Protocol):
    """Optional provider capability: open a server-side realtime connection for
    the telephony media bridge. Providers that only support browser-direct
    (ephemeral key) connections don't implement this."""

    def open_realtime(
        self, agent_config: AgentConfig, session_id: str
    ) -> RealtimeConnection: ...

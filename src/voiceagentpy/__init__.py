"""voiceagentpy — OpenAI-style client library for realtime voice agents."""

from __future__ import annotations

from .agent import VoiceAgent, mock_tool_response
from .client import VoiceClient, RealtimeResponse
from .events import Event
from .providers import (
    AgentConfig,
    Provider,
    OpenAIRealtimeProvider,
    XAIGrokProvider,
    build_provider,
    register_provider,
    resolve_provider_name,
)
from .session import Session, SessionCredentials
from .transports import BrowserTransport, Transport, build_transport


__version__ = "0.1.0"

__all__ = [
    "VoiceAgent",
    "mock_tool_response",
    "VoiceClient",
    "RealtimeResponse",
    "Event",
    "Session",
    "SessionCredentials",
    "AgentConfig",
    "Provider",
    "OpenAIRealtimeProvider",
    "XAIGrokProvider",
    "BrowserTransport",
    "Transport",
    "build_provider",
    "build_transport",
    "register_provider",
    "resolve_provider_name",
    "__version__",
]

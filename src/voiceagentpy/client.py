"""OpenAI-SDK-shaped client for retrofit users.

```python
from voiceagentpy import VoiceClient
client = VoiceClient(api_key="...")
resp = client.chat.completions.create(model="gpt-realtime-2", messages=[...], tools=[...])
# resp.client_secret, resp.url, resp.session_id
```

Most users should prefer `VoiceAgent` directly; this exists so projects with
existing `openai.chat.completions.create(...)` calls can do a near-textual
swap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agent import VoiceAgent


@dataclass
class RealtimeResponse:
    session_id: str
    provider: str
    model: str
    url: str
    client_secret: str
    expires_at: str
    transport: str = "browser"

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "provider": self.provider,
            "model": self.model,
            "url": self.url,
            "client_secret": self.client_secret,
            "expires_at": self.expires_at,
            "transport": self.transport,
        }


class _Completions:
    def __init__(self, parent: "VoiceClient") -> None:
        self._parent = parent

    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
        voice: str | None = None,
        temperature: float | None = None,
        **extra: Any,
    ) -> RealtimeResponse:
        # Compose instructions: explicit instructions wins, else stitch from
        # any `system` message present in `messages` (OpenAI Chat convention).
        if instructions is None and messages:
            sys_parts = [
                m.get("content", "")
                for m in messages
                if isinstance(m, dict) and m.get("role") == "system"
            ]
            if sys_parts:
                instructions = "\n\n".join(p for p in sys_parts if p)

        agent = VoiceAgent(
            model=model,
            instructions=instructions,
            voice=voice,
            tools=tools,
            temperature=temperature,
            api_key=self._parent.api_key,
            provider=self._parent._provider,
            **extra,
        )
        result = agent.connect(transport="browser")
        return RealtimeResponse(
            session_id=result.id,
            provider=result.provider,
            model=result.model,
            url=result.url,
            client_secret=result.client_secret,
            expires_at=result.expires_at,
            transport=result.transport,
        )


class _Chat:
    def __init__(self, parent: "VoiceClient") -> None:
        self.completions = _Completions(parent)


class VoiceClient:
    """OpenAI-SDK-shaped wrapper. Each `create(...)` call mints a fresh session."""

    def __init__(self, api_key: str | None = None, *, provider: str | None = None) -> None:
        self.api_key = api_key
        self._provider = provider
        self.chat = _Chat(self)

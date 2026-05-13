"""Session data structures for the voiceagentpy framework."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable
import uuid


def _new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:24]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SessionCredentials:
    """Returned by `Provider.mint_session`. Browser uses these to connect direct."""

    id: str
    provider: str
    model: str
    url: str
    client_secret: str
    expires_at: datetime
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "model": self.model,
            "url": self.url,
            "client_secret": self.client_secret,
            "expires_at": self.expires_at.isoformat(),
            **self.extra,
        }


@dataclass
class Session:
    """In-memory record of a live session, kept by the VoiceAgent."""

    id: str = field(default_factory=_new_session_id)
    credentials: SessionCredentials | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=_now)
    ended_at: datetime | None = None
    ended_reason: str | None = None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    event_handler: Callable[..., Any] | None = field(default=None, repr=False, compare=False)
    finish_handler: Callable[..., Any] | None = field(default=None, repr=False, compare=False)

    def duration_ms(self) -> int:
        end = self.ended_at or _now()
        return int((end - self.started_at).total_seconds() * 1000)

    def summary(self) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "duration_ms": self.duration_ms(),
            "transcript": list(self.transcript),
            "tool_calls": list(self.tool_calls),
            "ended_reason": self.ended_reason or "client_disconnect",
            "metadata": dict(self.metadata),
            "started_at": self.started_at.isoformat(),
            "ended_at": (self.ended_at or _now()).isoformat(),
        }

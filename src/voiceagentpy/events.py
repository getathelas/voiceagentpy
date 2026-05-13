"""Event types emitted by the voiceagentpy framework."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


EventType = Literal[
    "session.started",
    "session.ended",
    "transcript.delta",
    "transcript.final",
    "tool.called",
    "tool.completed",
    "audio.started",
    "audio.ended",
    "error",
]


@dataclass
class Event:
    type: EventType
    session_id: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "session_id": self.session_id,
            "timestamp": self.timestamp.isoformat(),
            "data": dict(self.data),
        }

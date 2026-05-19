"""Minimal tool set for the telephony example."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "lookup_user",
            "description": "Look up a user account by phone number.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string", "description": "E.164 phone number"}
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "current_time",
            "description": "Get the current server time in ISO 8601.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def lookup_user(phone: str | None = None, **_: Any) -> dict[str, Any]:
    return {
        "found": True,
        "phone": phone,
        "name": "Jordan Rivera",
        "plan": "Pro",
        "status": "active",
    }


def current_time(**_: Any) -> dict[str, Any]:
    return {"now": datetime.now(timezone.utc).isoformat()}


TOOL_HANDLERS = {"lookup_user": lookup_user, "current_time": current_time}

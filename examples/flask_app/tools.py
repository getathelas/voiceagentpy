"""Example tool definitions + mock handlers for the Flask example."""

from __future__ import annotations

from typing import Any, Callable


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "lookup_user",
            "description": "Look up a user account by phone number or email.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string", "description": "E.164 phone number"},
                    "email": {"type": "string", "description": "Email address"},
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
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]

# Fixed payloads for demos — no backend or clock.
_MOCK_USER = {"found": True, "name": "Avery Park", "plan": "pro", "tier_notes": "mock data"}
_MOCK_NOW_ISO = "2026-05-13T18:30:00+00:00"


def mock_lookup_user(phone: str | None = None, email: str | None = None) -> dict[str, Any]:
    if phone:
        return {**_MOCK_USER, "phone": phone}
    if email:
        return {**_MOCK_USER, "email": email}
    return {"found": False, "error": "phone or email required"}


def mock_current_time() -> dict[str, str]:
    return {"now": _MOCK_NOW_ISO}


TOOL_HANDLERS: dict[str, Callable[..., Any]] = {
    "lookup_user": mock_lookup_user,
    "current_time": mock_current_time,
}

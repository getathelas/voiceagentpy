"""Example tool definitions + handlers for the Flask example."""

from __future__ import annotations

from typing import Any


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


def lookup_user(phone: str | None = None, email: str | None = None) -> dict[str, Any]:
    # Stub implementation — return a deterministic fake record for the demo.
    if phone:
        return {"found": True, "name": "Avery Park", "phone": phone, "plan": "pro"}
    if email:
        return {"found": True, "name": "Avery Park", "email": email, "plan": "pro"}
    return {"found": False, "error": "phone or email required"}


def current_time() -> dict[str, str]:
    from datetime import datetime, timezone

    return {"now": datetime.now(timezone.utc).isoformat()}


TOOL_HANDLERS = {
    "lookup_user": lookup_user,
    "current_time": current_time,
}

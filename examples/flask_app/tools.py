"""Tool definitions + realistic mock handlers for the Flask example.

The example wires both `tool_handlers=TOOL_HANDLERS` (specific, realistic
mocks below) AND `default_tool_handler=mock_tool_response` (library-level
fallback) — specific handlers always win. Realistic-shaped mock data lets
the realtime model verbalize a useful response; generic stubs make it stall.
"""

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


_MOCK_USER = {
    "found": True,
    "name": "Avery Park",
    "plan": "pro",
    "joined": "2024-03-12",
    "open_tickets": 0,
}


def mock_lookup_user(phone: str | None = None, email: str | None = None) -> dict[str, Any]:
    if phone:
        return {**_MOCK_USER, "phone": phone}
    if email:
        return {**_MOCK_USER, "email": email}
    return {"found": False, "error": "phone or email required"}


def mock_current_time() -> dict[str, str]:
    return {"now": "2026-05-13T18:30:00+00:00", "timezone": "UTC"}


TOOL_HANDLERS: dict[str, Callable[..., Any]] = {
    "lookup_user": mock_lookup_user,
    "current_time": mock_current_time,
}

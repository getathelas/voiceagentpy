"""Tool definitions for the Flask example.

No handlers here — `app.py` wires `default_tool_handler=mock_tool_response`
so every defined tool is auto-mocked. Replace with a real
`tool_handlers={...}` dict in `app.py` when you want real implementations.
"""

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

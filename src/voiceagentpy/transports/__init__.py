"""Transport implementations."""

from __future__ import annotations

from .base import Transport
from .browser import BrowserTransport
from .twilio import TwilioTransport


_TRANSPORTS: dict[str, type] = {
    "browser": BrowserTransport,
    "twilio": TwilioTransport,
}


def build_transport(name: str) -> Transport:
    try:
        cls = _TRANSPORTS[name]
    except KeyError as e:
        raise ValueError(
            f"Unknown transport {name!r}. Available: {list(_TRANSPORTS)}."
        ) from e
    return cls()


__all__ = ["Transport", "BrowserTransport", "TwilioTransport", "build_transport"]

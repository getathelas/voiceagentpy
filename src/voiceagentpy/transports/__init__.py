"""Transport implementations."""

from __future__ import annotations

from .base import Transport
from .browser import BrowserTransport


_TRANSPORTS: dict[str, type] = {
    "browser": BrowserTransport,
}


def build_transport(name: str) -> Transport:
    try:
        cls = _TRANSPORTS[name]
    except KeyError as e:
        raise ValueError(
            f"Unknown transport {name!r}. Available: {list(_TRANSPORTS)}. "
            "Twilio is deferred to v2."
        ) from e
    return cls()


__all__ = ["Transport", "BrowserTransport", "build_transport"]

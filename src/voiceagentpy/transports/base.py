"""Transport interface. v1 only ships BrowserTransport; Twilio reserved for v2."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..session import SessionCredentials


@runtime_checkable
class Transport(Protocol):
    name: str

    def prepare(self, credentials: SessionCredentials, call_details: dict[str, Any] | None) -> dict[str, Any]: ...

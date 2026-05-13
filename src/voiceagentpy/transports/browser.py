"""Browser transport: WebRTC + ephemeral key, returned to frontend as-is."""

from __future__ import annotations

from typing import Any

from ..session import SessionCredentials


class BrowserTransport:
    name = "browser"

    def prepare(
        self,
        credentials: SessionCredentials,
        call_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = credentials.to_dict()
        payload["transport"] = "browser"
        # Surface the wire protocol the provider chose (webrtc vs websocket) so
        # the browser knows which connect path to take. Falls back to webrtc.
        payload["wire_protocol"] = credentials.extra.get("transport", "webrtc")
        return payload

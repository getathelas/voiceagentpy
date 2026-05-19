"""Twilio transport: turns a session into the TwiML + media-stream wiring.

Unlike `BrowserTransport` (hands the browser an ephemeral key to dial the
provider itself), Twilio needs us to (a) tell Twilio to open a Media Stream
WebSocket back to *our* server and (b) host the bridge on the other end. This
class produces (a) — the `<Connect><Stream>` TwiML and the derived URLs. The
bridge itself lives in `voiceagentpy.telephony`.
"""

from __future__ import annotations

from typing import Any
from xml.sax.saxutils import quoteattr

from ..session import SessionCredentials


def build_stream_twiml(stream_ws_url: str, session_id: str) -> str:
    """`<Connect><Stream>` TwiML — bidirectional μ-law 8 kHz media to our WS.

    `<Connect>` (not `<Start>`) makes the stream bidirectional and keeps the
    call alive for its duration. The session id rides along as a custom
    `<Parameter>` so the media WS can correlate even if the URL is rewritten.
    """
    url_attr = quoteattr(stream_ws_url)
    sid_attr = quoteattr(session_id)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f"<Stream url={url_attr}>"
        f'<Parameter name="session_id" value={sid_attr}/>'
        "</Stream></Connect></Response>"
    )


def _ws_base(public_base_url: str) -> str:
    base = public_base_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :]
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :]
    return base


class TwilioTransport:
    name = "twilio"

    def prepare(
        self,
        credentials: SessionCredentials,
        call_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cd = call_details or {}
        session_id = credentials.id
        base = (cd.get("public_base_url") or "").rstrip("/")

        media_path = f"/twilio/media/{session_id}"
        voice_path = f"/twilio/voice/{session_id}"
        payload: dict[str, Any] = {
            "transport": "twilio",
            "session_id": session_id,
            "media_path": media_path,
            "voice_path": voice_path,
        }
        if base:
            media_ws_url = f"{_ws_base(base)}{media_path}"
            payload["media_ws_url"] = media_ws_url
            payload["voice_url"] = f"{base}{voice_path}"
            payload["twiml"] = build_stream_twiml(media_ws_url, session_id)
        return payload

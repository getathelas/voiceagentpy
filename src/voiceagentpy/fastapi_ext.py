"""FastAPI integration for the Twilio telephony transport.

Requires the `fastapi` extra: `pip install voiceagentpy[fastapi]`.

Routes:
  POST   /twilio/voice                  inbound webhook -> <Connect><Stream> TwiML
  *      /twilio/voice/{session_id}      outbound TwiML callback (agent.call())
  WS     /twilio/media/{session_id}      the media bridge (Twilio <-> provider)
  POST   /sessions/{id}/events           control-plane HTTP mirror (prod split)
  POST   /sessions/{id}/end              control-plane HTTP mirror (prod split)

The browser transport keeps using the existing Flask blueprint; this module is
telephony-only and deliberately separate.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from typing import Any, Callable

try:
    from fastapi import APIRouter, FastAPI, Request, Response, WebSocket
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "The FastAPI extension requires the `fastapi` extra. Install with "
        "`pip install voiceagentpy[fastapi]`."
    ) from e

from .providers.base import RealtimeBridgeProvider, RealtimeConnection
from .telephony.bridge import MediaBridge
from .telephony.control_plane import ControlPlane, InProcessControlPlane
from .transports.twilio import build_stream_twiml

logger = logging.getLogger(__name__)

_XML = "application/xml"


def validate_twilio_signature(
    auth_token: str, url: str, params: dict[str, str], signature: str
) -> bool:
    """Twilio's scheme: HMAC-SHA1(auth_token, url + sorted k+v concatenation),
    base64-encoded, compared constant-time to the X-Twilio-Signature header."""
    payload = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(
        auth_token.encode(), payload.encode("utf-8"), hashlib.sha1
    ).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature or "")


def _twiml(content: str) -> Response:
    return Response(content=content, media_type=_XML)


def build_fastapi_app(
    agent: Any,
    *,
    public_base_url: str | None = None,
    control_plane: ControlPlane | None = None,
    connection_factory: Callable[[str], RealtimeConnection] | None = None,
    validate_signature: bool | None = None,
    app: FastAPI | None = None,
) -> FastAPI:
    """Wire `agent` to a FastAPI app for Twilio telephony.

    - `public_base_url`: e.g. `https://abc.ngrok.app` (defaults to
      `$PUBLIC_BASE_URL`). Needed to build absolute TwiML / wss URLs.
    - `control_plane`: defaults to `InProcessControlPlane(agent)` (monolith).
      Inject `HttpControlPlane(...)` to run this as a telephony microservice.
    - `connection_factory`: builds the provider `RealtimeConnection` for a
      session; defaults to `agent.provider.open_realtime(...)`. Injectable for
      tests so the media WS can run without a real provider socket.
    - `validate_signature`: enforce `X-Twilio-Signature` on inbound webhooks
      (defaults to on when `TWILIO_AUTH_TOKEN` is set).
    """
    base = (public_base_url or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    cp = control_plane or InProcessControlPlane(agent)
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    enforce_sig = (
        bool(auth_token) if validate_signature is None else validate_signature
    )

    def _make_connection(session_id: str) -> RealtimeConnection:
        if connection_factory is not None:
            return connection_factory(session_id)
        provider = agent.provider
        if not isinstance(provider, RealtimeBridgeProvider):
            raise RuntimeError(
                f"Provider {provider.name!r} has no open_realtime(); it can't "
                "serve telephony. Use a RealtimeBridgeProvider (e.g. xAI)."
            )
        return provider.open_realtime(agent._build_agent_config(), session_id)

    fapp = app or FastAPI(title="voiceagentpy telephony")
    router = APIRouter()

    @router.post("/twilio/voice")
    async def twilio_inbound(request: Request) -> Response:
        form = dict((await request.form()).items())
        if enforce_sig:
            sig = request.headers.get("X-Twilio-Signature", "")
            url = (base or str(request.base_url).rstrip("/")) + request.url.path
            if not validate_twilio_signature(auth_token, url, form, sig):
                logger.warning("rejected inbound: bad Twilio signature")
                return Response(status_code=403, content="bad signature")
        res = agent.connect(
            transport="twilio",
            call_details={
                "from": form.get("From"),
                "to": form.get("To"),
                "call_sid": form.get("CallSid"),
                "public_base_url": base,
            },
            metadata={
                "from": form.get("From"),
                "to": form.get("To"),
                "call_sid": form.get("CallSid"),
                "direction": "inbound",
            },
        )
        twiml = res.to_dict().get("twiml")
        if not twiml:
            return Response(status_code=500, content="public_base_url not configured")
        return _twiml(twiml)

    @router.api_route("/twilio/voice/{session_id}", methods=["GET", "POST"])
    async def twilio_outbound_twiml(session_id: str) -> Response:
        if not base:
            return Response(status_code=500, content="public_base_url not configured")
        ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
        media_ws_url = f"{ws_base}/twilio/media/{session_id}"
        return _twiml(build_stream_twiml(media_ws_url, session_id))

    @router.websocket("/twilio/media/{session_id}")
    async def twilio_media(ws: WebSocket, session_id: str) -> None:
        await ws.accept()
        logger.info("media WS open session=%s", session_id)
        try:
            conn = _make_connection(session_id)
        except Exception:  # noqa: BLE001
            logger.exception("could not open provider connection")
            await ws.close()
            return
        bridge = MediaBridge(
            session_id=session_id,
            twilio_ws=ws,
            provider=conn,
            control_plane=cp,
        )
        try:
            await bridge.run()
        except Exception:  # noqa: BLE001
            logger.exception("media bridge crashed session=%s", session_id)
        finally:
            logger.info("media WS closed session=%s", session_id)

    # --- control-plane HTTP mirror (for the prod microservice split) --------

    @router.post("/sessions/{session_id}/events")
    async def ingest_event(session_id: str, request: Request) -> dict[str, Any]:
        event = await request.json()
        resp = agent.ingest_event(session_id, event)
        return resp or {}

    @router.post("/sessions/{session_id}/end")
    async def end_session(session_id: str, request: Request) -> dict[str, Any]:
        body: dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            pass
        agent.end_session(session_id, reason=body.get("reason", "client_disconnect"))
        return {"ok": True}

    fapp.include_router(router)
    return fapp

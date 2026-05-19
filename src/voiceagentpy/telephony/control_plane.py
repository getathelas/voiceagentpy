"""The bridge's only dependency on session state / tool execution.

This is the seam between the **media plane** (Twilio ↔ provider audio bridge)
and the **control plane** (VoiceAgent: tool handlers, transcripts, events).

- `InProcessControlPlane` — prototype/monolith: call the in-process VoiceAgent
  directly.
- `HttpControlPlane` — production split: the telephony microservice POSTs to a
  central backend that hosts the VoiceAgent.

Same `ControlPlane` interface either way, so splitting the microservice out is
a wiring change (which class you inject), not a bridge rewrite.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class ControlPlane(Protocol):
    async def ingest(
        self, session_id: str, event: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Feed a provider event in; may return a `tool.result` dict to send
        back to the provider."""
        ...

    async def end(self, session_id: str, reason: str) -> None: ...


class InProcessControlPlane:
    """Monolith: drive the in-process VoiceAgent. `ingest_event` is sync and
    may run (possibly async) tool handlers, so we offload to a worker thread to
    keep the bridge's audio loop from stalling."""

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    async def ingest(
        self, session_id: str, event: dict[str, Any]
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._agent.ingest_event, session_id, event)

    async def end(self, session_id: str, reason: str) -> None:
        await asyncio.to_thread(self._agent.end_session, session_id, reason)


class HttpControlPlane:
    """Production split: telephony microservice → central backend over HTTP.
    Mirrors `VoiceAgent.ingest_event` / `end_session` via the FastAPI control
    endpoints (`POST /sessions/{id}/events` and `/sessions/{id}/end`)."""

    def __init__(
        self,
        base_url: str,
        *,
        http: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if http is None:
            import httpx  # noqa: PLC0415

            http = httpx.AsyncClient(timeout=15.0)
        self._base = base_url.rstrip("/")
        self._http = http
        self._headers = headers or {}

    async def ingest(
        self, session_id: str, event: dict[str, Any]
    ) -> dict[str, Any] | None:
        try:
            r = await self._http.post(
                f"{self._base}/sessions/{session_id}/events",
                json=event,
                headers=self._headers,
            )
        except Exception:  # noqa: BLE001
            logger.exception("control-plane ingest request failed")
            return None
        if r.status_code >= 400:
            logger.warning("control-plane ingest %s: %s", r.status_code, r.text)
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        return data or None

    async def end(self, session_id: str, reason: str) -> None:
        try:
            await self._http.post(
                f"{self._base}/sessions/{session_id}/end",
                json={"reason": reason},
                headers=self._headers,
            )
        except Exception:  # noqa: BLE001
            logger.exception("control-plane end failed")

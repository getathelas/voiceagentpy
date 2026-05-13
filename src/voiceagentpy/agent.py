"""VoiceAgent — primary developer-facing class."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Union

from .events import Event
from .providers import (
    AgentConfig,
    Provider,
    build_provider,
    resolve_provider_name,
)
from .session import Session, _now, _new_session_id
from .transports import build_transport


logger = logging.getLogger(__name__)


EventHandler = Callable[[dict[str, Any]], Union[None, Awaitable[None]]]
FinishHandler = Callable[[dict[str, Any]], Union[None, Awaitable[None]]]
ToolHandler = Callable[..., Union[Any, Awaitable[Any]]]
DefaultToolHandler = Callable[[str, dict[str, Any]], Union[Any, Awaitable[Any]]]


def mock_tool_response(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Stock `default_tool_handler` returning a generic mock payload.

    Useful for prototyping a voice agent before wiring real tool logic:

        agent = VoiceAgent(..., tools=[...], default_tool_handler=mock_tool_response)
    """
    return {"mock": True, "tool": name, "arguments": arguments}


@dataclass
class _ConnectResult:
    """Returned from VoiceAgent.connect — what the frontend needs to dial out."""

    id: str
    provider: str
    model: str
    url: str
    client_secret: str
    expires_at: str
    transport: str = "browser"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "provider": self.provider,
            "model": self.model,
            "url": self.url,
            "client_secret": self.client_secret,
            "expires_at": self.expires_at,
            "transport": self.transport,
        }
        d.update(self.extra)
        return d


class VoiceAgent:
    """High-level voice agent.

    Mirrors the OpenAI SDK ergonomics: pass `model`, `instructions`, `tools`, and
    handler callables. The agent picks a provider from the model name, mints an
    ephemeral session via the provider, and hands the credentials to the frontend
    so the browser can connect directly to the provider over WebRTC.
    """

    def __init__(
        self,
        *,
        model: str,
        instructions: str | None = None,
        voice: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_handlers: dict[str, ToolHandler] | None = None,
        default_tool_handler: DefaultToolHandler | None = None,
        event_handler: EventHandler | None = None,
        finish_handler: FinishHandler | None = None,
        api_key: str | None = None,
        provider: str | Provider | None = None,
        temperature: float | None = None,
        turn_detection: dict[str, Any] | None = None,
        input_audio_transcription: dict[str, Any] | None = None,
        modalities: list[str] | None = None,
        **extra: Any,
    ) -> None:
        self.model = model
        self.instructions = instructions
        self.voice = voice
        self.tools = list(tools) if tools else None
        self.tool_handlers = dict(tool_handlers or {})
        self.default_tool_handler = default_tool_handler
        self.event_handler = event_handler
        self.finish_handler = finish_handler
        self.temperature = temperature
        self.turn_detection = turn_detection
        self.input_audio_transcription = input_audio_transcription
        self.modalities = modalities
        self._extra = extra

        if isinstance(provider, Provider):
            self.provider: Provider = provider
        else:
            name = provider or resolve_provider_name(model)
            self.provider = build_provider(name, api_key=api_key)

        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    # ----- registration helpers ------------------------------------------------

    def tool(self, name: str | None = None) -> Callable[[ToolHandler], ToolHandler]:
        """Decorator form: register a handler for a previously-declared tool."""

        def decorate(fn: ToolHandler) -> ToolHandler:
            key = name or fn.__name__
            self.tool_handlers[key] = fn
            return fn

        return decorate

    def on_event(self, fn: EventHandler) -> EventHandler:
        self.event_handler = fn
        return fn

    def on_finish(self, fn: FinishHandler) -> FinishHandler:
        self.finish_handler = fn
        return fn

    # ----- session lifecycle ---------------------------------------------------

    def connect(
        self,
        *,
        transport: str = "browser",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        call_details: dict[str, Any] | None = None,
        onEvent: EventHandler | None = None,
        onFinish: FinishHandler | None = None,
    ) -> _ConnectResult:
        sid = session_id or _new_session_id()
        cfg = AgentConfig(
            model=self.model,
            instructions=self.instructions,
            voice=self.voice,
            tools=self.tools,
            temperature=self.temperature,
            turn_detection=self.turn_detection,
            input_audio_transcription=self.input_audio_transcription,
            modalities=self.modalities,
            extra=dict(self._extra),
        )
        creds = self.provider.mint_session(cfg, sid, metadata=metadata)
        session = Session(
            id=sid,
            credentials=creds,
            metadata=dict(metadata or {}),
            event_handler=onEvent,
            finish_handler=onFinish,
        )
        with self._lock:
            self._sessions[sid] = session

        self._emit(Event(type="session.started", session_id=sid, data={"model": self.model}))

        t = build_transport(transport)
        payload = t.prepare(creds, call_details)
        return _ConnectResult(
            id=sid,
            provider=creds.provider,
            model=creds.model,
            url=creds.url,
            client_secret=creds.client_secret,
            expires_at=creds.expires_at.isoformat(),
            transport=transport,
            extra={k: v for k, v in payload.items() if k not in {
                "id", "provider", "model", "url", "client_secret", "expires_at", "transport"
            }},
        )

    def end_session(self, session_id: str, reason: str = "client_disconnect") -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return
        session.ended_at = _now()
        session.ended_reason = reason
        self._emit(
            Event(type="session.ended", session_id=session_id, data={"reason": reason}),
            session=session,
        )
        finish_handler = session.finish_handler or self.finish_handler
        if finish_handler is not None:
            _call_maybe_async(finish_handler, session.summary())

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    # ----- ingest events/tool-calls relayed from the browser -------------------

    def ingest_event(self, session_id: str, raw: dict[str, Any]) -> dict[str, Any] | None:
        """Called by the control WebSocket whenever the browser forwards a
        provider event. May return a response dict to send back to the browser
        (used for tool results)."""
        session = self.get_session(session_id)
        if session is None:
            logger.warning("ingest_event for unknown session_id=%s", session_id)
            return None

        kind = raw.get("type") or ""

        if kind in ("transcript.delta", "response.audio_transcript.delta", "input_audio_transcription.delta"):
            text = raw.get("text") or raw.get("delta") or ""
            self._emit(Event(type="transcript.delta", session_id=session_id, data={"text": text, "role": raw.get("role", "assistant")}))
            return None

        if kind in ("transcript.final", "response.audio_transcript.done", "input_audio_transcription.completed", "conversation.item.input_audio_transcription.completed"):
            text = raw.get("text") or raw.get("transcript") or ""
            role = raw.get("role") or ("user" if "input" in kind else "assistant")
            session.transcript.append({"role": role, "text": text})
            self._emit(Event(type="transcript.final", session_id=session_id, data={"text": text, "role": role}))
            return None

        if kind in ("tool.call", "tool.called", "response.function_call_arguments.done"):
            return self._handle_tool_call(session_id, raw)

        if kind == "audio.started":
            self._emit(Event(type="audio.started", session_id=session_id))
            return None
        if kind == "audio.ended":
            self._emit(Event(type="audio.ended", session_id=session_id))
            return None

        if kind == "error":
            self._emit(Event(type="error", session_id=session_id, data=raw))
            return None

        # Unknown event — pass through to event_handler for visibility.
        self._emit(Event(type="error", session_id=session_id, data={"unhandled": raw}))
        return None

    def _handle_tool_call(self, session_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        name = raw.get("name") or raw.get("tool") or ""
        call_id = raw.get("call_id") or raw.get("id") or ""
        args_raw = raw.get("arguments") or raw.get("args") or {}
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw) if args_raw else {}
            except json.JSONDecodeError:
                args = {"_raw": args_raw}
        else:
            args = dict(args_raw or {})

        self._emit(Event(type="tool.called", session_id=session_id, data={"name": name, "arguments": args, "call_id": call_id}))

        handler = self.tool_handlers.get(name)
        if handler is not None:
            try:
                result = _call_maybe_async_sync(handler, **args)
            except Exception as e:  # noqa: BLE001
                logger.exception("Tool %s raised", name)
                result: Any = {"error": str(e)}
        elif self.default_tool_handler is not None:
            try:
                result = _call_maybe_async_sync(self.default_tool_handler, name, args)
            except Exception as e:  # noqa: BLE001
                logger.exception("default_tool_handler raised on %s", name)
                result = {"error": str(e)}
        else:
            err = f"No tool_handler registered for {name!r}"
            logger.warning(err)
            result = {"error": err}

        record = {"name": name, "call_id": call_id, "arguments": args, "result": result}
        session = self.get_session(session_id)
        if session is not None:
            session.tool_calls.append(record)

        self._emit(Event(type="tool.completed", session_id=session_id, data=record))

        return {
            "type": "tool.result",
            "call_id": call_id,
            "name": name,
            "result": result,
        }

    # ----- Flask helper --------------------------------------------------------

    def flask_blueprint(self, name: str = "voiceagentpy"):
        """Build a Flask blueprint exposing /sessions and the control WebSocket."""
        from .flask_ext import build_blueprint

        return build_blueprint(self, name=name)

    # ----- internals -----------------------------------------------------------

    def _emit(self, event: Event, session: Session | None = None) -> None:
        if session is None:
            with self._lock:
                session = self._sessions.get(event.session_id)
        handler = (session.event_handler if session is not None else None) or self.event_handler
        if handler is None:
            return
        try:
            _call_maybe_async(handler, event.to_dict())
        except Exception:  # noqa: BLE001
            logger.exception("event_handler raised")


def _call_maybe_async(fn: Callable, *args, **kwargs) -> None:
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        _run_coro(result)


def _call_maybe_async_sync(fn: Callable, *args, **kwargs) -> Any:
    """Like _call_maybe_async but returns the value, blocking on coroutines."""
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return _run_coro(result)
    return result


def _run_coro(coro: Awaitable) -> Any:
    """Run a coroutine to completion from sync code, even if a loop is running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    # Inside a running loop — dispatch to a worker thread.
    return asyncio.run_coroutine_threadsafe(coro, loop).result()  # type: ignore[arg-type]

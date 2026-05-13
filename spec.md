# Voice Agent Framework — Spec

## 1. Context

We're building a Python package that exposes a **drop-in, OpenAI-style client library for realtime voice agents**. Developers should be able to spin up a voice agent with the same shape as `openai.chat.completions.create(...)` — passing `model`, `messages`, and `tools` — and have the framework handle the realtime audio plumbing under the hood.

The framework wraps multiple underlying voice providers behind one interface. v1 ships with:

- **OpenAI Realtime** (`gpt-realtime-2`) — default, configured via `OPENAI_API_KEY`
- **xAI Grok Voice** (`grok-voice`) — secondary, configured via `XAI_API_KEY`

Browser audio travels **directly to the provider** over WebRTC using a short-lived ephemeral key minted by the developer's backend. The Python package never proxies audio; it only manages session lifecycle, tool execution, and events.

Twilio / telephony transport is **deferred to v2** but the `Transport` abstraction is designed with it in mind.

## 2. Deliverables

| # | Deliverable | Description |
|---|---|---|
| A | GitHub repo | Hosts the `voiceagentpy` Python package + example apps |
| B | Pip-installable package | `pip install git+https://github.com/ashbhat/voiceagentpy.git` |
| C | Example Flask backend | Mints ephemeral sessions, hosts tool handlers, emits events |
| D | Example Next.js frontend | Single-page app: press "Say hello" → realtime voice conversation |

## 3. Key Result (acceptance criteria)

1. `pip install` the package from the private repo into a fresh venv.
2. `python examples/flask_app/app.py` boots the backend on `:5050`.
3. `pnpm dev` in `examples/nextjs_app` boots the frontend on `:3000`.
4. Open `http://localhost:3000` → click **Say hello** → mic permission granted → real-time bidirectional voice conversation with the agent.
5. The agent can invoke a registered tool (e.g. `lookup_user`) and the result is spoken back.
6. Closing the tab triggers `finish_handler` server-side with a transcript + duration.

## 4. Python API

### 4.1 `VoiceAgent`

The primary developer-facing class. Mirrors the ergonomics of the OpenAI SDK while encapsulating realtime session config.

```python
from voiceagentpy import VoiceAgent

agent = VoiceAgent(
    model="gpt-realtime-2",          # or "grok-voice"
    api_key=None,                     # falls back to OPENAI_API_KEY / XAI_API_KEY
    instructions="You are a helpful support voice agent. Keep responses short.",
    voice="friendly-support",         # provider-specific voice id, normalized
    tools=[...],                      # OpenAI-format tool definitions
    tool_handlers={                   # name -> callable
        "lookup_user": lookup_user_fn,
    },
    event_handler=handle_event,       # called on every session event
    finish_handler=handle_finish,     # called once on session end
    temperature=0.7,
    turn_detection={"type": "server_vad"},
)
```

Constructor resolves the provider from `model` via a registry (`gpt-realtime-2*` → OpenAI, `grok-voice*` → xAI). Explicit override available via `provider="openai" | "xai"`.

### 4.2 `agent.connect(...)`

Creates a session and returns credentials the client (browser or Twilio) uses to connect directly to the provider.

```python
session = agent.connect(
    transport="browser",              # "browser" in v1; "twilio" reserved for v2
    session_id="optional-app-supplied-id",
    metadata={"user_id": "u_123"},    # passed through to event_handler/finish_handler
)

# session is a dataclass:
session.id                # str
session.client_secret     # ephemeral key, ~60s TTL
session.provider          # "openai" | "xai"
session.url               # provider WebRTC endpoint
session.expires_at        # datetime
```

The Flask example returns this directly as JSON to the frontend.

### 4.3 Drop-in `client.chat.completions.create(...)`

For symmetry with the OpenAI SDK, we also expose a lower-level client surface. It returns a `RealtimeResponse` object rather than a chat completion — but the call signature matches.

```python
from voiceagentpy import VoiceClient

client = VoiceClient(api_key="...")
response = client.chat.completions.create(
    model="gpt-realtime-2",
    messages=messages,
    tools=tool_definitions,
)
# response.session_id, response.client_secret, response.url
```

Most users will prefer `VoiceAgent`; this is for cases where a developer wants to retrofit existing OpenAI-style code.

### 4.4 Tools

Tools use the **OpenAI tools schema** verbatim:

```python
tools = [
    {
        "type": "function",
        "function": {
            "name": "lookup_user",
            "description": "Look up a user account by phone number",
            "parameters": {
                "type": "object",
                "properties": {"phone": {"type": "string"}},
                "required": ["phone"],
            },
        },
    },
]
```

Tool **execution** happens server-side in the Python process:

1. Provider emits a tool-call event to the browser.
2. Browser forwards the call to Flask via the control WebSocket (`/sessions/<id>/control`).
3. Flask dispatches to `tool_handlers[name](**args)`.
4. Result is sent back to the browser, which forwards it to the provider.

Handlers may be sync or async; the framework awaits coroutines.

### 4.5 Events

`event_handler(event)` is invoked for every session event. Event shape:

```python
{
  "type": "transcript.delta" | "transcript.final" | "tool.called"
        | "tool.completed" | "audio.started" | "audio.ended"
        | "error" | "session.started" | "session.ended",
  "session_id": "...",
  "timestamp": "2026-05-11T12:00:00Z",
  "data": { ... },                  # type-specific payload
}
```

`finish_handler(summary)` is invoked once when the session ends:

```python
{
  "session_id": "...",
  "duration_ms": 42000,
  "transcript": [{"role": "user"|"assistant", "text": "..."}, ...],
  "tool_calls": [...],
  "ended_reason": "client_disconnect" | "timeout" | "error",
  "metadata": {...},
}
```

## 5. Provider Abstraction

Every provider implements `voiceagentpy.providers.base.Provider`:

```python
class Provider(Protocol):
    name: str
    def mint_session(self, agent_config, session_id, metadata) -> SessionCredentials: ...
    def normalize_voice(self, voice: str) -> str: ...
    def supported_models(self) -> list[str]: ...
```

v1 implementations:

- `providers/openai_realtime.py` — POSTs to `/v1/realtime/client_secrets` with `instructions`, `voice`, `tools`, `turn_detection`. Returns ephemeral `client_secret` for direct WebRTC.
- `providers/xai_grok.py` — uses xAI's voice-agent session-mint endpoint per their docs. Voice IDs and turn-detection shapes are normalized into the framework's common config.

Provider differences (voice names, modalities, tool-event shape) are normalized inside the provider class so the developer-facing API stays uniform.

## 6. Transport Abstraction

```python
class Transport(Protocol):
    name: str
    def prepare(self, session: Session) -> dict: ...   # returns transport-specific payload
```

v1 ships `BrowserTransport` (WebRTC, ephemeral key). `TwilioTransport` (deferred) will return TwiML / media-stream config from the same `agent.connect(transport="twilio", ...)` call.

## 7. Backend — Flask Example

```
examples/flask_app/
├── app.py
├── tools.py
└── requirements.txt
```

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/sessions` | Create a session. Body: `{ "user_id": "..." }`. Returns `SessionCredentials` JSON. |
| `WS` | `/sessions/<id>/control` | Control channel: receives tool-call forwards, emits events. |
| `POST` | `/sessions/<id>/end` | Optional explicit-end hook (frontend calls on disconnect). |
| `GET` | `/health` | Liveness. |

### `app.py` shape

```python
from flask import Flask, request, jsonify
from voiceagentpy import VoiceAgent
from tools import lookup_user

app = Flask(__name__)

agent = VoiceAgent(
    model="gpt-realtime-2",
    instructions="You are a helpful support voice agent. Keep responses short.",
    voice="friendly-support",
    tools=[{"type": "function", "function": {...}}],
    tool_handlers={"lookup_user": lookup_user},
    event_handler=lambda e: app.logger.info(e),
    finish_handler=lambda s: app.logger.info(f"finished: {s['session_id']}"),
)

@app.post("/sessions")
def create_session():
    session = agent.connect(transport="browser", metadata=request.json)
    return jsonify(session.to_dict())
```

The control WebSocket is provided by `agent.flask_blueprint()` — a helper that registers the WS route and wires it into the agent's internal session registry. Developers can also wire it themselves.

### Config / env

- `OPENAI_API_KEY` — default provider key
- `XAI_API_KEY` — required only if using `grok-voice`
- `VOICE_AGENT_ALLOWED_ORIGINS` — CORS allowlist for `/sessions` (default `http://localhost:3000`)

## 8. Frontend — Next.js Example

```
examples/nextjs_app/
├── app/
│   ├── page.tsx          # "Say hello" button + transcript
│   └── layout.tsx
├── lib/
│   └── voice-client.ts   # thin wrapper around RTCPeerConnection + control WS
├── package.json
└── next.config.js
```

### Flow

1. Page renders **Say hello** button.
2. On click:
   - `fetch('http://localhost:5050/sessions', { method: 'POST', body: JSON.stringify({}) })` → receives `{ client_secret, url, session_id, ... }`.
   - Request mic permission via `navigator.mediaDevices.getUserMedia({ audio: true })`.
   - Open `RTCPeerConnection`, add mic track, create offer, POST SDP to provider's realtime URL with `Authorization: Bearer <client_secret>`, set answer.
   - Open WebSocket to `ws://localhost:5050/sessions/<id>/control` for tool/event relay.
3. Audio plays through a hidden `<audio>` element attached to the peer connection's remote stream.
4. Live transcript renders from events received over the data channel + control WS.
5. **End call** button (or page unload) closes the peer connection and POSTs `/sessions/<id>/end`.

### `lib/voice-client.ts` exports

```ts
export function startVoiceSession(opts: {
  sessionUrl: string;          // backend /sessions
  controlWsUrl: string;        // backend /sessions/{id}/control
  onTranscript(text, role): void;
  onToolCall(name, args): void;
  onEnded(reason): void;
}): Promise<{ stop(): void }>;
```

This file is the only realtime-specific code in the frontend — the rest is plain React.

## 9. Package layout

```
voiceagentpy/
├── pyproject.toml
├── README.md
├── src/voiceagentpy/
│   ├── __init__.py             # exports VoiceAgent, VoiceClient
│   ├── agent.py
│   ├── client.py
│   ├── session.py              # Session / SessionCredentials dataclasses
│   ├── events.py
│   ├── flask_ext.py            # blueprint helper
│   ├── providers/
│   │   ├── base.py
│   │   ├── openai_realtime.py
│   │   └── xai_grok.py
│   └── transports/
│       ├── base.py
│       └── browser.py
├── examples/
│   ├── flask_app/
│   └── nextjs_app/
└── tests/
    ├── test_agent.py
    ├── test_providers_openai.py
    └── test_providers_xai.py
```

`pyproject.toml` uses **hatchling**, declares `voiceagentpy` as the distribution name, and includes `flask` and `websockets` as optional extras under `[project.optional-dependencies] examples = [...]` so the core package stays lean.

## 10. Out of scope (v1)

- Twilio / SIP / PSTN transport (planned for v2; transport abstraction reserves space)
- On-disk recording / archival of audio (only transcripts are surfaced)
- Multi-tenant key management — single `OPENAI_API_KEY` per process
- Streaming partial tool results back into the model mid-call
- Auth on the example Flask app (it trusts localhost; production users must add their own)

## 11. Open questions

1. **Voice naming.** OpenAI and Grok use different voice IDs. Should the framework expose a normalized set (`"friendly-support"`, `"calm-narrator"`) and map per provider, or pass through raw provider voices? Spec currently assumes normalized — confirm before implementation.
2. **Tool execution path.** Routing every tool call browser → Flask → browser → provider adds a round-trip. Alternative: let the provider call a developer-hosted webhook directly. Stick with the relay model in v1 for simplicity; revisit if latency is an issue.
3. **Frontend package.** Should the JS client be published as `@voiceagentpy/web` on npm, or kept as example code only? v1 ships as example code; promote to a package once the API stabilizes.

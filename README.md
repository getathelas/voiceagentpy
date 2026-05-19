# voiceagentpy

Put a real-time **xAI Grok voice agent** on a phone call. Write the agent in
Python — instructions, tools, handlers — call `agent.call(to="+1...")`, and the
library places an outbound call through **Twilio** and bridges the caller's
audio to Grok in both directions.

OpenAI-SDK-shaped ergonomics (`model`, `instructions`, `tools`,
`tool_handlers`), but the runtime is telephony: Twilio Media Streams on one
side, xAI's realtime WebSocket on the other, and your tool code in the middle.

See [`spec.md`](./spec.md) for the full design doc.

## How it works

A phone call can't speak xAI's wire protocol, so a small FastAPI server sits
between them and translates:

```
                      agent.call(to=…)               outbound REST
 VoiceAgent ───────────────────────────────────────────────────────►  Twilio
 (instructions,                                                          │
  tools, handlers)                                                       │ PSTN
       ▲                                                                 ▼
       │ tool calls / transcript / events                              Callee
       │                                                                 │
       │            μ-law 8 kHz  ◄── Media Streams WS ──►  FastAPI bridge │
       │                                                  (transcode +   │
       └──────────────────────────────────────────────►  tool relay)    │
                                                              │          │
                                              PCM16 24 kHz WS │          │
                                                              ▼          │
                                                      xAI Grok realtime ◄┘
```

1. `agent.call(transport="twilio", call_details={"to": "+1…"})` uses the Twilio
   REST API to dial `to` from your `TWILIO_FROM_NUMBER`, telling Twilio to
   fetch `PUBLIC_BASE_URL/twilio/voice/{session_id}` for instructions when the
   callee picks up.
2. That endpoint returns `<Connect><Stream>` TwiML pointing Twilio at the media
   WebSocket on the same server.
3. The bridge accepts the Media Streams socket, opens a **server-side** xAI
   Grok realtime connection (authed directly with `XAI_API_KEY` — no ephemeral
   key, there's no browser here), and runs the two streams concurrently:
   transcoding caller audio μ-law 8 kHz → PCM16 24 kHz to Grok, and Grok's
   audio back the other way, with barge-in handling.
4. Tool calls, transcripts, and errors from Grok are relayed to your
   `VoiceAgent` (handlers run in Python; results are sent back to the model).
5. On hangup the session closes and a summary (duration, transcript, tool
   calls) is available via `agent.get_session(id).summary()`.

Telephony is the one place audio flows *through* Python — it has to, to
transcode between Twilio's μ-law 8 kHz and Grok's PCM16 24 kHz.

## Install

Telephony needs the `fastapi` extra (FastAPI, uvicorn, websockets):

```bash
pip install "git+https://github.com/ashbhat/voiceagentpy.git#egg=voiceagentpy[fastapi]"
```

For local development:

```bash
git clone https://github.com/ashbhat/voiceagentpy
cd voiceagentpy
pip install -e ".[fastapi,dev]"
```

## Quickstart — outbound Twilio call

Set the environment (see [Configuration](#configuration)):

```bash
export XAI_API_KEY=xai-...
export TWILIO_ACCOUNT_SID=AC...
export TWILIO_AUTH_TOKEN=...
export TWILIO_FROM_NUMBER=+1...
export PUBLIC_BASE_URL=https://your-tunnel.example.com   # must reach the app below
```

```python
import threading, uvicorn
from voiceagentpy import VoiceAgent
from voiceagentpy.fastapi_ext import build_fastapi_app

agent = VoiceAgent(
    model="grok-voice-latest",
    instructions=(
        "You are a friendly phone support agent. Keep replies short and "
        "conversational — this is a real phone call. Call a tool the moment "
        "you have enough info, before speaking."
    ),
    voice="friendly-support",
    tools=[{
        "type": "function",
        "function": {
            "name": "lookup_user",
            "description": "Look up a user account by phone number.",
            "parameters": {
                "type": "object",
                "properties": {"phone": {"type": "string"}},
            },
        },
    }],
    tool_handlers={
        "lookup_user": lambda phone=None, **_: {"name": "Avery", "plan": "pro"},
    },
    turn_detection={"type": "server_vad"},
)

# Twilio must be able to reach this app at PUBLIC_BASE_URL for the TwiML
# callback + media WebSocket, so run it before placing the call.
app = build_fastapi_app(agent)
threading.Thread(
    target=lambda: uvicorn.run(app, host="0.0.0.0", port=8000), daemon=True
).start()

# Place the outbound call. The callee's phone rings and they talk to Grok.
res = agent.call(transport="twilio", call_details={"to": "+1..."})
print("dialing", res.call_sid, "session", res.id)
```

`PUBLIC_BASE_URL` has to be an `https://` URL that Twilio can reach back on —
your deployment, an ngrok/cloudflared tunnel, etc.

### Runnable example (tunnel included)

[`example/`](example/) is a single, self-contained script that does all of the
above **and** launches a `cloudflared` tunnel for you (no public URL needed),
places the call, waits for it to finish, prints the summary, and exits:

```bash
cd example
python3 -m venv ../.venv && source ../.venv/bin/activate
pip install -r requirements.txt        # voiceagentpy[fastapi] + python-dotenv
cp ../.env.example ../.env             # fill in keys
python3 main.py +14085987929           # or set CALL_TO in .env
```

See [`example/README.md`](example/README.md) for the full walkthrough,
including the inbound webhook (`POST /twilio/voice`) and a carrier note about
outbound call blocking.

## Tool calls

Tools are how the agent does things mid-call — look up an account, check
availability, file a ticket. You declare them with the standard OpenAI
function-calling JSON schema and register a Python handler per tool:

```python
agent = VoiceAgent(
    model="grok-voice-latest",
    tools=[{
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Book an appointment for the caller.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "ISO 8601 date"},
                    "service": {"type": "string"},
                },
                "required": ["date", "service"],
            },
        },
    }],
    tool_handlers={"book_appointment": book_appointment},
)
```

When Grok decides to call a tool mid-conversation, the bridge relays it to the
`VoiceAgent`, which:

1. Parses the JSON arguments and invokes your handler as
   `handler(**arguments)` — **sync or async** both work.
2. JSON-encodes the return value and sends it back to the model as the tool
   result, then nudges the model to continue speaking with the answer.
3. Records the call on the session (`session.tool_calls`) and emits
   `tool.called` / `tool.completed` events.

If a handler raises, the exception is caught and returned to the model as
`{"error": "<message>"}` rather than dropping the call — the agent can recover
gracefully ("sorry, I couldn't reach that system").

### Registering handlers

Three equivalent ways:

```python
# 1. constructor map
VoiceAgent(..., tool_handlers={"book_appointment": book_appointment})

# 2. decorator (handler for an already-declared tool)
@agent.tool("book_appointment")
def book_appointment(date: str, service: str, **_):
    return {"confirmed": True, "date": date, "service": service}

# 3. async handler — awaited automatically
@agent.tool("lookup_user")
async def lookup_user(phone: str = None, **_):
    return await db.fetch_user(phone)
```

Handlers should accept `**_` (or `**kwargs`) so an extra/unexpected argument
from the model never raises a `TypeError`.

### Mocking tools while prototyping

Pass `default_tool_handler` and any tool call without a registered handler is
routed there instead of erroring. The stock `mock_tool_response` returns a
generic stub, so you can declare the full toolset and exercise the conversation
before writing a single handler:

```python
from voiceagentpy import VoiceAgent, mock_tool_response

agent = VoiceAgent(
    model="grok-voice-latest",
    tools=[...],                                # definitions only
    tool_handlers={},                           # nothing wired yet
    default_tool_handler=mock_tool_response,    # auto-mocks everything
)
```

`default_tool_handler` is called as `handler(tool_name, arguments)` (sync or
async). Specific entries in `tool_handlers` always win over the default.

## Observing the call

Pass `event_handler` and/or `finish_handler` to watch a call live and get a
summary at the end:

```python
agent = VoiceAgent(
    ...,
    event_handler=lambda e: print(e["type"], e.get("data")),
    finish_handler=lambda s: print("done:", s["session_id"]),
)
```

`event_handler` receives dicts for `session.started` / `session.ended`,
`transcript.delta` / `transcript.final`, `tool.called` / `tool.completed`,
`audio.started` / `audio.ended`, and `error`. After a call ends:

```python
sess = agent.get_session(res.id)
s = sess.summary()           # {"duration_ms", "transcript", "tool_calls", ...}
```

## Configuration

| Env var | Required | Purpose |
|---|---|---|
| `XAI_API_KEY` | yes | Auth for the Grok realtime connection |
| `TWILIO_ACCOUNT_SID` | yes | Twilio account (starts `AC…`) |
| `TWILIO_AUTH_TOKEN` | yes | Twilio auth token; also validates inbound webhooks |
| `TWILIO_FROM_NUMBER` | yes | Caller ID for outbound (a local long-code dials more reliably than a toll-free 8xx) |
| `PUBLIC_BASE_URL` | yes\* | `https://` URL Twilio uses for the TwiML callback + media WS |
| `CALL_TO` | no | Default number to dial (the `example/` script) |
| `PORT` | no | Port the FastAPI app binds (default `8000`) |

\* The `example/` script launches a `cloudflared` tunnel and sets
`PUBLIC_BASE_URL` for you, so you only set it for your own deployment/tunnel.

Per-call overrides go in `call_details`: `from`, `account_sid`, `auth_token`,
`public_base_url`, `status_callback`.

### Models & voices

`grok-voice-latest` (used above), plus `grok-voice-think-fast-1.0` and
`grok-voice-fast-1.0`. Voice accepts xAI voice names directly or these aliases:
`friendly-support`, `warm`, `calm-narrator`, `energetic`, `neutral`.

## Inbound & production split

`agent.call(...)` is outbound. The same FastAPI app also serves **inbound**:
point a Twilio number's voice webhook at `POST /twilio/voice` (signature is
validated when `TWILIO_AUTH_TOKEN` is set).

The bridge talks to your agent through a `ControlPlane` seam. The default
`InProcessControlPlane` is the monolith above; injecting `HttpControlPlane`
splits the media bridge into a standalone telephony microservice that calls
your agent over HTTP — the same code, a wiring change. See
`voiceagentpy.fastapi_ext.build_fastapi_app` and
`voiceagentpy.telephony.control_plane`.

## Roadmap

- **shipped**: Twilio telephony — outbound `agent.call`, inbound webhook,
  FastAPI media bridge, in-process / HTTP control-plane split
- **next**: SHAKEN/STIR + branded caller-ID guidance for production outbound
- **next**: normalized voice catalog and provider-direct tool webhooks

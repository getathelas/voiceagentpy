# voiceagentpy

OpenAI-style Python client for realtime voice agents. v1 wraps **OpenAI Realtime** (`gpt-realtime-2`) and **xAI Grok Voice** (`grok-voice`) behind one interface. Browser audio streams **directly to the provider** over WebRTC using a short-lived ephemeral key minted by your backend — the Python package never proxies audio.

See [`spec.md`](./spec.md) for the full design doc.

## Install

```bash
pip install "git+https://github.com/ashbhat/voiceagentpy.git#egg=voiceagentpy[flask]"
```

For local development:

```bash
git clone https://github.com/ashbhat/voiceagentpy
cd voiceagentpy
pip install -e ".[flask,dev]"
```

## Quickstart

```python
from voiceagentpy import VoiceAgent

agent = VoiceAgent(
    model="gpt-realtime-2",
    instructions="You are a helpful support voice agent. Keep responses short.",
    voice="friendly-support",
    tools=[{
        "type": "function",
        "function": {
            "name": "lookup_user",
            "description": "Look up a user account",
            "parameters": {
                "type": "object",
                "properties": {"phone": {"type": "string"}},
            },
        },
    }],
    tool_handlers={"lookup_user": lambda phone: {"name": "Avery", "plan": "pro"}},
    event_handler=lambda e: print(e),
    finish_handler=lambda s: print("done", s["session_id"]),
)

session = agent.connect(transport="browser")
# Hand session.to_dict() to your frontend; it dials the provider directly.
```

### Mocking tool responses for prototyping

Pass `default_tool_handler` and any tool call without a registered handler will be routed there instead of erroring. The stock `mock_tool_response` returns a generic stub so you can declare tools without writing handlers:

```python
from voiceagentpy import VoiceAgent, mock_tool_response

agent = VoiceAgent(
    model="gpt-realtime-2",
    tools=[...],                              # tool definitions only
    tool_handlers={},                          # no real handlers wired
    default_tool_handler=mock_tool_response,   # auto-mocks anything
)
```

The default handler is called with `(tool_name, arguments)` and can be sync or async. Specific entries in `tool_handlers` always win over the default.

### OpenAI-SDK-shaped client (drop-in)

```python
from voiceagentpy import VoiceClient

client = VoiceClient()
resp = client.chat.completions.create(
    model="gpt-realtime-2",
    messages=[{"role": "system", "content": "You are concise."}],
    tools=[...],
)
# resp.client_secret, resp.url, resp.session_id
```

## Run the demo

The repo ships with a Flask backend and a Next.js frontend that together implement the spec's key result: open localhost, press a button, talk to the agent.

### 1. Backend (Flask)

```bash
cd examples/flask_app
pip install -e ../..[flask]
pip install -r requirements.txt
cp .env.example .env  # set OPENAI_API_KEY
python app.py
```

Backend runs on `http://localhost:5050` (5000 conflicts with macOS AirPlay Receiver). Override with `PORT=...`.

### 2. Frontend (Next.js)

```bash
cd examples/nextjs_app
pnpm install   # or npm install / yarn
pnpm dev
```

Frontend runs on `http://localhost:3000`.

### 3. Talk

Open `http://localhost:3000`, click **Say hello**, grant mic permission, talk.

## Configuration

| Env var | Purpose |
|---|---|
| `OPENAI_API_KEY` | Required for `gpt-realtime*` models |
| `XAI_API_KEY` | Required for `grok-voice*` models |
| `VOICE_AGENT_MODEL` | Override the demo's model (default `gpt-realtime`) |
| `VOICE_AGENT_ALLOWED_ORIGINS` | CORS allowlist for the demo backend |
| `NEXT_PUBLIC_VOICE_AGENT_BACKEND` | Backend URL the frontend talks to |

## Architecture

```
Browser  ──── WebRTC + ephemeral key ────►  Provider (OpenAI Realtime / xAI Grok)
   │                                          ▲
   │ control WS (tool calls / events)         │ audio + events
   ▼                                          │
Flask backend (voiceagentpy + tool_handlers)  │
   └──── mints ephemeral key per /sessions ───┘
```

For the **browser** transport, audio never touches the Python process — the
control WebSocket exists only so the backend can execute tools and surface
events to your `event_handler` / `finish_handler`.

## Telephony (Twilio)

```
Caller ──PSTN──► Twilio ──μ-law 8k WS──► FastAPI bridge ──PCM16 24k WS──► xAI Grok
                                            │ transcode + tool relay
                                            ▼
                                  VoiceAgent (tool_handlers, transcript, events)
```

Phone calls connect through a server-side **Media Streams bridge** — here audio
*does* flow through Python (transcoded μ-law 8 kHz ↔ PCM16 24 kHz), a
deliberate, telephony-only departure from the browser model.

```python
# Outbound — Twilio dials out, callee talks to the agent
agent.call(transport="twilio", call_details={"to": "+1..."})

# Inbound — point a Twilio number's voice webhook at POST /twilio/voice
agent.connect(transport="twilio", call_details={...})
```

Twilio config resolves from the environment (`TWILIO_ACCOUNT_SID`,
`TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `PUBLIC_BASE_URL`). The bridge runs
on FastAPI (`voiceagentpy.fastapi_ext.build_fastapi_app`); the prototype
monolith and a production telephony microservice share one `ControlPlane` seam
(`InProcess` vs `Http`), so the split is a wiring change. See
[`examples/fastapi_app`](examples/fastapi_app). xAI Grok is the supported
telephony provider.

## Roadmap

- **shipped**: Twilio telephony transport (outbound `agent.call` + inbound webhook, FastAPI media bridge)
- **next**: OpenAI Realtime parity for the telephony bridge
- **next**: SHAKEN/STIR + branded caller-ID guidance for production outbound
- **v2**: provider-direct tool webhooks to skip the browser round-trip
- **v2**: normalized voice catalog across providers

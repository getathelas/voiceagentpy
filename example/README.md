# Telephony example (Twilio + xAI Grok)

One script: creates the agent, places an outbound call, stays running until
the call completes, exits. Audio flows **caller ↔ Twilio (μ-law 8 kHz) ↔ this
process (transcode) ↔ xAI realtime (PCM16 24 kHz)**.

`main.py` runs a small FastAPI server in a background thread because Twilio
must reach back in for the TwiML callback + media WebSocket — but you just run
one command.

## Setup

**1. Create and activate a virtualenv** (from the repo root)

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
```

**2. Install the example's dependencies**

`requirements.txt` is self-contained — it installs `voiceagentpy` (editable,
with the `[fastapi]` extra) plus `python-dotenv`, so run it from this folder:

```bash
cd example
pip install -r requirements.txt
```

**3. Install cloudflared once** (no account needed)

```bash
brew install cloudflared
```

The script launches `cloudflared` itself and wires the public URL in, so
`PUBLIC_BASE_URL` is **not** required. Set it to your own `https://` tunnel
only to skip cloudflared (any non-https value is ignored and the tunnel is
launched instead).

**4. Set the environment variables**

`main.py` loads them via `python-dotenv` from a **repo-root `.env`**. Copy the
template and fill it in:

```bash
cp ../.env.example ../.env           # then edit ../.env
```

```ini
XAI_API_KEY=xai-...
TWILIO_ACCOUNT_SID=AC...             # starts AC…
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_FROM_NUMBER=+15139515830      # a local long-code is far more reliable
                                     # for outbound than a toll-free 8xx
# CALL_TO=+14085987929               # optional: default number to dial
```

## Run

From the `example/` folder, with the venv active:

```bash
python3 main.py +14085987929         # or set CALL_TO in .env and omit the arg
```

The script starts the tunnel + server, dials, waits for the call to finish,
then exits (and shuts the tunnel down).

Your phone rings; answer and talk to Grok (ask the time, or to look up your
account — it will call a tool). Hang up and the script logs the summary
(duration, turns, tool calls) and exits.

> **Carrier note:** some carriers (notably T-Mobile) auto-block calls from new,
> unattested numbers — it shows up as an instant "busy". For dev, disable
> carrier scam-blocking on the test phone (T-Mobile: dial `#632#`). For
> production, register the number for SHAKEN/STIR + branded caller ID via
> Twilio Trust Hub / Voice Integrity.

## Inbound / production split

`main.py` is outbound-only for simplicity. The library also serves inbound
(point a Twilio number's voice webhook at `POST /twilio/voice`) and a
production telephony-microservice split — see
`voiceagentpy.fastapi_ext.build_fastapi_app` and
`voiceagentpy.telephony.control_plane.HttpControlPlane`.

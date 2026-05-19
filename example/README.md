# Telephony example (Twilio + xAI Grok)

Two one-command scripts: **`main.py`** places an *outbound* call and exits
when it completes; **`inbound.py`** receives calls on a Twilio number and
serves until Ctrl-C ([see below](#inbound)). Both share the same setup. Audio
flows **caller ↔ Twilio (μ-law 8 kHz) ↔ this process (transcode) ↔ xAI
realtime (PCM16 24 kHz)**.

Each script runs a small FastAPI server (Twilio must reach back in for the
TwiML callback + media WebSocket) — but you just run one command.

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

## Inbound

`inbound.py` is the receive-calls counterpart of `main.py` — one command, no
manual Twilio Console steps. Add the number to receive on to the repo-root
`.env`:

```ini
TWILIO_INCOMING_NUMBER=+15139515830  # a voice number you own on this account
```

Then, from the `example/` folder with the venv active:

```bash
python3 inbound.py                   # then call TWILIO_INCOMING_NUMBER
```

It starts the tunnel + server, then **points that number's Voice webhook at
`https://<tunnel>/twilio/voice`** via the Twilio REST API (saving the previous
value). Call the number and talk to Grok exactly as with the outbound demo;
each call's transcript and end-of-call summary (caller, duration, turns, tool
calls) are logged live. It serves until **Ctrl-C**, then restores the number's
previous Voice webhook and shuts the tunnel down — so it never leaves your
real number pointed at a dead tunnel.

Inbound webhooks are signature-checked automatically whenever
`TWILIO_AUTH_TOKEN` is set (a forged `POST /twilio/voice` gets a 403).

> **Production:** don't auto-rewire an ephemeral tunnel. Set a stable
> `PUBLIC_BASE_URL` (a named cloudflared tunnel or your deployment) and
> configure the number's Voice webhook to `https://your-domain/twilio/voice`
> once in the Twilio Console. The same `build_fastapi_app(agent)` also supports
> a telephony-microservice split — see
> `voiceagentpy.fastapi_ext.build_fastapi_app` and
> `voiceagentpy.telephony.control_plane.HttpControlPlane`.

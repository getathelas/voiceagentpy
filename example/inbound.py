"""One-shot inbound telephony demo — tunnel + auto-wired Twilio number.

    python3 inbound.py        # then call your TWILIO_INCOMING_NUMBER

Runs straight through:
  (A) create the xAI Grok voice agent (live transcript + per-call summary)
  (B) launch cloudflared + the FastAPI server, then point the Twilio number's
      Voice webhook at this tunnel (Twilio reaches back here per call for the
      <Connect><Stream> TwiML and the media WebSocket)
  (C) serve until Ctrl-C; each inbound call bridges to Grok. On exit the
      number's previous Voice webhook is restored and the tunnel is shut down.

PUBLIC_BASE_URL is optional: set it to an https:// URL to use your own
tunnel/deployment, otherwise cloudflared is launched automatically
(`brew install cloudflared`, no account needed). Required in the repo-root
.env: XAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_INCOMING_NUMBER.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from typing import Any

import httpx
import uvicorn
from dotenv import load_dotenv

from voiceagentpy import VoiceAgent, mock_tool_response
from voiceagentpy.fastapi_ext import build_fastapi_app

from tools import TOOL_DEFINITIONS, TOOL_HANDLERS

if __name__ == "__main__":
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    log = logging.getLogger("telephony-inbound")
    PORT = int(os.environ.get("PORT", "8000"))

    # --- required env --------------------------------------------------------
    missing = [
        v
        for v in ("XAI_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                  "TWILIO_INCOMING_NUMBER")
        if not os.environ.get(v)
    ]
    if missing:
        print(f"missing env (set in .env): {', '.join(missing)}")
        raise SystemExit(2)
    acc = os.environ["TWILIO_ACCOUNT_SID"]
    tok = os.environ["TWILIO_AUTH_TOKEN"]
    incoming = os.environ["TWILIO_INCOMING_NUMBER"]

    # --- (A) agent: same as main.py, plus live transcript + summary logging --
    def on_event(ev: dict[str, Any]) -> None:
        if ev.get("type") == "transcript.final":
            sid = (ev.get("session_id") or "")[-6:]
            d = ev.get("data") or {}
            log.info("[%s] %s: %s", sid, d.get("role", "?"), d.get("text", ""))

    def on_finish(summary: dict[str, Any]) -> None:
        meta = summary.get("metadata") or {}
        log.info(
            "[%s] call ended (from=%s reason=%s): %d ms, %d turns, %d tool calls",
            (summary.get("session_id") or "")[-6:],
            meta.get("from", "?"),
            summary.get("ended_reason", "?"),
            summary.get("duration_ms", 0),
            len(summary.get("transcript", [])),
            len(summary.get("tool_calls", [])),
        )

    agent = VoiceAgent(
        model="grok-voice-latest",
        instructions=(
            "You are a friendly phone support agent. Keep replies short and "
            "conversational — this is a real phone call. Call a tool the "
            "moment you have enough info, before speaking."
        ),
        voice="friendly-support",
        tools=TOOL_DEFINITIONS,
        tool_handlers=TOOL_HANDLERS,
        default_tool_handler=mock_tool_response,
        turn_detection={"type": "server_vad"},
        event_handler=on_event,
        finish_handler=on_finish,
    )

    # --- (B) public URL: preset https:// or launch cloudflared ---------------
    tunnel = None
    preset = os.environ.get("PUBLIC_BASE_URL", "")
    if preset.startswith("https://"):
        public_url = preset.rstrip("/")
        log.info("using preset PUBLIC_BASE_URL: %s", public_url)
    else:
        log.info("starting cloudflared tunnel...")
        cf_log = tempfile.NamedTemporaryFile(
            "w+", suffix=".cflog", delete=False
        )
        try:
            tunnel = subprocess.Popen(
                ["cloudflared", "tunnel", "--url",
                 f"http://localhost:{PORT}", "--no-autoupdate"],
                stdout=cf_log,
                stderr=subprocess.STDOUT,
            )
        except FileNotFoundError:
            print(
                "cloudflared not found — `brew install cloudflared`, or set "
                "PUBLIC_BASE_URL to your own https:// tunnel."
            )
            raise SystemExit(1)
        public_url = ""
        deadline = time.time() + 30
        while time.time() < deadline:
            with open(cf_log.name) as f:
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", f.read())
            if m:
                public_url = m.group(0)
                break
            time.sleep(0.5)
        if not public_url:
            tunnel.terminate()
            print("cloudflared did not produce a public URL within 30s")
            raise SystemExit(1)
        log.info("tunnel: %s", public_url)

    os.environ["PUBLIC_BASE_URL"] = public_url  # read by build_fastapi_app

    # --- server in a daemon thread (target is uvicorn's own method) ----------
    server = uvicorn.Server(
        uvicorn.Config(build_fastapi_app(agent), host="0.0.0.0", port=PORT,
                       log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    log.info("server up :%d -> %s", PORT, public_url)

    # --- point the Twilio number's Voice webhook here (save prior to restore) -
    voice_url = f"{public_url}/twilio/voice"
    api = f"https://api.twilio.com/2010-04-01/Accounts/{acc}"
    prior_voice_url = None
    pn_sid = None
    with httpx.Client(timeout=15.0, auth=(acc, tok)) as client:
        r = client.get(
            f"{api}/IncomingPhoneNumbers.json", params={"PhoneNumber": incoming}
        )
        nums = r.json().get("incoming_phone_numbers", []) if r.is_success else []
        if not nums:
            server.should_exit = True
            if tunnel is not None:
                tunnel.terminate()
            print(
                f"no IncomingPhoneNumber matching {incoming} on this account "
                f"(Twilio HTTP {r.status_code})"
            )
            raise SystemExit(1)
        pn_sid = nums[0]["sid"]
        prior_voice_url = nums[0].get("voice_url") or ""
        client.post(
            f"{api}/IncomingPhoneNumbers/{pn_sid}.json",
            data={"VoiceUrl": voice_url, "VoiceMethod": "POST"},
        )
    log.info(
        "wired %s (%s) voice webhook: %r -> %r",
        incoming, pn_sid, prior_voice_url, voice_url,
    )
    log.info("ready — call %s to talk to Grok. Ctrl-C to stop.", incoming)

    # --- (C) serve until interrupted, then restore + tear down ---------------
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("interrupted — restoring webhook + shutting down")

    try:
        with httpx.Client(timeout=15.0, auth=(acc, tok)) as client:
            client.post(
                f"{api}/IncomingPhoneNumbers/{pn_sid}.json",
                data={"VoiceUrl": prior_voice_url, "VoiceMethod": "POST"},
            )
        log.info("restored %s voice webhook -> %r", incoming, prior_voice_url)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "could not restore prior voice webhook (%s); set it back in the "
            "Twilio Console: %r",
            e, prior_voice_url,
        )

    server.should_exit = True
    if tunnel is not None:
        tunnel.terminate()
    raise SystemExit(0)

"""One-shot outbound telephony demo — tunnel included, no helper functions.

    python3 main.py +14085987929        # or set CALL_TO in .env

Runs straight through:
  (A) create the xAI Grok voice agent
  (B) launch cloudflared + the FastAPI server (Twilio reaches back here for
      the TwiML callback and media WebSocket)
  (C) place the call and poll Twilio until it completes, then exit

PUBLIC_BASE_URL is optional: set it to an https:// URL to use your own
tunnel/deployment, otherwise cloudflared is launched automatically
(`brew install cloudflared`, no account needed). Required in the repo-root
.env: XAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

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
    log = logging.getLogger("telephony-demo")
    PORT = int(os.environ.get("PORT", "8000"))

    # --- args + required env -------------------------------------------------
    to = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CALL_TO")
    if not to:
        print("usage: python3 main.py +1XXXXXXXXXX   (or set CALL_TO in .env)")
        raise SystemExit(2)
    missing = [
        v
        for v in ("XAI_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                  "TWILIO_FROM_NUMBER")
        if not os.environ.get(v)
    ]
    if missing:
        print(f"missing env (set in .env): {', '.join(missing)}")
        raise SystemExit(2)

    # --- (A) agent -----------------------------------------------------------
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

    os.environ["PUBLIC_BASE_URL"] = public_url  # read by build + agent.call

    # --- server in a daemon thread (target is uvicorn's own method) ----------
    server = uvicorn.Server(
        uvicorn.Config(build_fastapi_app(agent), host="0.0.0.0", port=PORT,
                       log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    log.info("server up :%d -> %s", PORT, public_url)

    # --- (C) place the call, poll Twilio until terminal ----------------------
    res = agent.call(transport="twilio", call_details={"to": to})
    log.info("dialing %s -> call_sid=%s session=%s", to, res.call_sid, res.id)

    acc = os.environ["TWILIO_ACCOUNT_SID"]
    tok = os.environ["TWILIO_AUTH_TOKEN"]
    status_url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{acc}/Calls/"
        f"{res.call_sid}.json"
    )
    terminal = {"completed", "failed", "busy", "no-answer", "canceled"}
    status = None
    deadline = time.time() + 600
    try:
        with httpx.Client(timeout=10.0) as client:
            while time.time() < deadline:
                try:
                    status = client.get(status_url, auth=(acc, tok)).json().get(
                        "status"
                    )
                except Exception:  # noqa: BLE001
                    status = None
                if status in terminal:
                    break
                time.sleep(3)
    except KeyboardInterrupt:
        log.info("interrupted")

    sess = agent.get_session(res.id)
    if sess is not None:
        s = sess.summary()
        log.info(
            "summary: %d ms, %d turns, %d tool calls",
            s["duration_ms"], len(s["transcript"]), len(s["tool_calls"]),
        )
    log.info("call ended: status=%s", status)

    server.should_exit = True
    if tunnel is not None:
        tunnel.terminate()
    raise SystemExit(0)

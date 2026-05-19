"""Outbound dialing via the Twilio REST API.

We hit `POST /2010-04-01/Accounts/{sid}/Calls.json` directly with `httpx`
(already a core dep) rather than pulling the `twilio` SDK — it's a single
form-encoded, basic-auth request, exactly the call we validated by hand.

Credentials/config resolve from the environment by default
(`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`,
`PUBLIC_BASE_URL`) and can be overridden per-call via `call_details`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

CALLS_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"


@dataclass
class TwilioConfig:
    account_sid: str
    auth_token: str
    from_number: str
    public_base_url: str  # no trailing slash; https:// (we derive wss:// from it)


def resolve_twilio_config(call_details: dict[str, Any] | None = None) -> TwilioConfig:
    cd = call_details or {}
    account_sid = cd.get("account_sid") or os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = cd.get("auth_token") or os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = (
        cd.get("from")
        or cd.get("from_number")
        or os.environ.get("TWILIO_FROM_NUMBER")
    )
    public_base_url = cd.get("public_base_url") or os.environ.get("PUBLIC_BASE_URL")

    missing = [
        name
        for name, val in (
            ("TWILIO_ACCOUNT_SID", account_sid),
            ("TWILIO_AUTH_TOKEN", auth_token),
            ("TWILIO_FROM_NUMBER", from_number),
            ("PUBLIC_BASE_URL", public_base_url),
        )
        if not val
    ]
    if missing:
        raise ValueError(
            f"Twilio config missing: {missing}. Set the env vars or pass them in "
            "call_details (account_sid, auth_token, from, public_base_url)."
        )
    return TwilioConfig(
        account_sid=account_sid,  # type: ignore[arg-type]
        auth_token=auth_token,  # type: ignore[arg-type]
        from_number=from_number,  # type: ignore[arg-type]
        public_base_url=public_base_url.rstrip("/"),  # type: ignore[union-attr]
    )


def place_call(
    *,
    config: TwilioConfig,
    to: str,
    voice_url: str,
    status_callback: str | None = None,
    http: Any | None = None,
) -> dict[str, Any]:
    """Place an outbound call. `voice_url` is the TwiML endpoint Twilio fetches
    when the callee answers (it returns the <Connect><Stream> for this session).
    Returns Twilio's parsed Call resource (`sid`, `status`, ...)."""
    if http is None:
        import httpx  # noqa: PLC0415

        http = httpx.Client(timeout=15.0)
    data: dict[str, str] = {"To": to, "From": config.from_number, "Url": voice_url}
    if status_callback:
        data["StatusCallback"] = status_callback
        data["StatusCallbackEvent"] = "completed"
    resp = http.post(
        CALLS_URL.format(sid=config.account_sid),
        data=data,
        auth=(config.account_sid, config.auth_token),
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Twilio call create failed ({resp.status_code}): {resp.text}"
        )
    return resp.json()

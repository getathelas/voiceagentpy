"""Server-side telephony: Twilio media-stream bridge.

Unlike the browser transport (audio goes direct to the provider over WebRTC),
telephony requires Python to sit in the audio path: Twilio streams μ-law 8 kHz
audio to us, we transcode and pump it to the provider's realtime WebSocket, and
pump model audio back. This package holds that bridge.
"""

from __future__ import annotations

from .audio import (
    TWILIO_FRAME_BYTES,
    iter_frames,
    mulaw8k_to_pcm16_24k,
    pcm16_24k_to_mulaw8k,
)
from .bridge import MediaBridge
from .control_plane import ControlPlane, HttpControlPlane, InProcessControlPlane

__all__ = [
    "TWILIO_FRAME_BYTES",
    "iter_frames",
    "mulaw8k_to_pcm16_24k",
    "pcm16_24k_to_mulaw8k",
    "MediaBridge",
    "ControlPlane",
    "InProcessControlPlane",
    "HttpControlPlane",
]

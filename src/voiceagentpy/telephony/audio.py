"""G.711 μ-law ↔ PCM16 transcoding and 8 kHz ↔ 24 kHz resampling.

Twilio Media Streams carry **8 kHz μ-law mono** in 20 ms frames (160 bytes).
The xAI / OpenAI realtime WebSockets carry **16-bit little-endian PCM mono at
24 kHz**. The media bridge has to convert between the two on every frame, both
directions.

We ship our own codec on purpose: Python's `audioop` (which had `ulaw2lin`,
`lin2ulaw`, `ratecv`) was deprecated in 3.11 and **removed in 3.13**, and we
don't want to pull a C extension just for telephony. μ-law is a fixed 256-entry
table and 8↔24 kHz is an exact 1:3 ratio, so pure Python is correct and fast
enough for one voice stream (24k samples/s).

All functions are stateless and operate on `bytes`. The resamplers use
per-call linear interpolation / 3-tap averaging; block-boundary discontinuities
are inaudible at telephony quality for v1. If artifacts ever matter, swap in a
stateful resampler behind these same signatures.
"""

from __future__ import annotations

import array
import sys
from typing import Iterator

# 20 ms of 8 kHz μ-law mono = 160 samples = 160 bytes — Twilio's frame size.
TWILIO_FRAME_BYTES = 160

_BIAS = 0x84
_CLIP = 32635
_LE = sys.byteorder == "little"


# --- μ-law codec -----------------------------------------------------------

def _build_ulaw_decode_table() -> array.array:
    """256-entry μ-law byte -> signed 16-bit linear (CCITT G.711 / Sun ref)."""
    table = array.array("h", [0] * 256)
    for byte in range(256):
        u = ~byte & 0xFF
        sign = u & 0x80
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
        table[byte] = -sample if sign else sample
    return table


_ULAW_DECODE = _build_ulaw_decode_table()


def _linear_to_ulaw(sample: int) -> int:
    """One signed 16-bit PCM sample -> one μ-law byte (G.711)."""
    sign = 0x80 if sample < 0 else 0x00
    if sign:
        sample = -sample
    if sample > _CLIP:
        sample = _CLIP
    sample += _BIAS
    seg = (sample >> 7) & 0xFF
    # G.711 segment->exponent: 0,1->0  2,3->1  4-7->2 ... i.e. bit_length()-1.
    exponent = 0 if seg == 0 else min(7, seg.bit_length() - 1)
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def ulaw_to_pcm16(mulaw: bytes) -> array.array:
    """μ-law bytes -> array('h') of signed 16-bit linear samples."""
    return array.array("h", (_ULAW_DECODE[b] for b in mulaw))


def pcm16_to_ulaw(samples: array.array) -> bytes:
    """array('h') of signed 16-bit linear samples -> μ-law bytes."""
    return bytes(_linear_to_ulaw(s) for s in samples)


# --- PCM helpers -----------------------------------------------------------

def _pcm_bytes_to_samples(pcm: bytes) -> array.array:
    a = array.array("h")
    a.frombytes(pcm)
    if not _LE:
        a.byteswap()
    return a


def _samples_to_pcm_bytes(samples: array.array) -> bytes:
    if _LE:
        return samples.tobytes()
    swapped = array.array("h", samples)
    swapped.byteswap()
    return swapped.tobytes()


# --- resampling (exact 1:3 ratio between 8 kHz and 24 kHz) -----------------

def _upsample_x3(samples: array.array) -> array.array:
    """8 kHz -> 24 kHz via linear interpolation (each input gap -> 3 outputs)."""
    n = len(samples)
    if n == 0:
        return array.array("h")
    out = array.array("h", [0] * (n * 3))
    for i in range(n):
        cur = samples[i]
        nxt = samples[i + 1] if i + 1 < n else cur
        out[i * 3] = cur
        out[i * 3 + 1] = (2 * cur + nxt) // 3
        out[i * 3 + 2] = (cur + 2 * nxt) // 3
    return out


def _downsample_x3(samples: array.array) -> array.array:
    """24 kHz -> 8 kHz via 3-tap averaging (crude low-pass + decimate)."""
    n = len(samples)
    out = array.array("h", [0] * ((n + 2) // 3))
    for j in range(len(out)):
        base = j * 3
        group = samples[base:base + 3]
        out[j] = sum(group) // len(group)
    return out


# --- public bridge conversions ---------------------------------------------

def mulaw8k_to_pcm16_24k(mulaw: bytes) -> bytes:
    """Twilio inbound frame -> provider input. μ-law 8k -> PCM16 LE 24k."""
    return _samples_to_pcm_bytes(_upsample_x3(ulaw_to_pcm16(mulaw)))


def pcm16_24k_to_mulaw8k(pcm: bytes) -> bytes:
    """Provider output -> Twilio outbound. PCM16 LE 24k -> μ-law 8k."""
    return pcm16_to_ulaw(_downsample_x3(_pcm_bytes_to_samples(pcm)))


def iter_frames(data: bytes, size: int = TWILIO_FRAME_BYTES) -> Iterator[bytes]:
    """Yield fixed-size frames from a byte buffer.

    The final partial chunk is zero-padded so Twilio always gets whole 20 ms
    frames (silence padding is inaudible and keeps stream timing aligned).
    """
    for i in range(0, len(data), size):
        chunk = data[i:i + size]
        if len(chunk) < size:
            chunk = chunk + b"\xff" * (size - len(chunk))  # 0xff = μ-law silence
        yield chunk

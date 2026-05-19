"""Audio transcoder tests — no network, pure byte math."""

from __future__ import annotations

import array
import math

from voiceagentpy.telephony.audio import (
    TWILIO_FRAME_BYTES,
    _linear_to_ulaw,
    _ULAW_DECODE,
    iter_frames,
    mulaw8k_to_pcm16_24k,
    pcm16_24k_to_mulaw8k,
)


def test_ulaw_codec_is_decode_stable():
    """Re-encoding a decoded sample must decode back to the same value for
    every code. (encode(decode(u))==u holds for all codes *except* the G.711
    negative-zero 0x7F, which by spec collapses onto 0xFF — so we assert the
    universal invariant: decode∘encode∘decode is a fixed point.)"""
    for u in range(256):
        v = _ULAW_DECODE[u]
        assert _ULAW_DECODE[_linear_to_ulaw(v)] == v
    # And the only non-idempotent code is exactly the negative-zero alias.
    offenders = [u for u in range(256) if _linear_to_ulaw(_ULAW_DECODE[u]) != u]
    assert offenders == [0x7F]


def test_ulaw_silence_byte_decodes_near_zero():
    assert abs(_ULAW_DECODE[0xFF]) <= 8


def test_mulaw8k_to_pcm16_24k_length():
    # N μ-law bytes -> N PCM16 samples -> *3 upsample -> *2 bytes/sample.
    mulaw = bytes(range(256))
    pcm = mulaw8k_to_pcm16_24k(mulaw)
    assert len(pcm) == len(mulaw) * 3 * 2


def test_pcm16_24k_to_mulaw8k_length():
    samples = array.array("h", [0] * 720)  # 30 ms @ 24 kHz
    pcm = samples.tobytes()
    mulaw = pcm16_24k_to_mulaw8k(pcm)
    assert len(mulaw) == 720 // 3


def test_round_trip_preserves_a_sine_wave():
    """μ-law + the crude 8k↔24k resampler are lossy by design (the chain is a
    mild low-pass). The waveform must still come back strongly correlated with
    the original and at comparable energy — i.e. recognizable speech, not
    garbage or silence."""
    from voiceagentpy.telephony.audio import ulaw_to_pcm16

    sr = 8000
    samples = array.array(
        "h",
        [int(8000 * math.sin(2 * math.pi * 440 * n / sr)) for n in range(sr // 10)],
    )
    mulaw_in = pcm16_24k_to_mulaw8k(
        mulaw8k_to_pcm16_24k(bytes(_linear_to_ulaw(s) for s in samples))
    )
    back = ulaw_to_pcm16(mulaw_in)
    assert len(back) == len(samples)

    n = len(samples)
    mean_a = sum(samples) / n
    mean_b = sum(back) / n
    cov = sum((a - mean_a) * (b - mean_b) for a, b in zip(samples, back))
    var_a = sum((a - mean_a) ** 2 for a in samples)
    var_b = sum((b - mean_b) ** 2 for b in back)
    corr = cov / math.sqrt(var_a * var_b)
    energy_ratio = math.sqrt(var_b / var_a)

    assert corr > 0.97          # waveform shape preserved
    assert 0.7 < energy_ratio < 1.05  # amplitude in the same ballpark


def test_iter_frames_pads_last_frame():
    frames = list(iter_frames(b"\x10" * (TWILIO_FRAME_BYTES + 5)))
    assert len(frames) == 2
    assert all(len(f) == TWILIO_FRAME_BYTES for f in frames)
    assert frames[1].endswith(b"\xff")  # padded with μ-law silence


def test_iter_frames_exact_multiple():
    frames = list(iter_frames(b"\x00" * (TWILIO_FRAME_BYTES * 3)))
    assert len(frames) == 3
    assert all(len(f) == TWILIO_FRAME_BYTES for f in frames)

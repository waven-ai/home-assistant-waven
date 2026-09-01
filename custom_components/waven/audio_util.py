"""Pure-Python audio helpers — no Home Assistant or numpy dependency.

HA's Assist pipeline streams STT audio as 16-bit signed PCM, little-endian,
16 kHz, mono. Waven's streaming STT WebSocket wants 32-bit float PCM,
little-endian, at the same rate/channel layout ("pcm_f32le"). The conversion
is a straight per-sample divide by 32768; we do it with the stdlib ``array``
module so the integration carries no extra requirements (HA bundles aiohttp
but not numpy, and a TTS/STT proxy has no business pulling in a BLAS stack).

Only the s16le->f32le direction is needed. The inverse used to live here,
documented as "used by the Wyoming TTS proxy" — it never was: the proxy is
built to import nothing from this package, and it hands Wyoming the PCM frames
the stdlib ``wave`` module unpacks from the backend's WAV. It was dead code
with a misleading comment and a per-sample Python loop, so it is gone; the
proxy is unaffected.

Kept import-free of ``homeassistant`` so it is unit-testable standalone.
"""

from __future__ import annotations

import array
import sys

# Full-scale for signed 16-bit. Dividing by 32768 keeps the result in
# [-1.0, 1.0): -32768 maps to -1.0 and +32767 maps to ~0.99997, which is the
# convention every float-PCM consumer (including Waven's upstream) expects.
_INT16_FULL_SCALE = 32768.0


def pcm_s16le_to_f32le(data: bytes) -> bytes:
    """Convert signed-16-bit little-endian PCM to 32-bit-float little-endian.

    ``data`` length is expected to be a multiple of 2 (one int16 per sample).
    A trailing odd byte (a torn frame) is dropped rather than raising, so a
    mid-utterance disconnect can't crash the stream.
    """
    if not data:
        return b""
    if len(data) % 2:
        data = data[:-1]

    samples = array.array("h")  # signed short, 2 bytes
    samples.frombytes(data)
    # ``array`` uses host byte order; the wire format is little-endian. Swap on
    # big-endian hosts so we interpret the int16s correctly before scaling.
    if sys.byteorder == "big":
        samples.byteswap()

    floats = array.array("f", (s / _INT16_FULL_SCALE for s in samples))
    if sys.byteorder == "big":
        floats.byteswap()
    return floats.tobytes()


def f32le_seconds(num_bytes: int, sample_rate: int = 16000, channels: int = 1) -> float:
    """Duration in seconds of ``num_bytes`` of float32 PCM. Mirrors the
    backend's ``pcm_f32le_seconds`` arithmetic so our local usage accounting
    agrees with what the server bills."""
    bytes_per_second = sample_rate * channels * 4
    if bytes_per_second <= 0:
        return 0.0
    return num_bytes / bytes_per_second


def s16le_seconds(num_bytes: int, sample_rate: int = 16000, channels: int = 1) -> float:
    """Duration in seconds of ``num_bytes`` of signed-16-bit PCM."""
    bytes_per_second = sample_rate * channels * 2
    if bytes_per_second <= 0:
        return 0.0
    return num_bytes / bytes_per_second

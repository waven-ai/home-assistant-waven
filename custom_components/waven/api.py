"""Async Waven API client used by the Home Assistant integration.

Thin network layer over the endpoints verified against ``backend/app/routers``:

  * ``GET  /api/v1/user``                   — validate key, read tier + quota
  * ``GET  /api/v1/voice-gallery``          — list cloned voices
  * ``POST /api/v1/generate`` (multipart)   — TTS → ``{file: /api/v1/audio/...}``
  * ``GET  /api/v1/audio/{uid}/{name}``     — fetch the rendered audio bytes
  * ``WS   /api/v1/stt/transcribe/stream``  — streaming STT

All request-building and audio conversion lives in the pure helpers
(``routing.py``, ``audio_util.py``); this module adds only aiohttp, auth
headers, retries and error mapping. It is the one integration module that
depends on aiohttp (which Home Assistant bundles).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import aiohttp

from . import audio_util
from .const import (
    DEFAULT_STT_LANGUAGE,
    PATH_GENERATE,
    PATH_STT_STREAM,
    PATH_USER,
    PATH_VOICE_GALLERY,
    RETENTION_HEADER,
    SOURCE_HEADER,
    SOURCE_VALUE,
    STT_MODE_AUTO,
    STT_WS_FORMAT,
)
from .routing import Account, GalleryVoice, TtsSelection, parse_account, parse_gallery

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

# Tight-ish timeouts: HA voice is interactive. A stuck request should fail fast
# so Assist ends the run with a `tts-failed`/empty-transcript instead of hanging
# the pipeline (spec §8) — a fast silence beats a wedged one.
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)

# Backoff between the two attempts of a retried leg (see `_retry_once`).
_RETRY_BACKOFF_SECONDS = 0.25

# --- WebSocket timeouts ------------------------------------------------------
# `ws_connect(timeout=...)` does NOT take a ClientTimeout: since aiohttp 3.10 a
# ClientTimeout passed here is funnelled through a deprecation branch that reads
# only its `total` as the *close* timeout, leaving `ws_receive` unset. The
# receive budget we actually want then never applies — a backend that answers
# pings but stops sending frames hangs `async for msg in ws` forever — and the
# eventual close() raises a swallowed TypeError, so every stream reports close
# code 1006. The correct type is `aiohttp.ClientWSTimeout`.
#
# HA 2025.1 (our hacs.json floor) ships aiohttp 3.11.x, so ClientWSTimeout is
# always present at runtime; the getattr guard only keeps an older aiohttp in a
# bare test environment working, where the pre-3.10 signature took a bare float
# receive timeout.
_WS_RECEIVE_TIMEOUT_SECONDS = 60.0
_WS_CLOSE_TIMEOUT_SECONDS = 10.0
_ClientWSTimeout = getattr(aiohttp, "ClientWSTimeout", None)
if _ClientWSTimeout is not None:
    _WS_TIMEOUT: object = _ClientWSTimeout(
        ws_receive=_WS_RECEIVE_TIMEOUT_SECONDS,
        ws_close=_WS_CLOSE_TIMEOUT_SECONDS,
    )
else:  # pragma: no cover - aiohttp < 3.10, below every supported HA release
    _WS_TIMEOUT = _WS_RECEIVE_TIMEOUT_SECONDS


class WavenError(Exception):
    """Base error for all Waven API failures."""


class WavenAuthError(WavenError):
    """Invalid API key, or the account is disabled (HTTP 401/403, WS 4001/4003)."""


class WavenQuotaError(WavenError):
    """Server-side usage limit hit (HTTP 402/429, WS 1008)."""


class WavenConsentError(WavenError):
    """Account hasn't accepted the current ToS/Privacy (HTTP 428). The TTS
    (`/generate`) and batch-STT (`/transcribe`) endpoints gate on consent; the
    streaming-STT WebSocket does not. Surfaced so the entity can tell the user
    to accept the terms in the dashboard rather than logging an opaque 4xx."""


class WavenConnectionError(WavenError):
    """Network failure, timeout, or a 5xx the retry didn't recover."""


@dataclass
class TtsResult:
    extension: str
    audio: bytes
    duration_seconds: float


@dataclass
class SttResult:
    text: str
    audio_seconds: float


class WavenClient:
    """Stateless-ish client. One per config entry; reuses HA's shared aiohttp
    session so connection pooling and DNS caching are handled by HA."""

    def __init__(self, session: aiohttp.ClientSession, api_key: str, host: str) -> None:
        self._session = session
        self._api_key = api_key
        self._host = host.rstrip("/")

    # --- helpers -------------------------------------------------------------
    def _headers(self, retain_audio: bool = True) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            SOURCE_HEADER: SOURCE_VALUE,
        }
        if not retain_audio:
            headers[RETENTION_HEADER] = "false"
        return headers

    @property
    def _ws_base(self) -> str:
        if self._host.startswith("https://"):
            return "wss://" + self._host[len("https://"):]
        if self._host.startswith("http://"):
            return "ws://" + self._host[len("http://"):]
        return self._host

    @staticmethod
    def _raise_for_status(status: int, body: str = "") -> None:
        if status in (401, 403):
            raise WavenAuthError(f"Authentication failed ({status}): {body[:200]}")
        if status == 428:
            raise WavenConsentError(
                "This Waven account hasn't accepted the current Terms of Service "
                "and Privacy Policy. Accept them in the Waven dashboard, then retry."
            )
        if status in (402, 429):
            raise WavenQuotaError(f"Usage limit reached ({status}): {body[:200]}")
        if status >= 500:
            raise WavenConnectionError(f"Server error ({status}): {body[:200]}")
        if status >= 400:
            raise WavenError(f"Request failed ({status}): {body[:200]}")

    # --- account / voices ----------------------------------------------------
    async def async_validate(self, retain_audio: bool = True) -> Account:
        """``GET /api/v1/user`` — also the config-flow key validator. Raises
        :class:`WavenAuthError` on a bad key."""
        try:
            async with self._session.get(
                f"{self._host}{PATH_USER}",
                headers=self._headers(retain_audio),
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    self._raise_for_status(resp.status, await resp.text())
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise WavenConnectionError(f"Could not reach Waven: {err}") from err
        return parse_account(data)

    async def async_list_voices(self, retain_audio: bool = True) -> list[GalleryVoice]:
        """``GET /api/v1/voice-gallery`` — the user's cloned voices. Returns an
        empty list (never raises) on transient errors so the picker degrades to
        stock-voices-only rather than blocking the options flow.

        Takes ``retain_audio`` for the same reason every other call does: the
        privacy copy promises the opt-out header on *every* request, so the
        household's preference must reach the header builder here too, not
        only on the calls that move audio.
        """
        try:
            async with self._session.get(
                f"{self._host}{PATH_VOICE_GALLERY}",
                headers=self._headers(retain_audio),
                params={"limit": "200"},
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug("voice-gallery list returned %s", resp.status)
                    return []
                return parse_gallery(await resp.json())
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("voice-gallery list failed: %s", err)
            return []

    # --- TTS -----------------------------------------------------------------
    async def async_generate_tts(
        self,
        text: str,
        selection: TtsSelection,
        fmt: str,
        language: str | None,
        retain_audio: bool = True,
    ) -> TtsResult:
        """``POST /api/v1/generate``, then fetch the rendered file.

        Each leg gets one retry on a transient failure (spec §8 — TTS responses
        are short, so a single retry fits the interactive budget), but the two
        legs retry **independently and the POST is never replayed once it has
        succeeded**. Re-running the whole round trip because the *audio fetch*
        failed would re-synthesise the text: a second upstream inference, a
        second ``Generation`` row, and a second billed minute for one spoken
        response. After a successful POST only ``_fetch_audio`` is retried —
        the rendered file already exists server-side, so re-GETting it is free.

        **Except under ``retain_audio=False``.** A flagged clip is burned by
        the first *complete* GET: the backend deletes file + sidecar as soon as
        that response has been sent. A ranged read (HTTP ``Range``) deliberately
        does NOT burn it — a browser or media player probing the file must not
        be able to destroy it — but ``_fetch_audio`` never sends ``Range``, so
        every fetch from here is the burning kind. A full download that dies
        mid-transfer burns it too (the server cannot tell a finished download
        from a dropped one), so the retry gets 404 → :class:`WavenError`, which
        ``_retry_once`` treats as terminal (only :class:`WavenConnectionError`
        is retried) and ``tts.py`` turns into one lost utterance. That is
        deliberate — privacy over retention. The retry still earns its keep for
        the flagged case's *other* failure shapes (connect/DNS/timeout before
        any bytes were served, 5xx), where the file is untouched.
        """
        file_path, duration = await self._retry_once(
            lambda: self._post_generate(text, selection, fmt, language, retain_audio),
            "TTS generate",
        )
        audio = await self._retry_once(
            lambda: self._fetch_audio(file_path, retain_audio), "TTS audio fetch"
        )
        return TtsResult(extension=fmt, audio=audio, duration_seconds=duration)

    @staticmethod
    async def _retry_once(
        call: Callable[[], Awaitable[_T]], label: str
    ) -> _T:
        """Run ``call``, retrying it exactly once on a transient failure.

        Only :class:`WavenConnectionError` (network/timeout/5xx) is retried —
        auth, quota, consent and content errors are terminal and re-issuing
        them just burns the interactive budget.
        """
        for attempt in (1, 2):
            try:
                return await call()
            except WavenConnectionError as err:
                if attempt == 1:
                    _LOGGER.debug("%s attempt 1 failed (%s); retrying once", label, err)
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    async def _post_generate(
        self,
        text: str,
        selection: TtsSelection,
        fmt: str,
        language: str | None,
        retain_audio: bool,
    ) -> tuple[str, float]:
        """The billable leg: one POST → ``(file_path, duration_seconds)``."""
        form = aiohttp.FormData()
        form.add_field("model", selection.model)
        form.add_field("text", text)
        form.add_field("format", fmt)
        form.add_field("language", language or "Auto")
        if selection.gallery_voice_id:
            form.add_field("gallery_voice_id", selection.gallery_voice_id)
        elif selection.speaker:
            form.add_field("speaker", selection.speaker)

        try:
            async with self._session.post(
                f"{self._host}{PATH_GENERATE}",
                headers=self._headers(retain_audio),
                data=form,
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    self._raise_for_status(resp.status, await resp.text())
                meta = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise WavenConnectionError(f"TTS request failed: {err}") from err

        file_path = meta.get("file")
        if not file_path:
            raise WavenError("TTS response missing audio file reference")
        return str(file_path), float(meta.get("duration") or 0.0)

    async def _fetch_audio(self, file_path: str, retain_audio: bool = True) -> bytes:
        """The download leg. Plain (non-Range) GET on purpose: a ranged read
        does not burn a flagged clip server-side, and pretending to stream one
        would leave the file behind for the sweep to mop up instead of having
        it deleted the moment we have it."""
        url = file_path if file_path.startswith("http") else f"{self._host}{file_path}"
        try:
            async with self._session.get(
                url, headers=self._headers(retain_audio), timeout=_HTTP_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    self._raise_for_status(resp.status, await resp.text())
                return await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise WavenConnectionError(f"Audio fetch failed: {err}") from err

    # --- STT (streaming) -----------------------------------------------------
    async def async_stream_stt(
        self,
        audio: AsyncIterable[bytes],
        mode: str = STT_MODE_AUTO,
        language: str | None = None,
        retain_audio: bool = True,
    ) -> SttResult:
        """Open the streaming WS, push the (s16le→f32le-converted) utterance,
        and return the final transcript.

        ``audio`` yields raw 16-bit-PCM mono 16 kHz chunks exactly as HA's
        Assist pipeline produces them; we convert to the float32 format the
        backend wants frame-by-frame so memory stays flat.
        """
        url = f"{self._ws_base}{PATH_STT_STREAM}"
        config: dict = {"format": STT_WS_FORMAT}
        # "auto" → omit latency_mode and let the backend resolve from language.
        if mode and mode != STT_MODE_AUTO:
            config["latency_mode"] = mode
        config["language"] = language or DEFAULT_STT_LANGUAGE

        final_text = ""
        f32_bytes = 0
        try:
            async with self._session.ws_connect(
                url,
                headers=self._headers(retain_audio),
                timeout=_WS_TIMEOUT,
                heartbeat=20,
            ) as ws:
                await ws.send_json(config)

                async for chunk in audio:
                    if not chunk:
                        continue
                    converted = audio_util.pcm_s16le_to_f32le(chunk)
                    f32_bytes += len(converted)
                    await ws.send_bytes(converted)

                await ws.send_json({"type": "end"})

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = msg.json()
                        mtype = data.get("type")
                        if mtype == "final":
                            final_text = data.get("text", "") or ""
                            break
                        if mtype == "error":
                            raise WavenError(data.get("detail") or "STT error")
                        # "partial" frames are ignored — HA's pipeline only
                        # consumes the final transcript from this entity.
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                    ):
                        break
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        raise WavenConnectionError("STT WebSocket error")

                self._raise_for_ws_close(ws.close_code)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise WavenConnectionError(f"STT stream failed: {err}") from err

        return SttResult(text=final_text, audio_seconds=audio_util.f32le_seconds(f32_bytes))

    @staticmethod
    def _raise_for_ws_close(code: int | None) -> None:
        """Map WS close codes (transcribe_stream.py) to client errors. Normal
        (1000/None) and our own protocol close are fine."""
        if code in (None, 1000, 1005):
            return
        if code in (4001, 4003):
            raise WavenAuthError(f"STT auth/account error (close {code})")
        if code == 1008:
            raise WavenQuotaError("STT usage/rate limit reached (close 1008)")
        if code == 1009:
            raise WavenConnectionError("STT utterance exceeded the streaming buffer (close 1009)")
        if code == 4500:
            raise WavenConnectionError("STT backend unavailable (close 4500)")
        if code == 1002:
            # Protocol/config rejection: bad audio format, unknown engine, or a
            # non-JSON first frame. A content-level error, not a network one.
            raise WavenError("STT config rejected by server (bad format/engine; close 1002)")
        raise WavenError(f"STT stream closed unexpectedly (close {code})")

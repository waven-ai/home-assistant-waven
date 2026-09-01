"""Constants for the Waven Home Assistant integration.

This module is intentionally free of any `homeassistant` imports so the
constants — and the small pure helpers that depend on them (see
``routing.py``, ``quota.py``, ``audio_util.py``) — can be unit-tested
without a Home Assistant runtime.

The integration plugs Waven's hosted STT + TTS into HA's Assist voice
pipeline. Wake word, VAD and intent matching stay local; only the
post-wake utterance (STT) and the response text (TTS) cross the WAN.
See ``docs/home-assistant-integration-spec.md`` in the monorepo for the
product rationale.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "waven"

# --- API surface (verified against backend/app/routers) ----------------------
# Base hosts per region. Only `us` is live today; `eu` is a placeholder so the
# region picker is a one-line change once the EU edge ships (spec §6). The
# config flow only offers regions whose host is non-empty.
REGION_HOSTS: Final[dict[str, str]] = {
    "us": "https://api.waven.ai",
    # "eu": "https://eu.api.waven.ai",  # gated until the EU region exists
}
DEFAULT_REGION: Final = "us"

# Endpoint paths (all relative to the region host).
PATH_USER: Final = "/api/v1/user"                       # GET — validate key + tier + quota
PATH_VOICE_GALLERY: Final = "/api/v1/voice-gallery"     # GET — list cloned/gallery voices
PATH_GENERATE: Final = "/api/v1/generate"               # POST (multipart) — TTS
PATH_STT_STREAM: Final = "/api/v1/stt/transcribe/stream"  # WS — streaming STT
PATH_STT_BATCH: Final = "/api/v1/stt/transcribe"        # POST (multipart) — batch STT fallback

# Header the integration stamps on every request. The backend READS it today
# (spec §4 — display-only attribution, never gates admission and never splits
# the quota pool): `backend/app/request_source.normalize_source` allowlists it
# and folds it into the stored `source="ha"` column on the Generation /
# Transcription row, which surfaces as `usage.source_breakdown.ha` in
# `GET /api/v1/user`.
#
# SOURCE_VALUE is therefore a WIRE CONTRACT, not a free-form label. The backend
# allowlist recognises exactly "home-assistant" (this integration) and
# "home-assistant-wyoming" (the Wyoming proxy); `normalize_source` maps anything
# else — including a plausible-looking rename like "homeassistant" or "ha" — to
# None, i.e. the NULL "not attributed" bucket. Renaming this constant would not
# error anywhere: it would silently zero the user's Home Assistant usage
# breakdown. Change it only together with `_SOURCE_MAP` in request_source.py.
SOURCE_HEADER: Final = "X-Waven-Source"
SOURCE_VALUE: Final = "home-assistant"
# Per-request retention opt-out (spec §6). ENFORCED SERVER-SIDE: the backend
# reads this header (`backend/app/request_source.retention_opted_out`) and, for
# a flagged POST /api/v1/generate, deletes the synthesized clip as soon as this
# integration has downloaded it in full. A *ranged* read (HTTP Range) does not
# burn the clip — a media player probing the file must not be able to destroy
# it — and `api._fetch_audio` never sends one, so every fetch we make is the
# burning kind.
#
# A clip that is never downloaded falls to the cleanup sweep, which drops it
# once it is older than `NO_RETAIN_AUDIO_TTL_MINUTES` (default 10) rather than
# the standard `AUDIO_TTL_HOURS` (72 h) that unflagged clips get. The worst
# case is NOT 10 minutes: 10 (TTL) + `AUDIO_CLEANUP_MIN_INTERVAL_SECONDS`
# (3600, the minimum gap between sweeps) + one `CLEANUP_INTERVAL_SECONDS`
# (300) tick to notice that gap has elapsed ≈ 75 minutes at the defaults. A
# sweep that stops running entirely is alerted on at 2 h (CleanupWedged).
#
# Scope: /generate is the only TTS endpoint this integration calls, so it is
# the only one wired (/generate-long and the async TTS job path are a
# documented backend follow-up). The STT endpoint this integration uses (the
# WS stream /api/v1/stt/transcribe/stream; the Wyoming proxy's batch
# POST /api/v1/stt/transcribe behaves the same way) never persists utterance
# audio server-side, flag or no flag, so there is nothing to drop there — the
# async job endpoint POST /api/v1/stt/transcribe/jobs, which neither HA client
# calls, DOES hold the upload in Redis for 1 h and sits in the same follow-up
# bucket. Gallery-voice reference audio is user-managed persistent storage and
# deliberately unaffected — deleting the voice removes it.
RETENTION_HEADER: Final = "X-Waven-Retain-Audio"

# --- Config / options keys ---------------------------------------------------
CONF_API_KEY: Final = "api_key"
CONF_REGION: Final = "region"

CONF_STT_MODE: Final = "stt_mode"
CONF_STT_LANGUAGE: Final = "stt_language"

# Hybrid voice routing (spec §5): a voice per response category.
CONF_VOICE_ACK: Final = "voice_ack"
CONF_VOICE_CONFIRMATION: Final = "voice_confirmation"
CONF_VOICE_ANNOUNCEMENT: Final = "voice_announcement"
CONF_VOICE_ERROR: Final = "voice_error"

CONF_TTS_FORMAT: Final = "tts_format"

CONF_DAILY_CAP_MINUTES: Final = "daily_cap_minutes"
CONF_RETAIN_AUDIO: Final = "retain_audio"
CONF_AUDIT_ENABLED: Final = "audit_enabled"

# --- STT modes ---------------------------------------------------------------
# Maps the user-facing mode onto the backend streaming config frame's
# `latency_mode`. "auto" omits latency_mode and lets the backend resolve an
# engine from the language (resolve_stt_stream_engine).
STT_MODE_AUTO: Final = "auto"
# Legacy alias: Moonshine was retired (docs/ADR-stt-model-unification.md), so the
# backend now treats latency_mode="fast" as a DEPRECATED alias that resolves to
# parakeet — i.e. identical to "accurate". The wire value is kept for backward
# compatibility with entries saved before the retirement; new setups should pick
# Accurate. See strings.json for the user-facing "(legacy)" label.
STT_MODE_FAST: Final = "fast"            # deprecated alias of parakeet (== accurate)
STT_MODE_MULTILINGUAL: Final = "multilingual"  # voxtral (en/fr/de/es/it/pt)
STT_MODE_ACCURATE: Final = "accurate"    # parakeet (highest WER quality, English)
STT_MODES: Final = (
    STT_MODE_AUTO,
    STT_MODE_FAST,
    STT_MODE_MULTILINGUAL,
    STT_MODE_ACCURATE,
)
DEFAULT_STT_MODE: Final = STT_MODE_AUTO
DEFAULT_STT_LANGUAGE: Final = "en"

# Languages we advertise to Assist for STT. Voxtral covers the streaming
# multilingual set; English is always available via Parakeet.
STT_SUPPORTED_LANGUAGES: Final = ["en", "fr", "de", "es", "it", "pt"]

# --- Audio formats -----------------------------------------------------------
# HA's Assist pipeline hands STT raw PCM: 16-bit signed LE, 16 kHz, mono.
# Waven's streaming STT wants 32-bit float LE ("pcm_f32le") at the same rate.
STT_SAMPLE_RATE: Final = 16000
STT_CHANNELS: Final = 1
STT_SAMPLE_WIDTH: Final = 2  # bytes per sample of the *incoming* s16le stream
STT_WS_FORMAT: Final = "pcm_f32le"

# TTS output container we ask the backend for. WAV plays everywhere in HA and
# avoids a transcode round-trip on the backend; mp3/ogg are also accepted.
TTS_FORMAT_WAV: Final = "wav"
TTS_FORMATS: Final = ("wav", "mp3", "ogg")
DEFAULT_TTS_FORMAT: Final = TTS_FORMAT_WAV

# Languages we advertise to Assist for TTS. Kokoro covers en variants; OmniVoice
# is broadly multilingual. Keep this curated — Assist only needs the codes a
# household is likely to select.
TTS_SUPPORTED_LANGUAGES: Final = [
    "en", "fr", "de", "es", "it", "pt", "nl", "ja", "zh", "hi",
]
DEFAULT_TTS_LANGUAGE: Final = "en"

# --- TTS models + voices -----------------------------------------------------
MODEL_KOKORO: Final = "kokoro"        # stock voices, low latency (spec: acks/confirmations/errors)
MODEL_OMNIVOICE: Final = "omnivoice"  # voice cloning via gallery (spec: long announcements)

# A voice is encoded as "<kind>:<id>" so a single options field round-trips both
# stock and cloned voices. parse_voice_value()/format_voice_value() in
# routing.py own the encoding.
VOICE_KIND_KOKORO: Final = "kokoro"
VOICE_KIND_GALLERY: Final = "gallery"

DEFAULT_KOKORO_VOICE: Final = "af_heart"
DEFAULT_VOICE_VALUE: Final = f"{VOICE_KIND_KOKORO}:{DEFAULT_KOKORO_VOICE}"

# Curated Kokoro stock voices. The container accepts any `<lang><gender>_<name>`
# id and falls back to af_heart for unknowns (tts/kokoro/README.md); this subset
# is what we surface in the picker. `gender`/`language` drive the spec's
# "pick a stock voice that roughly matches the clone's gender/accent" routing.
KOKORO_VOICES: Final[tuple[dict[str, str], ...]] = (
    {"id": "af_heart", "label": "Heart — US English, female", "gender": "female", "language": "en"},
    {"id": "af_bella", "label": "Bella — US English, female", "gender": "female", "language": "en"},
    {"id": "af_nicole", "label": "Nicole — US English, female", "gender": "female", "language": "en"},
    {"id": "af_sarah", "label": "Sarah — US English, female", "gender": "female", "language": "en"},
    {"id": "am_michael", "label": "Michael — US English, male", "gender": "male", "language": "en"},
    {"id": "am_adam", "label": "Adam — US English, male", "gender": "male", "language": "en"},
    {"id": "am_fenrir", "label": "Fenrir — US English, male", "gender": "male", "language": "en"},
    {"id": "bf_emma", "label": "Emma — UK English, female", "gender": "female", "language": "en"},
    {"id": "bf_isabella", "label": "Isabella — UK English, female", "gender": "female", "language": "en"},
    {"id": "bm_george", "label": "George — UK English, male", "gender": "male", "language": "en"},
    {"id": "bm_lewis", "label": "Lewis — UK English, male", "gender": "male", "language": "en"},
    {"id": "ef_dora", "label": "Dora — Spanish, female", "gender": "female", "language": "es"},
    {"id": "ff_siwis", "label": "Siwis — French, female", "gender": "female", "language": "fr"},
)

# --- Hybrid voice-routing categories (spec §5) -------------------------------
CATEGORY_ACK: Final = "ack"                    # "OK", "Done", "I didn't catch that"
CATEGORY_CONFIRMATION: Final = "confirmation"  # "Turning off the kitchen lights"
CATEGORY_ANNOUNCEMENT: Final = "announcement"  # morning briefing, story mode
CATEGORY_ERROR: Final = "error"                # "Something went wrong"
CATEGORIES: Final = (
    CATEGORY_ACK,
    CATEGORY_CONFIRMATION,
    CATEGORY_ANNOUNCEMENT,
    CATEGORY_ERROR,
)
# Default per-category voices. Quick/transactional/error responses use a fast
# Kokoro stock voice; long announcements use the cloned gallery voice once the
# user has assigned one (spec's hybrid policy keeps first-run impressions snappy).
DEFAULT_CATEGORY_VOICES: Final[dict[str, str]] = {
    CATEGORY_ACK: DEFAULT_VOICE_VALUE,
    CATEGORY_CONFIRMATION: DEFAULT_VOICE_VALUE,
    CATEGORY_ANNOUNCEMENT: DEFAULT_VOICE_VALUE,
    CATEGORY_ERROR: DEFAULT_VOICE_VALUE,
}
CONF_VOICE_BY_CATEGORY: Final[dict[str, str]] = {
    CATEGORY_ACK: CONF_VOICE_ACK,
    CATEGORY_CONFIRMATION: CONF_VOICE_CONFIRMATION,
    CATEGORY_ANNOUNCEMENT: CONF_VOICE_ANNOUNCEMENT,
    CATEGORY_ERROR: CONF_VOICE_ERROR,
}
DEFAULT_CATEGORY: Final = CATEGORY_CONFIRMATION

# TTS option keys callers can pass via the `tts.speak` service `options:` map.
OPTION_CATEGORY: Final = "category"  # one of CATEGORIES — selects the hybrid slot
OPTION_VOICE: Final = "voice"        # explicit "<kind>:<id>" override or a bare voice id

# --- Daily cap (spec §7) -----------------------------------------------------
DEFAULT_DAILY_CAP_MINUTES: Final = 30
MIN_DAILY_CAP_MINUTES: Final = 5
MAX_DAILY_CAP_MINUTES: Final = 1440  # a full day; the plan's monthly pool caps the rest
CAP_NOTIFY_PERCENTS: Final = (80, 100)

# How many recent requests the in-house audit ring buffer keeps. This is the
# "what did Waven hear last night?" card (spec §6); it is entirely client-side
# because the backend has no per-request audit endpoint.
AUDIT_LOG_MAX_ROWS: Final = 200

# --- Coordinator -------------------------------------------------------------
# Usage/quota poll interval. Usage moves slowly; 15 min is plenty and keeps us
# well under any per-account rate limit.
UPDATE_INTERVAL_SECONDS: Final = 900
# A cloud round-trip older than this flips the "cloud reachable" indicator and
# arms the local-fallback path (spec §8 network reliability).
REACHABLE_STALE_SECONDS: Final = 120

# Persistent-notification ids (so repeated fires replace rather than stack).
NOTIFY_CAP_80: Final = f"{DOMAIN}_cap_80"
NOTIFY_CAP_100: Final = f"{DOMAIN}_cap_100"
NOTIFY_CONSENT: Final = f"{DOMAIN}_consent_required"

ATTRIBUTION: Final = "Speech powered by Waven"

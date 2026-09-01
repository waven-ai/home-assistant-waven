"""Pure helpers for voice encoding, hybrid voice routing, and parsing the
Waven account/voice-gallery JSON. No ``homeassistant`` import — unit-testable.

The hybrid voice-routing policy is the product's wedge (spec §5): quick acks,
confirmations and errors use a fast Kokoro stock voice; long announcements use
the user's cloned gallery voice. The integration's options panel assigns a
voice per category; this module turns "(category, options)" into the concrete
(model, speaker | gallery_voice_id) tuple a ``/api/v1/generate`` call needs.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import (
    DEFAULT_CATEGORY,
    DEFAULT_CATEGORY_VOICES,
    DEFAULT_KOKORO_VOICE,
    MODEL_KOKORO,
    MODEL_OMNIVOICE,
    VOICE_KIND_GALLERY,
    VOICE_KIND_KOKORO,
)


# --- Voice value encoding ----------------------------------------------------
def format_voice_value(kind: str, voice_id: str) -> str:
    """Encode a voice as the ``"<kind>:<id>"`` string stored in options."""
    return f"{kind}:{voice_id}"


def parse_voice_value(value: str | None) -> tuple[str, str]:
    """Decode a stored/over­ridden voice value into ``(kind, id)``.

    Tolerant of three shapes so the ``tts.speak`` ``options.voice`` override is
    forgiving:
      * ``"kokoro:af_heart"`` / ``"gallery:<uuid>"`` — canonical.
      * a bare Kokoro speaker id like ``"af_heart"`` — treated as kokoro.
      * ``None``/empty — falls back to the default Kokoro voice.
    A ``"gallery:"`` prefix is required to reach the cloning path; a bare id is
    never guessed to be a gallery uuid.
    """
    if not value:
        return VOICE_KIND_KOKORO, DEFAULT_KOKORO_VOICE
    if ":" in value:
        kind, _, voice_id = value.partition(":")
        kind = kind.strip().lower()
        voice_id = voice_id.strip()
        if kind in (VOICE_KIND_KOKORO, VOICE_KIND_GALLERY) and voice_id:
            return kind, voice_id
        # Unknown prefix — treat the whole thing as a bare Kokoro id.
        return VOICE_KIND_KOKORO, value.strip()
    return VOICE_KIND_KOKORO, value.strip()


# --- Hybrid routing ----------------------------------------------------------
@dataclass(frozen=True)
class TtsSelection:
    """The concrete dispatch a TTS request resolves to."""

    model: str
    speaker: str | None = None
    gallery_voice_id: str | None = None
    category: str = DEFAULT_CATEGORY

    @property
    def is_clone(self) -> bool:
        return self.model == MODEL_OMNIVOICE and bool(self.gallery_voice_id)


def selection_from_voice_value(value: str | None, category: str = DEFAULT_CATEGORY) -> TtsSelection:
    """Turn a single ``"<kind>:<id>"`` value into a :class:`TtsSelection`."""
    kind, voice_id = parse_voice_value(value)
    if kind == VOICE_KIND_GALLERY:
        return TtsSelection(model=MODEL_OMNIVOICE, gallery_voice_id=voice_id, category=category)
    return TtsSelection(model=MODEL_KOKORO, speaker=voice_id, category=category)


def resolve_tts_request(
    options: dict | None,
    category_voices: dict[str, str],
) -> TtsSelection:
    """Resolve a TTS request to a concrete voice.

    Precedence:
      1. An explicit ``options.voice`` override always wins (a power user, an
         automation, or the spec's "always use clone regardless of latency").
      2. Otherwise the request's ``options.category`` (default
         ``confirmation``) selects the configured per-category voice.

    ``category_voices`` is the user's options-panel mapping
    (``{category: "<kind>:<id>"}``); missing entries fall back to the
    package defaults.
    """
    options = options or {}
    category = str(options.get("category") or DEFAULT_CATEGORY).lower()
    if category not in DEFAULT_CATEGORY_VOICES:
        category = DEFAULT_CATEGORY

    explicit = options.get("voice")
    if explicit:
        return selection_from_voice_value(str(explicit), category)

    value = category_voices.get(category) or DEFAULT_CATEGORY_VOICES[category]
    return selection_from_voice_value(value, category)


# --- Account + voice-gallery parsing -----------------------------------------
@dataclass
class Account:
    """The slice of ``GET /api/v1/user`` the integration cares about."""

    tier: str = "free"
    active: bool = True
    payg_enabled: bool = False
    tts_remaining: float | None = None
    tts_limit: float | None = None
    stt_remaining: float | None = None
    stt_limit: float | None = None
    email: str = ""


def _as_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_account(data: dict) -> Account:
    """Parse the user/quota JSON defensively. The combined pools are the
    enforced caps; per-engine fields are analytics-only.

    Note the asymmetry in ``GET /api/v1/user``: the TTS pool lives under
    ``usage`` (``tts_combined_*``), but the backend *pops* the STT block out of
    ``usage`` and re-homes it at the TOP LEVEL as ``stt_usage`` (users.py). So
    STT must be read from ``data["stt_usage"]``, not ``usage["stt"]`` — the
    latter is always absent. We keep a fallback to ``usage["stt"]`` for
    forward-compat in case the shape is ever unified."""
    usage = data.get("usage") or {}
    stt = data.get("stt_usage") or usage.get("stt") or {}
    return Account(
        tier=str(data.get("tier") or "free"),
        active=bool(data.get("active", True)),
        payg_enabled=bool(data.get("payg_enabled", False)),
        tts_remaining=_as_float(usage.get("tts_combined_remaining")),
        tts_limit=_as_float(usage.get("tts_combined_monthly_limit")),
        stt_remaining=_as_float(stt.get("combined_remaining")),
        stt_limit=_as_float(stt.get("combined_monthly_limit")),
        email=str(data.get("email") or ""),
    )


@dataclass
class GalleryVoice:
    id: str
    name: str
    gender: str | None = None
    language: str | None = None
    favorite: bool = False


def parse_gallery(data: dict) -> list[GalleryVoice]:
    """Parse ``GET /api/v1/voice-gallery`` into the fields the picker needs."""
    items = data.get("items") if isinstance(data, dict) else None
    out: list[GalleryVoice] = []
    for item in items or []:
        voice_id = item.get("id")
        if not voice_id:
            continue
        out.append(
            GalleryVoice(
                id=str(voice_id),
                name=str(item.get("name") or voice_id),
                gender=(item.get("gender") or None),
                language=(item.get("language_hint") or None),
                favorite=bool(item.get("favorite", False)),
            )
        )
    return out


# --- Selector option lists ---------------------------------------------------
@dataclass
class VoiceOption:
    value: str
    label: str


def build_voice_options(
    kokoro_voices: tuple[dict[str, str], ...],
    gallery_voices: list[GalleryVoice] | None = None,
) -> list[VoiceOption]:
    """Build the ``(value, label)`` list for the options-flow voice pickers:
    every Kokoro stock voice, then the user's cloned gallery voices."""
    options: list[VoiceOption] = [
        VoiceOption(
            value=format_voice_value(VOICE_KIND_KOKORO, v["id"]),
            label=v["label"],
        )
        for v in kokoro_voices
    ]
    for gv in gallery_voices or []:
        suffix = f" ({gv.language})" if gv.language else ""
        options.append(
            VoiceOption(
                value=format_voice_value(VOICE_KIND_GALLERY, gv.id),
                label=f"Clone: {gv.name}{suffix}",
            )
        )
    return options

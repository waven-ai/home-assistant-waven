"""Waven text-to-speech entity for Assist.

Implements the hybrid voice-routing policy (spec §5): the ``category`` option
selects the configured per-category voice (acks/confirmations use a fast Kokoro
stock voice; long announcements use the user's cloned gallery voice). An
explicit ``voice`` option always overrides. The daily cap gates the request.
"""

from __future__ import annotations

import logging

from homeassistant.components import tts
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import (
    WavenAuthError,
    WavenConnectionError,
    WavenConsentError,
    WavenError,
    WavenQuotaError,
)
from .const import (
    CATEGORIES,
    DEFAULT_CATEGORY,
    DEFAULT_TTS_LANGUAGE,
    KOKORO_VOICES,
    OPTION_CATEGORY,
    OPTION_VOICE,
    TTS_SUPPORTED_LANGUAGES,
    VOICE_KIND_GALLERY,
    VOICE_KIND_KOKORO,
)
from .coordinator import WavenCoordinator
from .entity import waven_device_info
from .quota import KIND_TTS
from .routing import format_voice_value, resolve_tts_request

_LOGGER = logging.getLogger(__name__)

# Rough speaking rate (chars/sec) used ONLY to pre-flight the daily-cap gate;
# the real billed duration comes back from the backend and is what we record.
# 15 was a guess that ran ~40% hot: Kokoro/OmniVoice at speed 1.0 land nearer
# 21 chars/sec for ordinary Assist responses, so every announcement reserved
# half again more budget than it spent and the cap bit early. It is deliberately
# still an over-estimate rather than an under-estimate — the gate exists to
# decline before spending, so erring long is the safe direction.
_CHARS_PER_SECOND = 21.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([WavenTTSEntity(entry.runtime_data, entry)])


class WavenTTSEntity(tts.TextToSpeechEntity):
    """Cloud TTS backed by Waven Kokoro (stock) + OmniVoice (clone)."""

    _attr_has_entity_name = True
    _attr_name = "Text-to-speech"

    def __init__(self, coordinator: WavenCoordinator, entry: ConfigEntry) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}-tts"
        self._attr_device_info = waven_device_info(entry)

    @property
    def default_language(self) -> str:
        return DEFAULT_TTS_LANGUAGE

    @property
    def supported_languages(self) -> list[str]:
        return TTS_SUPPORTED_LANGUAGES

    @property
    def supported_options(self) -> list[str]:
        # ATTR_VOICE is HA's standard per-call voice override; `category` is our
        # hybrid-routing selector.
        return [tts.ATTR_VOICE, OPTION_CATEGORY]

    @property
    def default_options(self) -> dict[str, str]:
        return {OPTION_CATEGORY: DEFAULT_CATEGORY}

    @callback
    def async_get_supported_voices(self, language: str) -> list[tts.Voice] | None:
        """Stock Kokoro voices + the user's cloned gallery voices, filtered to
        the requested language. ``voice_id`` is the encoded ``"<kind>:<id>"``
        value so it round-trips back through ATTR_VOICE into
        :func:`resolve_tts_request`. A clone with no declared language is always
        offered (its model is multilingual)."""
        lang = (language or "").split("-")[0].lower()
        voices: list[tts.Voice] = [
            tts.Voice(
                voice_id=format_voice_value(VOICE_KIND_KOKORO, v["id"]),
                name=v["label"],
            )
            for v in KOKORO_VOICES
            if not lang or v["language"] == lang
        ]
        for gv in self.coordinator.voices:
            if lang and gv.language and gv.language.lower() != lang:
                continue
            suffix = f" ({gv.language})" if gv.language else ""
            voices.append(
                tts.Voice(
                    voice_id=format_voice_value(VOICE_KIND_GALLERY, gv.id),
                    name=f"Clone: {gv.name}{suffix}",
                )
            )
        return voices or None

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict | None = None
    ) -> tts.TtsAudioType:
        options = options or {}
        # HA delivers a per-call voice override under ATTR_VOICE; our resolver
        # reads it as `voice`. `category` selects the hybrid slot.
        selection = resolve_tts_request(
            {
                OPTION_CATEGORY: options.get(OPTION_CATEGORY, DEFAULT_CATEGORY),
                OPTION_VOICE: options.get(tts.ATTR_VOICE) or options.get(OPTION_VOICE),
            },
            self.coordinator.category_voices(),
        )

        estimate = max(1.0, len(message) / _CHARS_PER_SECOND)
        if not self.coordinator.cap_allows(estimate):
            _LOGGER.info("Waven TTS declined: daily voice cap reached")
            return (None, None)

        try:
            result = await self.coordinator.client.async_generate_tts(
                message,
                selection,
                fmt=self.coordinator.tts_format,
                language=(language or DEFAULT_TTS_LANGUAGE).split("-")[0],
                retain_audio=self.coordinator.retain_audio,
            )
        except WavenAuthError as err:
            # A dead/revoked key. Don't wait for the next coordinator poll to
            # notice — start reauth so the user gets a repair card instead of
            # silently mute responses.
            _LOGGER.error("Waven TTS rejected the API key: %s", err)
            self.coordinator.note_auth_failure()
            return (None, None)
        except WavenQuotaError:
            _LOGGER.warning("Waven TTS declined upstream: usage limit reached")
            return (None, None)
        except WavenConsentError as err:
            # Config flow can't pre-detect this (GET /user has no consent gate),
            # so tell the user how to fix it the first time TTS is used.
            _LOGGER.error("Waven TTS blocked: %s", err)
            self.coordinator.notify_consent_required()
            return (None, None)
        except WavenConnectionError as err:
            _LOGGER.error("Waven TTS unreachable: %s", err)
            self.coordinator.note_failure()
            return (None, None)
        except WavenError as err:
            # Catch-all for terminal 4xx, including the retain_audio=False 404:
            # a flagged clip is burned by its first complete download, so a
            # fetch that dies mid-transfer has no file left to retry against
            # (see api.async_generate_tts). Returning (None, None) is not a
            # fallback — HA core has no second TTS engine to try. It raises
            # "No TTS from waven", the pipeline run ends with `tts-failed`,
            # nothing is spoken for that utterance, and the rest of the
            # automation carries on. One lost response, no stale audio.
            _LOGGER.error("Waven TTS failed: %s", err)
            return (None, None)

        # Encode the audit voice with its *kind* (kokoro/gallery), not the model
        # name, so the logged value round-trips through parse_voice_value.
        kind = VOICE_KIND_GALLERY if selection.is_clone else VOICE_KIND_KOKORO
        voice_label = selection.gallery_voice_id or selection.speaker or ""
        self.coordinator.record_usage(
            KIND_TTS,
            result.duration_seconds,
            model=selection.model,
            voice=format_voice_value(kind, voice_label),
            chars=len(message),
        )
        return (result.extension, result.audio)


# Re-exported so the options flow / tests can introspect the category list.
__all__ = ["WavenTTSEntity", "async_setup_entry", "CATEGORIES"]

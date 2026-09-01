"""Waven streaming speech-to-text entity for Assist.

HA's pipeline hands us raw 16-bit PCM mono 16 kHz; we convert it to float32 and
stream it to Waven's STT WebSocket, returning the final transcript. The daily
cap gates the request: if the household is already over cap we decline and the
pipeline falls through to whatever local STT the user had (spec §7).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable

from homeassistant.components import stt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import WavenAuthError, WavenConnectionError, WavenError, WavenQuotaError
from .const import STT_SUPPORTED_LANGUAGES
from .coordinator import WavenCoordinator
from .entity import waven_device_info
from .quota import KIND_STT

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([WavenSTTEntity(entry.runtime_data, entry)])


class WavenSTTEntity(stt.SpeechToTextEntity):
    """Cloud STT backed by Waven's streaming endpoint."""

    _attr_has_entity_name = True
    _attr_name = "Speech-to-text"

    def __init__(self, coordinator: WavenCoordinator, entry: ConfigEntry) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}-stt"
        self._attr_device_info = waven_device_info(entry)

    @property
    def supported_languages(self) -> list[str]:
        return STT_SUPPORTED_LANGUAGES

    @property
    def supported_formats(self) -> list[stt.AudioFormats]:
        return [stt.AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        return [stt.AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        return [stt.AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[stt.AudioChannels]:
        return [stt.AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> stt.SpeechResult:
        # We can't know the utterance length up front; the only honest pre-flight
        # gate is "are we already over the cap?". A single over-run utterance is
        # fine — it just trips the 100% notification and blocks the *next* one.
        if not self.coordinator.cap_allows(0):
            _LOGGER.info("Waven STT declined: daily voice cap reached")
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)

        language = (metadata.language or self.coordinator.stt_language).split("-")[0]
        try:
            result = await self.coordinator.client.async_stream_stt(
                stream,
                mode=self.coordinator.stt_mode,
                language=language,
                retain_audio=self.coordinator.retain_audio,
            )
        except WavenAuthError as err:
            # A dead/revoked key. The coordinator poll would eventually raise
            # ConfigEntryAuthFailed, but that is up to one poll interval away —
            # every utterance until then fails silently with nothing in the UI
            # saying why. Start reauth now so the repair card appears.
            _LOGGER.error("Waven STT rejected the API key: %s", err)
            self.coordinator.note_auth_failure()
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        except WavenQuotaError:
            _LOGGER.warning("Waven STT declined upstream: usage limit reached")
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        except WavenConnectionError as err:
            # Only a genuine connection failure flips the reachability flag.
            _LOGGER.error("Waven STT unreachable: %s", err)
            self.coordinator.note_failure()
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        except WavenError as err:
            # Content/protocol error (e.g. backend error frame) — not a network
            # outage, so leave reachability alone.
            _LOGGER.error("Waven STT failed: %s", err)
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)

        self.coordinator.record_usage(
            KIND_STT,
            result.audio_seconds,
            model=self.coordinator.stt_mode,
            chars=len(result.text),
            ok=bool(result.text),
        )
        return stt.SpeechResult(result.text, stt.SpeechResultState.SUCCESS)

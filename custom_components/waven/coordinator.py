"""Coordinator: polls account/quota, owns the daily-cap tracker, tracks cloud
reachability, and fires the 80%/100% cap notifications.

The STT and TTS entities do their real work live (they don't block on the
coordinator), but they consult it for the daily-cap gate and report usage back
to it. Sensors read ``coordinator.data`` (the parsed account) plus the tracker.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    WavenAuthError,
    WavenClient,
    WavenConnectionError,
    WavenConsentError,
    WavenError,
    WavenQuotaError,
)
from .const import (
    CONF_API_KEY,
    CONF_AUDIT_ENABLED,
    CONF_DAILY_CAP_MINUTES,
    CONF_REGION,
    CONF_RETAIN_AUDIO,
    CONF_STT_LANGUAGE,
    CONF_STT_MODE,
    CONF_TTS_FORMAT,
    CONF_VOICE_BY_CATEGORY,
    DEFAULT_DAILY_CAP_MINUTES,
    DEFAULT_REGION,
    DEFAULT_STT_LANGUAGE,
    DEFAULT_STT_MODE,
    DEFAULT_TTS_FORMAT,
    DOMAIN,
    NOTIFY_CAP_80,
    NOTIFY_CAP_100,
    NOTIFY_CONSENT,
    REACHABLE_STALE_SECONDS,
    REGION_HOSTS,
    UPDATE_INTERVAL_SECONDS,
)
from .quota import DailyCapTracker
from .routing import Account, GalleryVoice

_LOGGER = logging.getLogger(__name__)
_STORE_VERSION = 1


class WavenCoordinator(DataUpdateCoordinator[Account]):
    """Single source of truth for account state + the household daily cap."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: WavenClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} ({entry.title})",
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.entry = entry
        self.client = client
        self.tracker = DailyCapTracker()
        self.voices: list[GalleryVoice] = []
        self.reachable: bool = False
        self.last_contact: float | None = None
        # Set when the poll reached the backend but it answered with a
        # recoverable account-level condition (over quota, terms not accepted).
        # The entry stays LOADED; this is how the UI says why the numbers are
        # stale. See _async_update_data.
        self.degraded: bool = False
        self.degraded_reason: str | None = None
        self._store: Store = Store(hass, _STORE_VERSION, f"{DOMAIN}.{entry.entry_id}.quota")

    # --- config accessors (entry.options wins over entry.data) ---------------
    def _opt(self, key: str, default=None):
        if key in self.entry.options:
            return self.entry.options[key]
        return self.entry.data.get(key, default)

    @property
    def host(self) -> str:
        region = self._opt(CONF_REGION, DEFAULT_REGION)
        return REGION_HOSTS.get(region, REGION_HOSTS[DEFAULT_REGION])

    @property
    def daily_cap_minutes(self) -> int:
        return int(self._opt(CONF_DAILY_CAP_MINUTES, DEFAULT_DAILY_CAP_MINUTES))

    @property
    def retain_audio(self) -> bool:
        return bool(self._opt(CONF_RETAIN_AUDIO, True))

    @property
    def audit_enabled(self) -> bool:
        return bool(self._opt(CONF_AUDIT_ENABLED, True))

    @property
    def tts_format(self) -> str:
        return str(self._opt(CONF_TTS_FORMAT, DEFAULT_TTS_FORMAT))

    @property
    def stt_mode(self) -> str:
        return str(self._opt(CONF_STT_MODE, DEFAULT_STT_MODE))

    @property
    def stt_language(self) -> str:
        return str(self._opt(CONF_STT_LANGUAGE, DEFAULT_STT_LANGUAGE))

    def category_voices(self) -> dict[str, str]:
        """The user's per-category voice assignments (spec §5)."""
        out: dict[str, str] = {}
        for category, conf_key in CONF_VOICE_BY_CATEGORY.items():
            value = self._opt(conf_key)
            if value:
                out[category] = value
        return out

    # --- lifecycle -----------------------------------------------------------
    async def async_load_store(self) -> None:
        self.tracker = DailyCapTracker.from_dict(await self._store.async_load())
        self.tracker.ensure_day(self._local_today())

    async def _async_update_data(self) -> Account:
        try:
            account = await self.client.async_validate(retain_audio=self.retain_audio)
        except WavenAuthError as err:
            # Trigger HA's reauth flow rather than spinning on a dead key.
            raise ConfigEntryAuthFailed(str(err)) from err
        except WavenQuotaError as err:
            # 402/429 — the backend answered, the account is just out of
            # minutes. Recoverable, and nothing the entry does needs this poll:
            # STT/TTS gate themselves live and every sensor is either local or
            # tolerates stale data. Note that UpdateFailed is NOT enough here:
            # on the FIRST refresh HA re-raises it as ConfigEntryNotReady
            # (DataUpdateCoordinator.async_config_entry_first_refresh), which
            # would still tear the whole entry down at setup — the exact bug
            # this maps away. So: don't raise, degrade.
            return self._degraded("quota", err)
        except WavenConsentError as err:
            # 428 — terms not accepted. Same reasoning as quota; TTS surfaces
            # its own notification when a live request hits it.
            return self._degraded("consent", err)
        except WavenConnectionError as err:
            self.reachable = False
            raise UpdateFailed(str(err)) from err
        except WavenError as err:
            # Terminal catch-all for anything unclassified. The entry survives
            # (UpdateFailed after a successful first refresh only marks the data
            # stale), but an unknown failure on the very first poll is still a
            # legitimate ConfigEntryNotReady — we don't know it's recoverable.
            raise UpdateFailed(str(err)) from err
        self.degraded = False
        self.degraded_reason = None
        self._mark_contact()
        # Refresh the cloned-voice list for the TTS picker (best-effort; never
        # raises). Skipped on the very first refresh only if it would slow setup
        # — but it's cheap, so we keep it inline.
        self.voices = await self.client.async_list_voices(retain_audio=self.retain_audio)
        # A poll is a good moment to roll the local day over.
        self.tracker.ensure_day(self._local_today())
        return account

    def _degraded(self, reason: str, err: Exception) -> Account:
        """Keep the entry alive on a recoverable account-level condition.

        Returns the last-known account so the sensors keep their previous
        values (a placeholder on the very first poll, when there is none), and
        flags the coordinator so the UI can say why. The backend *answered*, so
        this still counts as cloud contact — reachability is about the network,
        not about the account's standing.
        """
        self.degraded = True
        self.degraded_reason = reason
        _LOGGER.warning(
            "Waven account is %s (%s); keeping the entry loaded with stale data",
            reason,
            err,
        )
        self._mark_contact()
        self.tracker.ensure_day(self._local_today())
        return self.data if self.data is not None else Account()

    # --- reachability --------------------------------------------------------
    @callback
    def _mark_contact(self) -> None:
        self.reachable = True
        self.last_contact = dt_util.utcnow().timestamp()

    @callback
    def note_failure(self) -> None:
        """Called by the entities when a live request can't reach the cloud."""
        self.reachable = False

    @callback
    def note_auth_failure(self) -> None:
        """Called by the entities when a LIVE request is rejected as 401/403.

        The poll raises ConfigEntryAuthFailed and starts reauth on its own, but
        that is up to UPDATE_INTERVAL_SECONDS away — until then every utterance
        fails with nothing in the UI saying the key is dead. Start the same
        reauth flow immediately; HA de-duplicates concurrent reauth flows for
        one entry, so a burst of failing requests still shows one repair card.
        """
        self.reachable = False
        self.entry.async_start_reauth(self.hass)

    @property
    def is_reachable(self) -> bool:
        """Fresh-enough successful contact within the staleness window."""
        if not self.reachable or self.last_contact is None:
            return False
        return (dt_util.utcnow().timestamp() - self.last_contact) <= REACHABLE_STALE_SECONDS

    # --- daily cap -----------------------------------------------------------
    def _local_today(self) -> str:
        return dt_util.now().date().isoformat()

    def cap_allows(self, estimated_seconds: float) -> bool:
        """Pre-flight gate. False → the entity declines. For TTS that means no
        audio, so HA raises "No TTS from waven" and the pipeline run ends
        `tts-failed` with nothing spoken (HA core has no second TTS engine to
        fall back to); for STT it means an empty transcript. Everything that
        isn't speech keeps running (spec §7). ``daily_cap_minutes <= 0``
        disables the cap."""
        self.tracker.ensure_day(self._local_today())
        return not self.tracker.would_exceed(self.daily_cap_minutes, estimated_seconds)

    @callback
    def record_usage(
        self,
        kind: str,
        seconds: float,
        *,
        model: str | None = None,
        voice: str | None = None,
        chars: int | None = None,
        ok: bool = True,
        detail: str | None = None,
    ) -> None:
        """Record a completed request, persist, and fire cap notifications."""
        today = self._local_today()
        self.tracker.record(
            kind,
            seconds,
            today_iso=today,
            ts=dt_util.utcnow().isoformat(),
            model=model,
            voice=voice if self.audit_enabled else None,
            chars=chars,
            ok=ok,
            detail=detail if self.audit_enabled else None,
        )
        self._fire_cap_notifications()
        self._store.async_delay_save(self.tracker.to_dict, 5)
        # Refresh sensor state without a network round-trip.
        self.async_update_listeners()

    @callback
    def _fire_cap_notifications(self) -> None:
        cap = self.daily_cap_minutes
        for threshold in self.tracker.newly_crossed(cap):
            from homeassistant.components import persistent_notification

            if threshold >= 100:
                persistent_notification.async_create(
                    self.hass,
                    (
                        "Waven has reached today's voice cap "
                        f"({cap} min). Cloud voice is paused until local "
                        "midnight; your automations keep running."
                    ),
                    title="Waven daily voice cap reached",
                    notification_id=NOTIFY_CAP_100,
                )
            else:
                used = round(self.tracker.used_minutes, 1)
                persistent_notification.async_create(
                    self.hass,
                    (
                        f"Waven voice usage is at {threshold}% of today's "
                        f"{cap} min cap ({used} min used)."
                    ),
                    title="Waven daily voice cap warning",
                    notification_id=NOTIFY_CAP_80,
                )

    def api_key(self) -> str:
        return str(self._opt(CONF_API_KEY, ""))

    @callback
    def notify_consent_required(self) -> None:
        """Surface the one HTTP-428 case the config flow can't pre-detect:
        the account validated fine but hasn't accepted the current ToS/Privacy,
        so `/generate` (TTS) and batch STT return 428. The notification id is
        fixed so repeated fires replace rather than stack."""
        from homeassistant.components import persistent_notification

        persistent_notification.async_create(
            self.hass,
            (
                "Waven text-to-speech is blocked because this account hasn't "
                "accepted the current Terms of Service and Privacy Policy. "
                "Accept them in the Waven dashboard, then try again. "
                "(Speech-to-text is unaffected.)"
            ),
            title="Waven: accept the terms to enable voice responses",
            notification_id=NOTIFY_CONSENT,
        )

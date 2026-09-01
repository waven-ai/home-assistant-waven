"""Diagnostic sensors that render the spec's HA dashboard card: today's usage,
cap status, monthly pool, last cloud contact, and the recent-requests audit."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import AUDIT_LOG_MAX_ROWS
from .coordinator import WavenCoordinator
from .entity import WavenCoordinatorEntity, WavenLocalEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            DailyUsageSensor(coordinator, entry),
            DailyRemainingSensor(coordinator, entry),
            MonthlyTTSRemainingSensor(coordinator, entry),
            MonthlySTTRemainingSensor(coordinator, entry),
            LastContactSensor(coordinator, entry),
            RecentRequestsSensor(coordinator, entry),
        ]
    )


class _MinutesSensor(WavenCoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "min"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1


class _LocalMinutesSensor(WavenLocalEntity, SensorEntity):
    """A minutes sensor computed from the local tracker — stays available
    through a backend outage (see WavenLocalEntity)."""

    _attr_native_unit_of_measurement = "min"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1


class DailyUsageSensor(_LocalMinutesSensor):
    _attr_name = "Voice used today"

    def __init__(self, coordinator: WavenCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "daily_usage")

    @property
    def native_value(self) -> float:
        return round(self.coordinator.tracker.used_minutes, 2)

    @property
    def extra_state_attributes(self) -> dict:
        cap = self.coordinator.daily_cap_minutes
        t = self.coordinator.tracker
        return {
            "daily_cap_minutes": cap,
            "remaining_minutes": round(t.remaining_minutes(cap), 2),
            "percent_used": round(t.percent_used(cap), 1),
            "stt_minutes": round(t.stt_seconds / 60.0, 2),
            "tts_minutes": round(t.tts_seconds / 60.0, 2),
        }


class DailyRemainingSensor(_LocalMinutesSensor):
    _attr_name = "Voice remaining today"

    def __init__(self, coordinator: WavenCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "daily_remaining")

    @property
    def native_value(self) -> float:
        cap = self.coordinator.daily_cap_minutes
        return round(self.coordinator.tracker.remaining_minutes(cap), 2)


class _MonthlyPoolSensor(_MinutesSensor):
    """A cloud-derived pool sensor.

    The coordinator keeps the entry loaded (and the last-known figures) when the
    backend answers with a recoverable account condition — over quota (402/429)
    or terms not accepted (428). These attributes are the only place the UI can
    say the number in front of you is the last one we got, and why.
    """

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "degraded": self.coordinator.degraded,
            "degraded_reason": self.coordinator.degraded_reason,
        }


class MonthlyTTSRemainingSensor(_MonthlyPoolSensor):
    _attr_name = "TTS minutes remaining this month"

    def __init__(self, coordinator: WavenCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "tts_remaining")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.tts_remaining


class MonthlySTTRemainingSensor(_MonthlyPoolSensor):
    _attr_name = "STT minutes remaining this month"

    def __init__(self, coordinator: WavenCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "stt_remaining")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.stt_remaining


class LastContactSensor(WavenCoordinatorEntity, SensorEntity):
    _attr_name = "Last cloud contact"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: WavenCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_contact")

    @property
    def native_value(self) -> datetime | None:
        if self.coordinator.last_contact is None:
            return None
        return dt_util.utc_from_timestamp(self.coordinator.last_contact)


class RecentRequestsSensor(WavenLocalEntity, SensorEntity):
    """State = today's request count; attributes carry the audit tail so the
    'what did Waven hear?' card can render without a backend round-trip.

    The state reads the tracker's per-day counter, NOT ``len(audit)``: the audit
    list is a cross-day ring buffer capped at ``AUDIT_LOG_MAX_ROWS``, so its
    length neither resets at local midnight nor keeps counting past the cap. The
    full ring size is still exposed as the ``audit_rows`` attribute.

    ``MEASUREMENT`` is the right state class here — the state is the current
    value of a quantity that resets to 0 at local midnight, not a monotonic
    lifetime total, so HA must not accumulate long-term sum statistics from it.
    """

    _attr_name = "Recent voice requests"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: WavenCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "recent_requests")

    @property
    def native_value(self) -> int:
        return self.coordinator.tracker.requests_today

    @property
    def extra_state_attributes(self) -> dict:
        # Most-recent-first, bounded so the attribute payload stays small.
        tracker = self.coordinator.tracker
        tail = list(reversed(tracker.audit))[:25]
        return {
            "audit_enabled": self.coordinator.audit_enabled,
            "max_rows": AUDIT_LOG_MAX_ROWS,
            # Total rows retained across days (the state is today's count only).
            "audit_rows": len(tracker.audit),
            "day": tracker.day,
            "requests": tail,
        }

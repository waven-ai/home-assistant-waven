"""Diagnostics dump for a Waven config entry (API key redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_API_KEY

TO_REDACT = {CONF_API_KEY, "email"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    account = coordinator.data
    tracker = coordinator.tracker
    cap = coordinator.daily_cap_minutes

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "account": async_redact_data(
            {
                "tier": getattr(account, "tier", None),
                "active": getattr(account, "active", None),
                "payg_enabled": getattr(account, "payg_enabled", None),
                "tts_remaining": getattr(account, "tts_remaining", None),
                "tts_limit": getattr(account, "tts_limit", None),
                "stt_remaining": getattr(account, "stt_remaining", None),
                "stt_limit": getattr(account, "stt_limit", None),
                "email": getattr(account, "email", None),
            },
            TO_REDACT,
        ),
        "reachability": {
            "is_reachable": coordinator.is_reachable,
            "last_contact": coordinator.last_contact,
        },
        "daily_cap": {
            "cap_minutes": cap,
            "used_minutes": round(tracker.used_minutes, 2),
            "remaining_minutes": round(tracker.remaining_minutes(cap), 2),
            "percent_used": round(tracker.percent_used(cap), 1),
            "over_cap": tracker.is_over_cap(cap),
            "day": tracker.day,
            "notified": tracker.notified,
            "requests_today": tracker.requests_today,
            "audit_rows": len(tracker.audit),
        },
        "config": {
            "host": coordinator.host,
            "stt_mode": coordinator.stt_mode,
            "stt_language": coordinator.stt_language,
            # The user's retention preference. When False the integration adds
            # the X-Waven-Retain-Audio: false header to every request, and the
            # backend honours it: a flagged synthesized clip is deleted as soon
            # as this integration has downloaded it in full, and one that is
            # never downloaded is dropped by the cleanup sweep — 10 min
            # (NO_RETAIN_AUDIO_TTL_MINUTES) + 3600 s
            # (AUDIO_CLEANUP_MIN_INTERVAL_SECONDS) + one 300 s
            # (CLEANUP_INTERVAL_SECONDS) tick ≈ 75 min worst case at the
            # defaults, with a wedged sweep alerted at 2 h. See
            # const.RETENTION_HEADER. The STT endpoints this integration uses
            # never store utterance audio server-side either way. Reported as
            # a pair so a diagnostics dump states both the preference and the
            # fact that the server enforces it.
            "retain_audio": coordinator.retain_audio,
            # Hardcoded True is a BUILD-TIME ASSERTION, not a runtime probe: it
            # says "the version of api.waven.ai (the default host) paired with
            # this release of the integration enforces the opt-out", which is
            # true from the prod deploy of the 2026-08 backend change onward —
            # hence the release ordering in README ▸ Publishing. It can lie for
            # a user who pointed `host` at their own or a stale deployment,
            # which may not enforce anything. Deriving it from a backend
            # capability flag (e.g. a field on GET /api/v1/user) is the
            # follow-up that makes it a fact instead of a claim.
            "retain_audio_opt_out_enforced_by_server": True,
            "audit_enabled": coordinator.audit_enabled,
            "cloned_voices": len(coordinator.voices),
            "category_voices": coordinator.category_voices(),
        },
    }

"""Client-side daily-cap accounting and request audit log.

The Waven backend has no per-household daily cap and no per-request audit
endpoint (the spec's ``GET /usage/ha-audit`` was never built). Both are
therefore implemented here, in the integration, which is the right home for them
anyway: HA already has persistent storage, a UI, and a notification surface, and
a household-level cap is a household-level control.

(The spec's other backend ask — the ``source=ha`` attribution column — *does*
now exist: migration ``041`` added it, and ``usage.source_breakdown.ha`` in
``GET /api/v1/user`` reports the Home Assistant slice of the shared pool. It is
display-only, so it neither feeds nor replaces the daily cap below.)

The hard daily cap (spec §7) is the non-negotiable safety rail: a stuck
microphone or a misconfigured continuous-capture automation must not be able to
drain an account. When the cap is hit, voice goes quiet but automations keep
running — the STT/TTS entities simply decline, so the pipeline run ends
without a spoken response (HA core has no second TTS engine to fall back to)
and everything that is not speech carries on.

This module is pure: the caller injects the current local date and timestamp
(HA passes ``homeassistant.util.dt.now()``), so the reset-at-local-midnight and
threshold-crossing logic is deterministically unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .const import AUDIT_LOG_MAX_ROWS, CAP_NOTIFY_PERCENTS

KIND_STT = "stt"
KIND_TTS = "tts"


@dataclass
class DailyCapTracker:
    """Mutable daily-usage state. Serialise via :meth:`to_dict` for HA Store."""

    day: str = ""  # ISO local date (YYYY-MM-DD) the counters below belong to
    tts_seconds: float = 0.0
    stt_seconds: float = 0.0
    # Requests recorded on ``day``. A dedicated counter rather than
    # ``len(audit)``: the audit list is a CROSS-DAY ring buffer capped at
    # AUDIT_LOG_MAX_ROWS, so its length is neither today-scoped nor an accurate
    # count once a busy day evicts older rows.
    requests_today: int = 0
    # Percent thresholds already notified *today* (so 80%/100% fire once each).
    notified: list[int] = field(default_factory=list)
    # Rolling, cross-day request log — the "what did Waven hear?" card.
    audit: list[dict] = field(default_factory=list)

    # --- day rollover --------------------------------------------------------
    def ensure_day(self, today_iso: str) -> bool:
        """Reset the daily counters if the local day changed. Returns True iff
        a reset happened. The audit log persists across days (capped); only the
        cap counters, the request count and the notify flags reset."""
        if self.day == today_iso:
            return False
        self.day = today_iso
        self.tts_seconds = 0.0
        self.stt_seconds = 0.0
        self.requests_today = 0
        self.notified = []
        return True

    # --- accounting ----------------------------------------------------------
    def record(
        self,
        kind: str,
        seconds: float,
        *,
        today_iso: str,
        ts: str,
        model: str | None = None,
        voice: str | None = None,
        chars: int | None = None,
        ok: bool = True,
        detail: str | None = None,
    ) -> None:
        """Add a completed request to the counters and the audit log."""
        self.ensure_day(today_iso)
        self.requests_today += 1
        seconds = max(0.0, float(seconds))
        if kind == KIND_TTS:
            self.tts_seconds += seconds
        elif kind == KIND_STT:
            self.stt_seconds += seconds

        row: dict = {
            "ts": ts,
            "kind": kind,
            "seconds": round(seconds, 2),
            "ok": ok,
        }
        if model is not None:
            row["model"] = model
        if voice is not None:
            row["voice"] = voice
        if chars is not None:
            row["chars"] = chars
        if detail is not None:
            row["detail"] = detail
        self.audit.append(row)
        # Bound the ring buffer.
        if len(self.audit) > AUDIT_LOG_MAX_ROWS:
            del self.audit[: len(self.audit) - AUDIT_LOG_MAX_ROWS]

    # --- derived views -------------------------------------------------------
    @property
    def used_seconds(self) -> float:
        return self.tts_seconds + self.stt_seconds

    @property
    def used_minutes(self) -> float:
        return self.used_seconds / 60.0

    def remaining_minutes(self, cap_minutes: float) -> float:
        return max(cap_minutes - self.used_minutes, 0.0)

    def percent_used(self, cap_minutes: float) -> float:
        if cap_minutes <= 0:
            return 0.0
        return min(self.used_minutes / cap_minutes * 100.0, 100.0)

    def is_over_cap(self, cap_minutes: float) -> bool:
        """True once the cap is reached. ``cap_minutes <= 0`` disables the cap."""
        if cap_minutes <= 0:
            return False
        return self.used_minutes >= cap_minutes

    def would_exceed(self, cap_minutes: float, seconds: float) -> bool:
        """Pre-flight check: would charging ``seconds`` reach or pass the cap?
        Used to decline a request *before* spending the minute upstream.
        ``cap_minutes <= 0`` disables the cap.

        ``>=`` here, matching :meth:`is_over_cap`. They used to disagree — this
        one was ``>`` — which made the two views of "at the cap" contradict
        each other at exactly the boundary: `binary_sensor.cap_reached` said
        on, while `cap_allows(0)` (the STT entity's only honest pre-flight,
        since an utterance's length isn't known up front) still said yes, so
        one more full-length utterance went through *after* the household had
        been told voice was paused. The cap is a hard safety rail; when the two
        readings disagree the rail has to win."""
        if cap_minutes <= 0:
            return False
        return (self.used_seconds + max(0.0, seconds)) / 60.0 >= cap_minutes

    def newly_crossed(self, cap_minutes: float) -> list[int]:
        """Return the notify thresholds (80, 100) crossed since the last call,
        marking them so each fires at most once per local day. The caller turns
        these into HA persistent notifications."""
        if cap_minutes <= 0:
            return []
        pct = self.percent_used(cap_minutes)
        crossed: list[int] = []
        for threshold in CAP_NOTIFY_PERCENTS:
            if pct >= threshold and threshold not in self.notified:
                self.notified.append(threshold)
                crossed.append(threshold)
        return crossed

    # --- persistence ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "tts_seconds": self.tts_seconds,
            "stt_seconds": self.stt_seconds,
            "requests_today": self.requests_today,
            "notified": list(self.notified),
            "audit": list(self.audit),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "DailyCapTracker":
        if not data:
            return cls()
        # Trim a possibly-oversized persisted blob (older build, hand edit) so
        # the count never transiently exceeds the bound before the next record().
        audit = list(data.get("audit") or [])[-AUDIT_LOG_MAX_ROWS:]
        return cls(
            day=str(data.get("day") or ""),
            tts_seconds=float(data.get("tts_seconds") or 0.0),
            stt_seconds=float(data.get("stt_seconds") or 0.0),
            # Missing key → 0. It cannot be recovered from the audit rows: the
            # coordinator stamps ``ts`` in UTC while ``day`` is the LOCAL date,
            # so a date-prefix match would miscount for every user not on UTC.
            # No released build ever wrote a blob without this key, so 0 only
            # ever applies to a hand-edited store.
            requests_today=int(data.get("requests_today") or 0),
            notified=[int(p) for p in (data.get("notified") or [])],
            audit=audit,
        )

"""From camera verdicts to "the roof may open".

Opening is slow and closing is fast.  The roof may open once the sky has shown
stars over most of its cells, on average, for a while: a few clouds passing
through the wait do not start it again, a cloudy look or a look without data
does.  Cloud, missing data, daylight or a gap in the monitor's own time take
the permission away again.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from .analysis import Verdict

_SEVERITY = {Verdict.CLEAR: 0, Verdict.PARTLY: 1, Verdict.CLOUDY: 2}
_CLOSED_REASON = {
    Verdict.PARTLY: "PARTLY_CLOUDY",
    Verdict.CLOUDY: "CLOUDY",
    Verdict.UNKNOWN: "NO_DATA",
}
_HOLDING_REASON = {
    Verdict.PARTLY: "PARTLY_HOLDING",
    Verdict.CLOUDY: "CLOUDY_HOLDING",
    Verdict.UNKNOWN: "NO_DATA_HOLDING",
}


@dataclass(frozen=True)
class DecisionParams:
    # The wait before the roof may open, over at least three looks.
    open_after_seconds: float = 600.0
    # Over the wait the sky must show stars in this share of its cells on average ...
    open_coverage: float = 0.70
    # ... and in at least this share at every look; a look below it starts the wait again.
    hold_coverage: float = 0.40
    # Consecutive cloudy looks that close an open roof.
    close_after_cycles: int = 2
    # An open roof closes once the sky has not been clear for this long; 0 never closes for it.
    partly_close_seconds: float = 1800.0
    # An open roof tolerates this long without any sign of stars (no data, or cloud).
    unknown_grace_seconds: float = 120.0
    # A longer silence between two looks (sleep, a stall) starts over from "closed".
    max_gap_seconds: float = 300.0
    # Cameras that must deliver a verdict.
    min_cameras: int = 1
    # The Sun must be this far down for the roof, and this far for imaging.
    sun_max_altitude_deg: float = -8.0
    imaging_sun_altitude_deg: float = -12.0
    # Imaging (never the roof) waits while a Moon lit beyond this fraction is above the horizon; 1 = no limit.
    imaging_moon_max_illumination: float = 1.0


@dataclass(frozen=True)
class Decision:
    when: datetime
    # The roof may be open.
    safe: bool
    # The roof may be open, the sky is clear now and it is dark enough to image.
    imaging_ok: bool
    verdict: Verdict
    # The worst camera's share of sky cells with stars; None without a verdict.
    coverage: float | None
    reason: str
    # When ``safe`` last changed.
    since: datetime
    # How long the sky has been good enough without interruption, and its mean coverage.
    held_seconds: float
    held_coverage: float | None


def fuse(verdicts: Sequence[Verdict], min_cameras: int = 1) -> Verdict:
    """The site verdict: the worst camera, once enough cameras deliver one."""

    known = [verdict for verdict in verdicts if verdict is not Verdict.UNKNOWN]
    if len(known) < max(1, min_cameras):
        return Verdict.UNKNOWN
    return max(known, key=_SEVERITY.__getitem__)


def fuse_coverage(verdicts: Sequence[Verdict], coverages: Sequence[float | None]) -> float | None:
    """The lowest coverage among the cameras that delivered a verdict."""

    known = [
        coverage
        for verdict, coverage in zip(verdicts, coverages, strict=True)
        if verdict is not Verdict.UNKNOWN and coverage is not None
    ]
    return min(known) if known else None


class DecisionMachine:
    def __init__(self, params: DecisionParams = DecisionParams()) -> None:
        self._params = params
        self._safe = False
        self._since: datetime | None = None
        self._last: datetime | None = None
        # The looks of the current wait: (when, coverage).
        self._wait: deque[tuple[datetime, float]] = deque()
        self._cloudy_cycles = 0
        # While open: since when the sky is not clear, and since when no stars are seen at all.
        self._not_clear_since: datetime | None = None
        self._blind_since: datetime | None = None

    def reset(self) -> None:
        """Start over from "closed", as after the operator took charge."""

        self._close()
        self._last = None

    def _close(self) -> None:
        self._safe = False
        self._wait.clear()
        self._cloudy_cycles = 0
        self._not_clear_since = None
        self._blind_since = None

    def _held(self, when: datetime) -> tuple[float, float | None]:
        if not self._wait:
            return 0.0, None
        span = (when - self._wait[0][0]).total_seconds()
        return span, sum(coverage for _, coverage in self._wait) / len(self._wait)

    def update(
        self,
        when: datetime,
        verdicts: Sequence[Verdict],
        coverages: Sequence[float | None],
        sun_altitude_deg: float | None,
        veto: str | None = None,
        moon_altitude_deg: float | None = None,
        moon_illumination: float | None = None,
    ) -> Decision:
        """One look; ``sun_altitude_deg`` is None when the site's position is not known.

        ``veto`` names a reason (such as rain in the forecast) that keeps a closed roof closed
        while the wait goes on, so that it opens as soon as the reason is gone.
        """

        params = self._params
        was_safe = self._safe
        if self._last is not None:
            gap = (when - self._last).total_seconds()
            if gap < 0 or gap > params.max_gap_seconds:
                self._close()
        self._last = when
        dark = sun_altitude_deg is None or sun_altitude_deg <= params.sun_max_altitude_deg
        verdict = fuse(verdicts, params.min_cameras) if dark else Verdict.UNKNOWN
        coverage = fuse_coverage(verdicts, coverages) if dark else None
        good = verdict in (Verdict.CLEAR, Verdict.PARTLY) and (coverage or 0.0) >= params.hold_coverage

        if not dark:
            self._close()
            reason = "DAYLIGHT"
        elif not self._safe:
            if good:
                assert coverage is not None
                self._wait.append((when, coverage))
                span, mean = self._held(when)
                ready = span >= params.open_after_seconds and len(self._wait) >= 3 and mean >= params.open_coverage
                if ready and veto is None:
                    self._safe = True
                reason = "CLEAR" if self._safe else (veto if ready and veto else "WAITING")
            else:
                self._close()
                reason = _CLOSED_REASON[verdict]
        elif verdict is Verdict.CLEAR:
            self._wait.append((when, coverage or 1.0))
            self._cloudy_cycles = 0
            self._not_clear_since = None
            self._blind_since = None
            reason = "CLEAR"
        else:
            self._wait.clear()
            self._not_clear_since = self._not_clear_since or when
            self._cloudy_cycles = self._cloudy_cycles + 1 if verdict is Verdict.CLOUDY else 0
            if verdict is Verdict.PARTLY:
                self._blind_since = None
            else:
                self._blind_since = self._blind_since or when
            not_clear = (when - self._not_clear_since).total_seconds()
            blind = (
                self._blind_since is not None
                and (when - self._blind_since).total_seconds() >= params.unknown_grace_seconds
            )
            if (
                self._cloudy_cycles >= params.close_after_cycles
                or blind
                or 0 < params.partly_close_seconds <= not_clear
            ):
                self._close()
                reason = _CLOSED_REASON[verdict]
            else:
                reason = _HOLDING_REASON[verdict]

        # The wait only reaches back as far as it needs to.
        while len(self._wait) > 1 and (when - self._wait[0][0]).total_seconds() > params.open_after_seconds:
            if (when - self._wait[1][0]).total_seconds() < params.open_after_seconds:
                break
            self._wait.popleft()

        if self._since is None or self._safe != was_safe:
            self._since = when
        sun_allows_imaging = sun_altitude_deg is None or sun_altitude_deg <= params.imaging_sun_altitude_deg
        moon_allows_imaging = (
            moon_altitude_deg is None
            or moon_illumination is None
            or moon_altitude_deg < 0
            or moon_illumination <= params.imaging_moon_max_illumination
        )
        held_seconds, held_coverage = self._held(when)
        return Decision(
            when=when,
            safe=self._safe,
            imaging_ok=self._safe and verdict is Verdict.CLEAR and sun_allows_imaging and moon_allows_imaging,
            verdict=verdict,
            coverage=coverage,
            reason=reason,
            since=self._since,
            held_seconds=held_seconds,
            held_coverage=held_coverage,
        )

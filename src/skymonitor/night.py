"""The operator's word for tonight ("the roof is open") and what was mailed about it.

A night runs from noon to noon, so that the mark set at 22:00 is still there at
02:00 and gone the next afternoon: noon by the site's solar time when its
longitude is known (the computer may keep another time zone), else by the
computer's clock.  The state lives in a small
JSON file that the running monitor, the control page and the ``roof`` command
all read and write.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class NightState:
    # The evening's date, YYYY-MM-DD, local time.
    night: str
    roof_opened: bool = False
    opened_at: str | None = None
    # When the last "clear sky" mail went out this night; None once a "lost" mail followed.
    notified_at: str | None = None
    # The operator has read the reminder: no more repeats until the sky closes and opens again.
    acknowledged: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "night": self.night,
            "roofOpened": self.roof_opened,
            "openedAt": self.opened_at,
            "notifiedAt": self.notified_at,
            "acknowledged": self.acknowledged,
        }


def night_of(now: datetime, longitude_deg: float | None = None) -> str:
    """The evening date a moment belongs to."""

    if longitude_deg is not None:
        solar = now.astimezone(UTC) + timedelta(hours=longitude_deg / 15.0)
        return (solar - timedelta(hours=12)).date().isoformat()
    return (now.astimezone() - timedelta(hours=12)).date().isoformat()


def _iso(moment: datetime) -> str:
    return moment.astimezone().isoformat(timespec="seconds")


class NightLog:
    """The night state in ``path``; picks up what another process wrote."""

    def __init__(self, path: Path, longitude_deg: float | None = None) -> None:
        self._path = Path(path)
        self._longitude = longitude_deg
        self._state: NightState | None = None
        self._stamp: int | None = None

    def _read(self) -> None:
        try:
            stamp = self._path.stat().st_mtime_ns
        except OSError:
            self._stamp = None
            return
        if stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            stored = json.loads(self._path.read_text(encoding="utf-8"))
            self._state = NightState(
                night=str(stored["night"]),
                roof_opened=bool(stored.get("roof_opened", False)),
                opened_at=stored.get("opened_at"),
                notified_at=stored.get("notified_at"),
                acknowledged=bool(stored.get("acknowledged", False)),
            )
        except (OSError, ValueError, KeyError, TypeError):
            self._state = None

    def _write(self, state: NightState) -> None:
        self._state = state
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(self._path.name + ".tmp")
        temporary.write_text(json.dumps(asdict(state), indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self._path)
        try:
            self._stamp = self._path.stat().st_mtime_ns
        except OSError:
            self._stamp = None

    def current(self, now: datetime) -> NightState:
        """Tonight's state; a new night starts clean."""

        self._read()
        night = night_of(now, self._longitude)
        if self._state is None or self._state.night != night:
            self._write(NightState(night=night))
        assert self._state is not None
        return self._state

    def set_roof_opened(self, now: datetime, opened: bool) -> NightState:
        state = self.current(now)
        if opened == state.roof_opened:
            return state
        # Opening ends the reminders; after a cancel the next clear sky is a new occasion.
        self._write(
            replace(
                state,
                roof_opened=opened,
                opened_at=_iso(now) if opened else None,
                notified_at=None,
                acknowledged=False,
            )
        )
        assert self._state is not None
        return self._state

    def set_notified(self, now: datetime, notified: bool) -> NightState:
        """A reminder went out (True), or the sky closed again (False: the next clear sky mails anew)."""

        state = self.current(now)
        if notified:
            self._write(replace(state, notified_at=_iso(now)))
        else:
            self._write(replace(state, notified_at=None, acknowledged=False))
        assert self._state is not None
        return self._state

    def set_acknowledged(self, now: datetime, acknowledged: bool) -> NightState:
        state = self.current(now)
        if acknowledged != state.acknowledged:
            self._write(replace(state, acknowledged=acknowledged))
        assert self._state is not None
        return self._state

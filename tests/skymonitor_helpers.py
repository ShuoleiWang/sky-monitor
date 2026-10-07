"""Synthetic cameras for the Sky Monitor tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from skymonitor.sources import FrameBurst, SourceError
from skymonitor.synthetic import SkyScene

# 22:40 local time at 100 E: the Sun is far below the horizon, the Moon has not risen.
NIGHT = datetime(2026, 10, 6, 14, 40, tzinfo=UTC)
# Stars cross the frame by this many pixels a minute.
DRIFT = (3.0, 1.0)


def sky_burst(
    scene: SkyScene,
    minute: float,
    *,
    when: datetime | None = None,
    cloud: np.ndarray | None = None,
    glow: float = 0.0,
    pair: bool = True,
) -> FrameBurst:
    """What a camera delivers ``minute`` minutes into the night: two stacks four seconds apart."""

    def frame(at: float, seed: int) -> np.ndarray:
        shift = (DRIFT[0] * at, DRIFT[1] * at)
        return scene.frame(shift=shift, cloud=cloud, cloud_glow=glow, frame_seed=seed, frames=8)

    seed = int(round(minute * 1000))
    images = (frame(minute, seed), frame(minute + 4 / 60, seed + 1)) if pair else (frame(minute, seed),)
    return FrameBurst(
        images=images,
        seconds_apart=4.0 if pair else 0.0,
        frames=16 if pair else 1,
        content_id=f"{seed}-{cloud is not None}-{glow}",
        captured_at=when or NIGHT + timedelta(minutes=minute),
    )


class Clock:
    def __init__(self, start: datetime = NIGHT) -> None:
        self.start = start
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    @property
    def minute(self) -> float:
        return (self.now - self.start).total_seconds() / 60.0


class SkySource:
    """A camera on a synthetic sky; a test changes ``cloud`` and ``failure`` between looks."""

    def __init__(self, scene: SkyScene, clock: Clock) -> None:
        self.scene = scene
        self.clock = clock
        self.cloud: np.ndarray | None = None
        self.failure: str | None = None
        self.looks = 0

    def grab(self) -> FrameBurst:
        self.looks += 1
        if self.failure:
            raise SourceError(self.failure)
        return sky_burst(self.scene, self.clock.minute, when=self.clock.now, cloud=self.cloud, glow=0.2)

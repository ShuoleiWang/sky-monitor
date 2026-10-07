"""Synthetic night-sky frames: stars on a noisy background, clouds, fixed bright pixels.

Used by the tests and for trying thresholds without a camera.  Every random
draw comes from a generator seeded by the scene or the frame, never from the
global state.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class SkyScene:
    shape: tuple[int, int] = (360, 640)
    stars: int = 400
    seed: int = 7
    background: float = 0.06
    # Standard deviation of the noise of one frame.
    noise: float = 0.012
    star_sigma: float = 1.1
    # Sensor defects and lamps: bright, compact and fixed in the frame.
    fixed_sources: int = 0

    def _catalogue(self) -> tuple[np.ndarray, np.ndarray]:
        """Star positions over an area wider than the frame, and their peak values."""

        generator = np.random.default_rng(self.seed)
        height, width = self.shape
        margin = 0.5
        # Same density inside the frame as ``stars`` asks for.
        count = int(round(self.stars * (1 + 2 * margin) ** 2))
        x = generator.uniform(-margin * width, (1 + margin) * width, count)
        y = generator.uniform(-margin * height, (1 + margin) * height, count)
        peak = np.minimum(0.05 * (1 - generator.uniform(0, 1, count)) ** -0.8, 0.9)
        return np.stack([x, y], axis=1), peak

    def _drifted(self, shift: tuple[float, float]) -> tuple[np.ndarray, np.ndarray]:
        """The catalogue after a drift of ``shift`` pixels; the sky wraps around, so it never runs out."""

        positions, peaks = self._catalogue()
        height, width = self.shape
        origin = np.array([-0.5 * width, -0.5 * height])
        span = np.array([2.0 * width, 2.0 * height])
        return (positions + np.asarray(shift, dtype=np.float64) - origin) % span + origin, peaks

    def _fixed(self) -> tuple[np.ndarray, np.ndarray]:
        generator = np.random.default_rng(self.seed + 100_003)
        height, width = self.shape
        x = generator.uniform(8, width - 8, self.fixed_sources)
        y = generator.uniform(8, height - 8, self.fixed_sources)
        return np.stack([x, y], axis=1), generator.uniform(0.15, 0.6, self.fixed_sources)

    def star_positions(self, shift: tuple[float, float] = (0.0, 0.0)) -> np.ndarray:
        """Positions (x, y) of the stars inside the frame after a drift of ``shift`` pixels."""

        positions, _ = self._drifted(shift)
        height, width = self.shape
        inside = (
            (positions[:, 0] >= 0) & (positions[:, 0] < width) & (positions[:, 1] >= 0) & (positions[:, 1] < height)
        )
        return positions[inside]

    def frame(
        self,
        *,
        shift: tuple[float, float] = (0.0, 0.0),
        cloud: np.ndarray | None = None,
        cloud_glow: float = 0.0,
        frame_seed: int = 0,
        frames: int = 1,
    ) -> np.ndarray:
        """The mean of ``frames`` exposures as float32 in 0..1.

        ``cloud`` is the opacity (0 clear, 1 opaque) at every pixel; opaque
        cloud hides the stars behind it and adds ``cloud_glow`` of diffuse light.
        """

        height, width = self.shape
        image = np.full(self.shape, self.background, dtype=np.float64)
        if cloud is not None:
            image += cloud_glow * cloud
        positions, peaks = self._drifted(shift)
        if cloud is not None:
            column = np.clip(np.round(positions[:, 0]).astype(int), 0, width - 1)
            row = np.clip(np.round(positions[:, 1]).astype(int), 0, height - 1)
            peaks = peaks * (1.0 - cloud[row, column])
        _stamp(image, positions, peaks, self.star_sigma)
        if self.fixed_sources:
            fixed_positions, fixed_peaks = self._fixed()
            _stamp(image, fixed_positions, fixed_peaks, 0.8)
        generator = np.random.default_rng([self.seed, frame_seed])
        image += generator.normal(0.0, self.noise / np.sqrt(frames), self.shape)
        return np.clip(image, 0.0, 1.0).astype(np.float32)


def _stamp(image: np.ndarray, positions: np.ndarray, peaks: np.ndarray, sigma: float) -> None:
    height, width = image.shape
    radius = int(np.ceil(4 * sigma))
    offsets = np.arange(-radius, radius + 1)
    for (x, y), peak in zip(positions, peaks, strict=True):
        if peak <= 0 or not (-radius <= x < width + radius and -radius <= y < height + radius):
            continue
        column, row = int(round(x)), int(round(y))
        xs = column + offsets
        ys = row + offsets
        keep_x = (xs >= 0) & (xs < width)
        keep_y = (ys >= 0) & (ys < height)
        if not keep_x.any() or not keep_y.any():
            continue
        profile_x = np.exp(-0.5 * ((xs[keep_x] - x) / sigma) ** 2)
        profile_y = np.exp(-0.5 * ((ys[keep_y] - y) / sigma) ** 2)
        image[np.ix_(ys[keep_y], xs[keep_x])] += peak * np.outer(profile_y, profile_x)


def cloud_bank(shape: tuple[int, int], covered: float, *, soft: float = 12.0) -> np.ndarray:
    """Opacity of a cloud bank covering the left ``covered`` fraction of the frame."""

    height, width = shape
    edge = covered * width
    x = np.arange(width, dtype=np.float64)
    profile = 1.0 / (1.0 + np.exp((x - edge) / max(soft, 1e-3)))
    if covered <= 0:
        profile[:] = 0.0
    elif covered >= 1:
        profile[:] = 1.0
    return np.broadcast_to(profile, (height, width)).copy()

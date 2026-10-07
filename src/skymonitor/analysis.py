"""Night-sky clarity of one camera.

A star is a compact source that is seen again where the sky has carried it and
that does not stay at one place in the frame.  Noise does not come back, and a
hot pixel, a lamp or an overlay does not move.  The sky is clear where the
cells of the frame hold such stars; cloud, fog, dew or rain on the camera all
take them away, so every failure reads as "not clear".
"""

from __future__ import annotations

import math
import os
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy.spatial import cKDTree

from .sources import FrameBurst
from .stars import DetectionParams, detect_stars

_FORGET_SECONDS = 14 * 86400.0


class Verdict(str, Enum):
    CLEAR = "CLEAR"
    PARTLY = "PARTLY"
    CLOUDY = "CLOUDY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SkyRegion:
    """The part of the frame that shows sky; every coordinate is a fraction of the frame."""

    # x0, y0, x1, y1
    rectangle: tuple[float, float, float, float] | None = None
    # centre x, centre y, radius as a fraction of the shorter side
    circle: tuple[float, float, float] | None = None
    exclude: tuple[tuple[float, float, float, float], ...] = ()
    # An image, white where the frame shows sky; resized to the frame.
    mask_path: Path | None = None

    def mask(self, shape: tuple[int, int]) -> np.ndarray | None:
        if not (self.rectangle or self.circle or self.exclude or self.mask_path):
            return None
        height, width = shape
        mask = np.ones(shape, dtype=bool)
        if self.mask_path is not None:
            with Image.open(self.mask_path) as picture:
                resized = picture.convert("L").resize((width, height), Image.Resampling.NEAREST)
                mask &= np.asarray(resized) >= 128
        y, x = np.ogrid[0:height, 0:width]

        def box(edges: tuple[float, float, float, float]) -> np.ndarray:
            x0, y0, x1, y1 = edges
            return (x >= x0 * width) & (x < x1 * width) & (y >= y0 * height) & (y < y1 * height)

        if self.rectangle is not None:
            mask &= box(self.rectangle)
        if self.circle is not None:
            centre_x, centre_y, radius = self.circle
            mask &= (x - centre_x * width) ** 2 + (y - centre_y * height) ** 2 <= (radius * min(shape)) ** 2
        for edges in self.exclude:
            mask &= ~box(edges)
        return mask


@dataclass(frozen=True)
class AnalysisParams:
    detection: DetectionParams = DetectionParams()
    # Share of the sky cells that must hold stars for a clear sky, and below which it is cloudy.
    clear_coverage: float = 0.70
    cloudy_coverage: float = 0.40
    # Stars a clear sky must show while no clear-sky reference is known for the camera.
    min_stars: int = 50
    # With a reference: the share of its star count a clear sky must show.
    min_star_ratio: float = 0.5
    # Stars the camera shows under a clear, moonless sky; 0 when not known.
    reference_star_count: int = 0
    # How far a bright Moon lowers that share: a full Moon high up leaves 1 - this of it.
    moon_allowance: float = 0.7
    # Sky cells; 0 sizes them to hold ``stars_per_cell`` stars each.
    grid_cells: int = 0
    max_grid_cells: int = 48
    stars_per_cell: float = 5.0
    # A cell holds stars when it has this share of the frame's mean density, and at least one.
    cell_density_ratio: float = 0.25
    # Position tolerance when a star is looked for again, and the drift of the sky on top of it.
    match_radius_px: float = 2.5
    drift_px_per_minute: float = 8.0
    # A place that shows a source in this share of the last minutes is fixed in the frame.
    fixed_window_minutes: float = 30.0
    fixed_mature_minutes: float = 15.0
    fixed_fraction: float = 0.5
    # Background level above which a cell is blinded (Moon glare, lights) ...
    glare_level: float = 0.85
    # ... and, while the Moon is up, this many times the frame's median background (its halo).
    glare_ratio: float = 3.0
    max_blinded_fraction: float = 0.5
    # A verdict needs the fixed sources learned first; off only for one-off analysis.
    require_mature: bool = True


@dataclass(frozen=True)
class Overlay:
    """What a preview draws; not part of the published status."""

    image: np.ndarray
    stars: np.ndarray
    fixed: np.ndarray
    unconfirmed: np.ndarray
    cell: int
    clear: np.ndarray
    usable: np.ndarray
    blinded: np.ndarray


@dataclass(frozen=True)
class Assessment:
    verdict: Verdict
    reason: str
    detail: str = ""
    # Share of the usable sky cells that hold stars.
    coverage: float | None = None
    # Stars: compact, seen again, and not fixed in the frame.
    star_count: int = 0
    required_stars: int = 0
    reference_star_count: int = 0
    detections: int = 0
    fixed_sources: int = 0
    cells_clear: int = 0
    cells_usable: int = 0
    cells_blinded: int = 0
    background: float | None = None
    frames: int = 0
    overlay: Overlay | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "detail": self.detail,
            "coverage": None if self.coverage is None else round(self.coverage, 3),
            "starCount": self.star_count,
            "requiredStars": self.required_stars,
            "referenceStarCount": self.reference_star_count or None,
            "detections": self.detections,
            "fixedSources": self.fixed_sources,
            "cells": {
                "clear": self.cells_clear,
                "usable": self.cells_usable,
                "blinded": self.cells_blinded,
            },
            "background": None if self.background is None else round(self.background, 4),
            "frames": self.frames,
        }


class FixedSourceTracker:
    """Places in the frame where a source keeps appearing: hot pixels, lamps, overlays.

    A star crosses a place and is gone; whatever is found at the same place in
    half of the recent cycles is part of the camera or the scene.
    """

    def __init__(
        self,
        shape: tuple[int, int],
        *,
        window_seconds: float = 1800.0,
        mature_seconds: float = 900.0,
        fraction: float = 0.5,
        interval_seconds: float = 60.0,
        quantum: int = 2,
    ) -> None:
        self.shape = (int(shape[0]), int(shape[1]))
        self._window = float(window_seconds)
        self._mature = float(mature_seconds)
        self._fraction = float(fraction)
        self._interval = float(interval_seconds)
        self._quantum = int(quantum)
        self._rows = -(-self.shape[0] // self._quantum)
        self._columns = -(-self.shape[1] // self._quantum)
        self._entries: deque[tuple[float, np.ndarray]] = deque()
        self._fixed = np.empty(0, dtype=np.int64)

    def _cells(self, xy: np.ndarray, *, spread: bool) -> np.ndarray:
        if len(xy) == 0:
            return np.empty(0, dtype=np.int64)
        column = (xy[:, 0] // self._quantum).astype(np.int64)
        row = (xy[:, 1] // self._quantum).astype(np.int64)
        if not spread:
            return row * self._columns + column
        offsets = np.array([-1, 0, 1], dtype=np.int64)
        column = np.clip(column[:, None, None] + offsets[None, None, :], 0, self._columns - 1)
        row = np.clip(row[:, None, None] + offsets[None, :, None], 0, self._rows - 1)
        return np.unique(row * self._columns + column)

    @property
    def mature(self) -> bool:
        entries = self._entries
        return len(entries) >= 5 and entries[-1][0] - entries[0][0] >= self._mature

    def update(self, when: float, xy: np.ndarray) -> None:
        """Record the compact sources of one cycle; ``when`` is in seconds."""

        if self._entries:
            gap = when - self._entries[-1][0]
            if gap < 0 or gap > _FORGET_SECONDS:
                self._entries.clear()
            elif gap > max(4 * self._interval, 300.0):
                # Daylight or a restart: what was learned still holds, so carry it forward.
                shift = gap - self._interval
                self._entries = deque((moment + shift, cells) for moment, cells in self._entries)
        self._entries.append((float(when), self._cells(xy, spread=True)))
        while when - self._entries[0][0] > self._window:
            self._entries.popleft()
        if len(self._entries) < 5:
            self._fixed = np.empty(0, dtype=np.int64)
            return
        cells, counts = np.unique(np.concatenate([cells for _, cells in self._entries]), return_counts=True)
        self._fixed = cells[counts >= self._fraction * len(self._entries)]

    def fixed(self, xy: np.ndarray) -> np.ndarray:
        """Which of the positions lie on a fixed source."""

        return np.isin(self._cells(xy, spread=False), self._fixed)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp.npz")
        np.savez_compressed(
            temporary,
            shape=np.array(self.shape, dtype=np.int64),
            quantum=np.array(self._quantum, dtype=np.int64),
            times=np.array([moment for moment, _ in self._entries], dtype=np.float64),
            lengths=np.array([len(cells) for _, cells in self._entries], dtype=np.int64),
            cells=np.concatenate([cells for _, cells in self._entries] or [self._fixed]),
        )
        os.replace(temporary, path)

    def restore(self, path: Path) -> bool:
        """Load what an earlier run learned for the same frame size; False when unusable."""

        try:
            with np.load(path, allow_pickle=False) as stored:
                if tuple(int(v) for v in stored["shape"]) != self.shape:
                    return False
                if int(stored["quantum"]) != self._quantum:
                    return False
                times, lengths, cells = stored["times"], stored["lengths"], stored["cells"]
        except (OSError, KeyError, ValueError):
            return False
        if len(times) != len(lengths) or int(lengths.sum()) != len(cells):
            return False
        bounds = np.concatenate([[0], np.cumsum(lengths)])
        self._entries = deque(
            (float(moment), cells[bounds[index] : bounds[index + 1]].astype(np.int64))
            for index, moment in enumerate(times)
        )
        return True


def _near(points: np.ndarray, reference: np.ndarray, radius: float) -> np.ndarray:
    """Which ``points`` have a ``reference`` point within ``radius`` pixels."""

    if len(points) == 0 or len(reference) == 0:
        return np.zeros(len(points), dtype=bool)
    distance, _ = cKDTree(reference).query(points, k=1, distance_upper_bound=radius)
    return np.isfinite(distance)


def _block_sum(values: np.ndarray, cell: int) -> np.ndarray:
    rows = np.add.reduceat(values, np.arange(0, values.shape[0], cell), axis=0)
    return np.add.reduceat(rows, np.arange(0, values.shape[1], cell), axis=1)


@dataclass(frozen=True)
class _Grid:
    coverage: float
    blinded_fraction: float
    clear: np.ndarray
    usable: np.ndarray
    blinded: np.ndarray


def _grid(mask: np.ndarray | None, glare: np.ndarray, stars: np.ndarray, cell: int, density_ratio: float) -> _Grid:
    sky = np.ones(glare.shape, dtype=np.float32) if mask is None else mask.astype(np.float32)
    area = _block_sum(sky, cell)
    valid = area >= 0.3 * cell * cell
    blinded = valid & (_block_sum(sky * glare, cell) >= 0.5 * area)
    usable = valid & ~blinded
    counts = np.zeros(area.shape, dtype=np.int64)
    if len(stars):
        rows = (stars[:, 1] // cell).astype(np.intp)
        columns = (stars[:, 0] // cell).astype(np.intp)
        np.add.at(counts, (rows, columns), 1)
    usable_area = float(area[usable].sum())
    valid_area = float(area[valid].sum())
    if usable_area <= 0:
        nothing = np.zeros(area.shape, dtype=bool)
        return _Grid(0.0, 1.0 if valid_area > 0 else 0.0, nothing, usable, blinded)
    expected = counts[usable].sum() * area / usable_area
    clear = usable & (counts >= np.maximum(1.0, np.floor(density_ratio * expected)))
    return _Grid(
        float(area[clear].sum()) / usable_area,
        float(area[blinded].sum()) / valid_area,
        clear,
        usable,
        blinded,
    )


class CameraAnalyzer:
    """Turns the bursts of one camera into assessments; keeps what it learned between cycles."""

    def __init__(
        self,
        params: AnalysisParams = AnalysisParams(),
        *,
        region: SkyRegion | None = None,
        state_path: Path | None = None,
        interval_seconds: float = 60.0,
    ) -> None:
        self._params = params
        self._region = region
        self._state_path = state_path
        self._interval = float(interval_seconds)
        self._shape: tuple[int, int] | None = None
        self._mask: np.ndarray | None = None
        self._tracker: FixedSourceTracker | None = None
        self._previous: tuple[float, np.ndarray] | None = None
        self._content_id: str | None = None
        self._assessment: Assessment | None = None
        self._updates = 0

    def _start(self, shape: tuple[int, int]) -> FixedSourceTracker:
        params = self._params
        self._shape = shape
        self._mask = self._region.mask(shape) if self._region is not None else None
        self._previous = None
        tracker = FixedSourceTracker(
            shape,
            window_seconds=params.fixed_window_minutes * 60.0,
            mature_seconds=params.fixed_mature_minutes * 60.0,
            fraction=params.fixed_fraction,
            interval_seconds=self._interval,
        )
        if self._state_path is not None:
            tracker.restore(self._state_path)
        self._tracker = tracker
        return tracker

    def save(self) -> None:
        """Keep the fixed sources for the next run, so that it need not learn them again."""

        if self._tracker is not None and self._state_path is not None:
            self._tracker.save(self._state_path)

    def assess(self, burst: FrameBurst, *, moon_strength: float = 0.0, reference_star_count: int = 0) -> Assessment:
        if burst.content_id == self._content_id and self._assessment is not None:
            # Nothing new from the camera; whether that is too old is the source's business.
            return self._assessment
        params = self._params
        image = burst.images[-1]
        tracker = self._tracker
        if tracker is None or image.shape != self._shape:
            tracker = self._start(image.shape)
        when = burst.captured_at.timestamp()

        latest = detect_stars(image, self._mask, params.detection)
        xy = latest.xy
        detections = len(xy)
        if len(burst.images) > 1:
            earlier = detect_stars(burst.images[0], self._mask, params.detection)
            radius = params.match_radius_px + params.drift_px_per_minute * burst.seconds_apart / 60.0
            xy = xy[_near(xy, earlier.xy, radius)]
        tracker.update(when, xy)
        fixed = tracker.fixed(xy)
        seen_before: np.ndarray | None = None
        if self._previous is not None:
            age = when - self._previous[0]
            if 0 < age <= max(4 * self._interval, 300.0):
                radius = params.match_radius_px + params.drift_px_per_minute * age / 60.0
                seen_before = _near(xy, self._previous[1], radius)
        self._previous = (when, xy)
        self._content_id = burst.content_id
        self._updates += 1
        if self._updates % 10 == 0:
            try:
                self.save()
            except OSError:
                pass  # learned again after a restart; not worth failing a cycle

        moving = ~fixed if seen_before is None else seen_before & ~fixed
        stars = xy[moving]
        count = len(stars)
        reference = params.reference_star_count or reference_star_count
        # Moonlight hides the faint stars: a bright Moon high up lowers what a clear sky must show.
        allowance = 1.0 - params.moon_allowance * moon_strength
        if reference > 0:
            required = max(5, math.ceil(params.min_star_ratio * allowance * reference - 1e-9))
        else:
            required = max(5, math.ceil(params.min_stars * allowance - 1e-9))
        basis = reference if reference > 0 else max(count, params.min_stars)
        cells = params.grid_cells or int(np.clip(round(basis / params.stars_per_cell), 6, params.max_grid_cells))
        sky_area = image.size if self._mask is None else int(self._mask.sum())
        cell = max(8, int(round(math.sqrt(max(sky_area, 1) / cells))))
        usable_level = latest.background[::8, ::8]
        if self._mask is not None:
            usable_level = usable_level[self._mask[::8, ::8]]
        median_level = float(np.median(usable_level)) if usable_level.size else 0.0
        glare = latest.background > params.glare_level
        if moon_strength > 0.05 and median_level > 0:
            # The Moon's halo: far brighter than the rest of the sky, whatever its absolute level.
            glare |= latest.background > params.glare_ratio * median_level
        grid = _grid(self._mask, glare, stars, cell, params.cell_density_ratio)

        if grid.blinded_fraction > params.max_blinded_fraction:
            verdict, reason = Verdict.UNKNOWN, "TOO_BRIGHT"
        elif seen_before is None:
            verdict, reason = Verdict.UNKNOWN, "CONFIRMING"
        elif params.require_mature and not tracker.mature:
            verdict, reason = Verdict.UNKNOWN, "WARMING_UP"
        elif grid.coverage >= params.clear_coverage and count >= required:
            verdict, reason = Verdict.CLEAR, "CLEAR"
        elif grid.coverage >= params.cloudy_coverage and count >= 0.4 * required:
            verdict, reason = Verdict.PARTLY, "PARTLY_CLOUDY"
        elif count < 0.4 * required:
            verdict, reason = Verdict.CLOUDY, "FEW_STARS"
        else:
            verdict, reason = Verdict.CLOUDY, "LOW_COVERAGE"

        self._assessment = Assessment(
            verdict=verdict,
            reason=reason,
            coverage=grid.coverage,
            star_count=count,
            required_stars=required,
            reference_star_count=reference,
            detections=detections,
            fixed_sources=int(fixed.sum()),
            cells_clear=int(grid.clear.sum()),
            cells_usable=int(grid.usable.sum()),
            cells_blinded=int(grid.blinded.sum()),
            background=median_level if usable_level.size else None,
            frames=burst.frames,
            overlay=Overlay(
                image=image,
                stars=stars,
                fixed=xy[fixed],
                unconfirmed=xy[~moving & ~fixed],
                cell=cell,
                clear=grid.clear,
                usable=grid.usable,
                blinded=grid.blinded,
            ),
        )
        return self._assessment


def render_preview(overlay: Overlay, caption: str = "") -> Image.Image:
    """The frame with its sky cells and sources marked: what the verdict was read from."""

    image = overlay.image
    low, high = np.percentile(image[::4, ::4], (1.0, 99.8))
    stretched = np.clip((image - low) / max(float(high - low), 1e-4), 0.0, 1.0) ** 0.6
    picture = Image.fromarray((stretched * 255).astype(np.uint8)).convert("RGB")
    draw = ImageDraw.Draw(picture)
    height, width = image.shape
    cell = overlay.cell
    for (row, column), usable in np.ndenumerate(overlay.usable):
        if overlay.blinded[row, column]:
            colour = (120, 120, 120)
        elif not usable:
            continue
        else:
            colour = (0, 150, 0) if overlay.clear[row, column] else (210, 40, 40)
        left, top = column * cell, row * cell
        draw.rectangle(
            [left + 1, top + 1, min(left + cell, width) - 2, min(top + cell, height) - 2],
            outline=colour,
        )
    for x, y in overlay.unconfirmed:
        draw.ellipse([x - 2, y - 2, x + 2, y + 2], outline=(200, 0, 200))
    for x, y in overlay.fixed:
        draw.rectangle([x - 3, y - 3, x + 3, y + 3], outline=(255, 210, 0))
    for x, y in overlay.stars:
        draw.ellipse([x - 4, y - 4, x + 4, y + 4], outline=(90, 255, 90))
    if caption:
        text = caption.encode("ascii", "replace").decode("ascii")
        box = draw.textbbox((4, 3), text)
        draw.rectangle([0, 0, box[2] + 4, box[3] + 3], fill=(0, 0, 0))
        draw.text((4, 3), text, fill=(255, 255, 255))
    return picture

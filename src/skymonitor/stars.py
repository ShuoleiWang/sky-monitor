"""Point-source detection on a stacked sky frame.

A matched filter over a mesh-subtracted image, thresholded against the local
noise of the filtered image, keeping only peaks that are compact: a source
whose response at three times the filter width is nearly as strong as at the
filter width is a cloud edge, a glow or a lamp, not a star.

Written for small computers: the mesh statistics use every other pixel, the
wide response is computed at half resolution, and the peak tests run only on
the pixels that pass the threshold.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
from scipy import ndimage


@dataclass(frozen=True)
class DetectionParams:
    """Detection settings; pixel values are on a 0..1 scale."""

    # Gaussian sigma of the matched filter, in pixels.
    psf_sigma: float = 1.2
    # Peak height over the local noise of the filtered image.
    threshold_sigma: float = 4.5
    # Lowest filtered peak accepted whatever the noise (1.5 levels of an 8-bit frame).
    min_peak: float = 0.006
    # Filtered peak over the same peak at three times the filter width; a star gives 3 to 7.
    point_ratio_min: float = 2.5
    # Side of the window in which a peak must be the maximum.
    separation: int = 5
    # Pixels kept clear of the frame and mask edges.
    edge_margin: int = 3
    # Side of the background mesh tiles; 0 chooses it from the frame size.
    tile: int = 0
    max_sources: int = 4000


@dataclass(frozen=True)
class Detections:
    # (N, 2) x, y pixel positions, brightest first.
    xy: np.ndarray
    snr: np.ndarray
    # Background level at every pixel, same shape as the frame.
    background: np.ndarray
    truncated: bool


def _tiles(image: np.ndarray, tile: int) -> np.ndarray:
    height, width = image.shape
    rows, columns = -(-height // tile), -(-width // tile)
    if (rows * tile, columns * tile) != (height, width):
        padded = np.full((rows * tile, columns * tile), np.nan, dtype=np.float32)
        padded[:height, :width] = image
        image = padded
    return image.reshape(rows, tile, columns, tile).swapaxes(1, 2).reshape(rows, columns, tile * tile)


def _mesh_median_and_mad(values: np.ndarray, tile: int) -> tuple[np.ndarray, np.ndarray]:
    """Per-tile median and median absolute deviation; NaN where too few pixels are valid."""

    blocks = _tiles(values, tile)
    count = np.isfinite(blocks).sum(axis=2)
    complete = count == blocks.shape[2]
    partial = ~complete & (count >= max(4, tile * tile // 4))
    median = np.full(count.shape, np.nan, dtype=np.float32)
    deviation = np.full(count.shape, np.nan, dtype=np.float32)
    # Tiles without a masked pixel (nearly all of them) take the fast path; nanmedian loops in Python.
    if complete.any():
        whole = blocks[complete]
        centre = np.median(whole, axis=1)
        median[complete] = centre
        deviation[complete] = np.median(np.abs(whole - centre[:, None]), axis=1)
    if partial.any():
        some = blocks[partial]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            centre = np.nanmedian(some, axis=1)
            median[partial] = centre
            deviation[partial] = np.nanmedian(np.abs(some - centre[:, None]), axis=1)
    return median, deviation


def _fill_nearest(mesh: np.ndarray) -> np.ndarray:
    missing = ~np.isfinite(mesh)
    if not missing.any():
        return mesh
    if missing.all():
        return np.zeros_like(mesh)
    nearest = ndimage.distance_transform_edt(missing, return_distances=False, return_indices=True)
    return mesh[tuple(nearest)]


def _smooth_mesh(mesh: np.ndarray) -> np.ndarray:
    return ndimage.median_filter(_fill_nearest(mesh), size=3, mode="nearest").astype(np.float32)


def _mesh_axis(length: int, cells: int, tile: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    position = (np.arange(length, dtype=np.float32) + 0.5) / tile - 0.5
    low = np.clip(np.floor(position).astype(np.intp), 0, max(cells - 2, 0))
    fraction = np.clip(position - low, 0.0, 1.0).astype(np.float32)
    return low, np.minimum(low + 1, cells - 1), fraction


def _upsample(mesh: np.ndarray, tile: int, shape: tuple[int, int]) -> np.ndarray:
    """Bilinear interpolation of a tile mesh, tile centres at their own value."""

    y0, y1, fy = _mesh_axis(shape[0], mesh.shape[0], tile)
    x0, x1, fx = _mesh_axis(shape[1], mesh.shape[1], tile)
    rows = mesh[y0] * (1 - fy)[:, None] + mesh[y1] * fy[:, None]
    return rows[:, x0] * (1 - fx) + rows[:, x1] * fx


def _sample(mesh: np.ndarray, tile: int, shape: tuple[int, int], ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
    """The same interpolation, at some pixels only."""

    y0, y1, fy = _mesh_axis(shape[0], mesh.shape[0], tile)
    x0, x1, fx = _mesh_axis(shape[1], mesh.shape[1], tile)
    y0, y1, fy, x0, x1, fx = y0[ys], y1[ys], fy[ys], x0[xs], x1[xs], fx[xs]
    top = mesh[y0, x0] * (1 - fx) + mesh[y0, x1] * fx
    bottom = mesh[y1, x0] * (1 - fx) + mesh[y1, x1] * fx
    return top * (1 - fy) + bottom * fy


def _halve(image: np.ndarray) -> np.ndarray:
    height, width = (side // 2 * 2 for side in image.shape)
    return image[:height, :width].reshape(height // 2, 2, width // 2, 2).mean(axis=(1, 3), dtype=np.float32)


def _empty(background: np.ndarray) -> Detections:
    return Detections(
        xy=np.empty((0, 2), dtype=np.float32),
        snr=np.empty(0, dtype=np.float32),
        background=background,
        truncated=False,
    )


def detect_stars(
    image: np.ndarray,
    mask: np.ndarray | None = None,
    params: DetectionParams = DetectionParams(),
) -> Detections:
    """Compact sources in ``image`` (2-D, 0..1); ``mask`` is True where the frame shows sky."""

    frame = np.asarray(image, dtype=np.float32)
    if frame.ndim != 2:
        raise ValueError("a 2-D luminance frame is required")
    valid = np.isfinite(frame)
    if mask is not None:
        valid &= mask
    shape = frame.shape
    # Mesh statistics come from every other pixel; the tile is kept even so both grids agree.
    sub = max(8, int(np.clip(round(min(shape) / 48), 8, 32)) if params.tile == 0 else max(4, params.tile // 2))
    tile = 2 * sub

    masked = np.where(valid, frame, np.nan)
    level, _ = _mesh_median_and_mad(masked[::2, ::2], sub)
    background = _upsample(_smooth_mesh(level), tile, shape)
    residual = np.where(valid, frame - background, np.float32(0.0)).astype(np.float32)
    narrow = ndimage.gaussian_filter(residual, params.psf_sigma, mode="nearest", truncate=3.0)
    # The wide response is smooth: half resolution is enough, and it is only read at candidates.
    wide = ndimage.gaussian_filter(_halve(residual), 1.5 * params.psf_sigma, mode="nearest", truncate=3.0)
    _, deviation = _mesh_median_and_mad(np.where(valid, narrow, np.nan)[::2, ::2], sub)
    noise_mesh = _smooth_mesh(1.4826 * deviation)

    radius = params.separation // 2
    margin = max(params.edge_margin, radius, 1)
    bright = narrow >= params.min_peak
    bright &= valid
    bright[:margin] = False
    bright[-margin:] = False
    bright[:, :margin] = False
    bright[:, -margin:] = False
    ys, xs = np.nonzero(bright)
    if ys.size == 0:
        return _empty(background)

    peak = narrow[ys, xs]
    noise = np.maximum(_sample(noise_mesh, tile, shape, ys, xs), np.float32(1e-6))
    keep = peak >= params.threshold_sigma * noise
    keep &= (
        peak
        >= params.point_ratio_min * wide[np.minimum(ys // 2, wide.shape[0] - 1), np.minimum(xs // 2, wide.shape[1] - 1)]
    )
    # The maximum of its window, and no masked pixel within the margin.
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dy or dx:
                keep &= peak > narrow[ys + dy, xs + dx]
    if mask is not None:
        for dy in range(-margin, margin + 1):
            for dx in range(-margin, margin + 1):
                keep &= valid[ys + dy, xs + dx]
    ys, xs, peak, noise = ys[keep], xs[keep], peak[keep], noise[keep]
    if ys.size == 0:
        return _empty(background)

    snr = peak / noise
    order = np.argsort(-snr, kind="stable")
    truncated = order.size > params.max_sources
    order = order[: params.max_sources]
    xy = np.stack([xs[order], ys[order]], axis=1).astype(np.float32)
    return Detections(xy=xy, snr=snr[order].astype(np.float32), background=background, truncated=truncated)

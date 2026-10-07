from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree

from skymonitor.stars import detect_stars
from skymonitor.synthetic import SkyScene, cloud_bank


def _distance_to_truth(scene: SkyScene, xy: np.ndarray) -> np.ndarray:
    distance, _ = cKDTree(scene.star_positions()).query(xy, k=1)
    return distance


def test_stars_are_found_and_nothing_else() -> None:
    scene = SkyScene()

    found = detect_stars(scene.frame(frames=8))

    assert len(found.xy) > 0.8 * len(scene.star_positions())
    assert (_distance_to_truth(scene, found.xy) < 2.0).all()
    assert (np.diff(found.snr) <= 0).all()


def test_noise_alone_gives_no_stars() -> None:
    assert len(detect_stars(SkyScene(stars=0).frame(frames=8)).xy) == 0
    assert len(detect_stars(SkyScene(stars=0).frame(frames=1)).xy) <= 5


def test_glowing_overcast_gives_no_stars() -> None:
    scene = SkyScene()
    overcast = scene.frame(frames=8, cloud=cloud_bank(scene.shape, 1.0), cloud_glow=0.25)

    assert len(detect_stars(overcast).xy) == 0


def test_stars_show_only_beside_a_cloud_bank() -> None:
    scene = SkyScene()
    half = scene.frame(frames=8, cloud=cloud_bank(scene.shape, 0.5), cloud_glow=0.15)

    found = detect_stars(half)

    width = scene.shape[1]
    assert (found.xy[:, 0] < 0.4 * width).sum() <= 2
    assert (found.xy[:, 0] > 0.6 * width).sum() > 50


def test_blobs_and_flat_bright_areas_are_not_stars() -> None:
    scene = SkyScene(stars=0)
    image = scene.frame(frames=8)
    y, x = np.mgrid[0 : scene.shape[0], 0 : scene.shape[1]]
    image += 0.3 * np.exp(-((x - 200) ** 2 + (y - 120) ** 2) / (2 * 6.0**2)).astype(np.float32)
    image[220:260, 400:450] = 1.0

    found = detect_stars(image)

    near_blob = np.hypot(found.xy[:, 0] - 200, found.xy[:, 1] - 120) < 20
    in_block = (found.xy[:, 0] > 395) & (found.xy[:, 0] < 455) & (found.xy[:, 1] > 215) & (found.xy[:, 1] < 265)
    assert not near_blob.any()
    assert not in_block.any()


def test_the_mask_limits_where_stars_are_looked_for() -> None:
    scene = SkyScene()
    mask = np.zeros(scene.shape, dtype=bool)
    mask[:, : scene.shape[1] // 2] = True

    found = detect_stars(scene.frame(frames=8), mask)

    assert len(found.xy) > 100
    assert (found.xy[:, 0] < scene.shape[1] // 2).all()
    assert found.background.shape == scene.shape

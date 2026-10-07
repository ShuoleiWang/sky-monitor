from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import numpy as np

from skymonitor.analysis import AnalysisParams, CameraAnalyzer, SkyRegion, Verdict, render_preview
from skymonitor.sources import FrameBurst
from skymonitor.synthetic import SkyScene, cloud_bank
from skymonitor_helpers import NIGHT, sky_burst

# The fixed sources count as known after 15 minutes: the first verdict comes at minute 15.
WARM_UP = 15


def _watch(analyzer: CameraAnalyzer, scene: SkyScene, minutes: range, **sky) -> list:
    return [analyzer.assess(sky_burst(scene, minute, **sky)) for minute in minutes]


def test_a_clear_sky_reads_clear_once_the_fixed_sources_are_known() -> None:
    scene = SkyScene(fixed_sources=30)
    analyzer = CameraAnalyzer()

    looks = _watch(analyzer, scene, range(WARM_UP + 3))

    assert (looks[0].verdict, looks[0].reason) == (Verdict.UNKNOWN, "CONFIRMING")
    assert {(look.verdict, look.reason) for look in looks[1:WARM_UP]} == {(Verdict.UNKNOWN, "WARMING_UP")}
    for look in looks[WARM_UP:]:
        assert (look.verdict, look.reason) == (Verdict.CLEAR, "CLEAR")
        assert look.coverage > 0.9
        assert look.star_count > 250
        # A star that happens to cross a fixed place is counted with it.
        assert 25 <= look.fixed_sources <= 34


def test_hot_pixels_under_an_overcast_sky_never_read_clear() -> None:
    # More fixed bright pixels than a clear sky needs stars, spread over the whole frame.
    scene = SkyScene(fixed_sources=80)
    analyzer = CameraAnalyzer()

    looks = _watch(analyzer, scene, range(WARM_UP + 5), cloud=cloud_bank(scene.shape, 1.0), glow=0.2)

    assert all(look.verdict is not Verdict.CLEAR for look in looks)
    assert all(look.verdict is not Verdict.PARTLY for look in looks)
    for look in looks[WARM_UP:]:
        assert (look.verdict, look.reason) == (Verdict.CLOUDY, "FEW_STARS")
        assert look.star_count <= 2
        assert look.fixed_sources >= 70


def test_half_a_sky_of_cloud_is_partly_cloudy() -> None:
    scene = SkyScene()
    analyzer = CameraAnalyzer()

    looks = _watch(analyzer, scene, range(WARM_UP + 3), cloud=cloud_bank(scene.shape, 0.5), glow=0.15)

    for look in looks[WARM_UP:]:
        assert look.verdict is Verdict.PARTLY
        assert 0.4 <= look.coverage <= 0.65


def test_mostly_cloud_is_cloudy_although_stars_show_in_the_gap() -> None:
    scene = SkyScene()
    analyzer = CameraAnalyzer()

    looks = _watch(analyzer, scene, range(WARM_UP + 3), cloud=cloud_bank(scene.shape, 0.8), glow=0.15)

    for look in looks[WARM_UP:]:
        assert (look.verdict, look.reason) == (Verdict.CLOUDY, "LOW_COVERAGE")
        assert look.star_count > 20


def test_single_pictures_are_confirmed_from_one_look_to_the_next() -> None:
    scene = SkyScene()
    analyzer = CameraAnalyzer()

    looks = _watch(analyzer, scene, range(WARM_UP + 2), pair=False)

    assert looks[0].reason == "CONFIRMING"
    assert looks[-1].verdict is Verdict.CLEAR
    assert looks[-1].frames == 1


def test_an_unchanged_picture_is_not_new_evidence() -> None:
    scene = SkyScene()
    analyzer = CameraAnalyzer(replace(AnalysisParams(), require_mature=False))
    first = analyzer.assess(sky_burst(scene, 0))
    again = replace(sky_burst(scene, 0), captured_at=NIGHT + timedelta(minutes=1))

    # The same picture a minute later would otherwise confirm every noise peak with itself.
    assert analyzer.assess(again) is first
    assert first.reason == "CONFIRMING"


def test_a_washed_out_frame_is_unknown() -> None:
    bright = np.full((360, 640), 0.95, dtype=np.float32)
    burst = FrameBurst((bright,), 0.0, 1, "bright", NIGHT)

    look = CameraAnalyzer().assess(burst)

    assert (look.verdict, look.reason) == (Verdict.UNKNOWN, "TOO_BRIGHT")


def test_the_fixed_sources_are_kept_for_the_next_run(tmp_path) -> None:
    scene = SkyScene(fixed_sources=30)
    state = tmp_path / "state" / "camera.fixed.npz"
    first_run = CameraAnalyzer(state_path=state)
    _watch(first_run, scene, range(WARM_UP + 1))
    first_run.save()

    # The next evening: nothing has to be learned again, only the stars confirmed once.
    second_run = CameraAnalyzer(state_path=state)
    looks = _watch(second_run, scene, range(20 * 60, 20 * 60 + 3))

    assert looks[0].reason == "CONFIRMING"
    assert looks[1].verdict is Verdict.CLEAR
    assert looks[1].fixed_sources >= 25


def test_a_reference_sets_the_star_count_a_clear_sky_needs() -> None:
    scene = SkyScene()
    params = replace(AnalysisParams(), require_mature=False)

    def second_look(**context) -> object:
        analyzer = CameraAnalyzer(params)
        analyzer.assess(sky_burst(scene, 0), **context)
        return analyzer.assess(sky_burst(scene, 1), **context)

    # The camera is said to show 1000 stars when clear: 300 are thin cloud or haze.
    hazy = second_look(reference_star_count=1000)
    moonlit = second_look(reference_star_count=1000, moon_strength=1.0)

    assert (hazy.verdict, hazy.required_stars) == (Verdict.PARTLY, 500)
    assert (moonlit.verdict, moonlit.required_stars) == (Verdict.CLEAR, 150)


def test_the_sky_region_masks_the_frame() -> None:
    region = SkyRegion(rectangle=(0.0, 0.0, 1.0, 0.5), exclude=((0.0, 0.0, 0.25, 0.1),))
    mask = region.mask((100, 200))
    circle = SkyRegion(circle=(0.5, 0.5, 0.5)).mask((100, 200))

    assert mask[:50].sum() == 50 * 200 - 10 * 50
    assert not mask[50:].any()
    assert not mask[5, 10]
    assert circle[50, 100] and not circle[50, 20]
    assert SkyRegion().mask((100, 200)) is None


def test_only_the_sky_region_is_judged() -> None:
    scene = SkyScene()
    # Cloud over the left half, and the camera is told that only the right half is sky.
    region = SkyRegion(rectangle=(0.55, 0.0, 1.0, 1.0))
    analyzer = CameraAnalyzer(replace(AnalysisParams(), require_mature=False), region=region)

    looks = _watch(analyzer, scene, range(2), cloud=cloud_bank(scene.shape, 0.5), glow=0.15)

    assert looks[1].verdict is Verdict.CLEAR
    assert looks[1].coverage > 0.9


def test_the_preview_marks_what_was_measured() -> None:
    scene = SkyScene()
    analyzer = CameraAnalyzer(replace(AnalysisParams(), require_mature=False))
    look = _watch(analyzer, scene, range(2))[1]

    picture = render_preview(look.overlay, "sky  CLEAR  晴")

    assert picture.size == (scene.shape[1], scene.shape[0])
    assert picture.mode == "RGB"
    colours = np.asarray(picture).reshape(-1, 3)
    assert ((colours[:, 1] > 200) & (colours[:, 0] < 120)).sum() > 500

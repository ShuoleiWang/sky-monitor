from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from skymonitor.analysis import AnalysisParams, CameraAnalyzer, Verdict
from skymonitor.decision import DecisionMachine, DecisionParams
from skymonitor.ephemeris import moon_events, sun_moon
from skymonitor.forecast import observed_cloud_from_history
from skymonitor.synthetic import SkyScene
from skymonitor_helpers import NIGHT, sky_burst


def test_moonrise_and_moonset_are_found_to_the_minute() -> None:
    # Lijiang area: the Moon was 47 deg below the horizon at 13:00 UTC and 34 deg up at 22:30 UTC.
    start = datetime(2026, 10, 6, 13, 0, tzinfo=UTC)
    events = moon_events(start, 26.70, 100.03)
    rise = events["rise"]

    assert rise is not None and start < rise < datetime(2026, 10, 6, 22, 30, tzinfo=UTC)
    assert abs(sun_moon(rise, 26.70, 100.03).moon.altitude_deg) < 0.05
    assert sun_moon(rise + timedelta(hours=1), 26.70, 100.03).moon.altitude_deg > 0
    moon_set = events["set"]
    assert moon_set is not None and moon_set > rise
    assert abs(sun_moon(moon_set, 26.70, 100.03).moon.altitude_deg) < 0.05


def test_moonlight_lowers_the_stars_a_clear_sky_must_show() -> None:
    scene = SkyScene()
    params = replace(AnalysisParams(), require_mature=False)

    def second_look(moon_strength: float) -> int:
        analyzer = CameraAnalyzer(params)
        analyzer.assess(sky_burst(scene, 0), moon_strength=moon_strength)
        return analyzer.assess(sky_burst(scene, 1), moon_strength=moon_strength).required_stars

    assert second_look(0.0) == 50
    assert second_look(0.5) == 33
    assert second_look(1.0) == 15


def test_the_moons_halo_blinds_its_cells_only_while_the_moon_is_up() -> None:
    scene = SkyScene()
    params = replace(AnalysisParams(), require_mature=False)
    y, x = np.mgrid[0 : scene.shape[0], 0 : scene.shape[1]]
    halo = (0.45 * np.exp(-((x - 560) ** 2 + (y - 80) ** 2) / (2 * 70.0**2))).astype(np.float32)

    def second_look(moon_strength: float):
        analyzer = CameraAnalyzer(params)
        for minute in (0, 1):
            burst = sky_burst(scene, minute)
            lit = replace(
                burst, images=tuple(np.clip(image + halo, 0, 1) for image in burst.images), content_id=f"halo-{minute}"
            )
            look = analyzer.assess(lit, moon_strength=moon_strength)
        return look

    moonless, moonlit = second_look(0.0), second_look(0.6)

    assert moonless.cells_blinded == 0
    assert moonlit.cells_blinded >= 1
    assert moonlit.verdict is Verdict.CLEAR and moonlit.coverage > 0.9


def test_a_bright_moon_can_hold_back_imaging_but_never_the_roof() -> None:
    machine = DecisionMachine(DecisionParams(imaging_moon_max_illumination=0.5))
    when = NIGHT
    for _ in range(11):
        when += timedelta(minutes=1)
        up = machine.update(when, [Verdict.CLEAR], [0.9], -30.0, moon_altitude_deg=40.0, moon_illumination=0.9)
    when += timedelta(minutes=1)
    down = machine.update(when, [Verdict.CLEAR], [0.9], -30.0, moon_altitude_deg=-5.0, moon_illumination=0.9)
    when += timedelta(minutes=1)
    thin = machine.update(when, [Verdict.CLEAR], [0.9], -30.0, moon_altitude_deg=40.0, moon_illumination=0.2)

    assert (up.safe, up.imaging_ok) == (True, False)
    assert (down.safe, down.imaging_ok) == (True, True)
    assert (thin.safe, thin.imaging_ok) == (True, True)


def test_moonlit_hours_do_not_score_the_forecasts() -> None:
    hour = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)
    entries = []
    for minute in range(0, 60, 10):
        moment = (hour + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")
        entries.append(
            {
                "updatedAt": moment,
                "coverage": 0.6,
                "verdict": "PARTLY",
                "moon": {"altitudeDeg": 60.0, "illumination": 0.95},
            }
        )
        later = (hour + timedelta(hours=1, minutes=minute)).isoformat().replace("+00:00", "Z")
        entries.append(
            {
                "updatedAt": later,
                "coverage": 0.6,
                "verdict": "PARTLY",
                "moon": {"altitudeDeg": -10.0, "illumination": 0.95},
            }
        )

    observed = observed_cloud_from_history(entries)

    assert observed == {hour + timedelta(hours=1): pytest.approx(0.4)}

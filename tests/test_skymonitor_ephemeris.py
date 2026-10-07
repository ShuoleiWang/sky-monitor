from __future__ import annotations

from datetime import UTC, datetime

import pytest

from skymonitor.ephemeris import julian_day, sun_moon

# Astropy 8.0.1 (get_sun, get_body("moon") to AltAz without refraction), computed once:
# UTC, latitude, longitude, Sun altitude and azimuth, Moon altitude and azimuth, lit fraction.
ASTROPY = (
    ("2026-10-06T13:00:00", 26.7, 100.03, -27.313, 278.308, -46.910, 339.658, 0.1877),
    ("2026-10-06T22:30:00", 26.7, 100.03, -10.871, 90.612, 34.439, 93.393, 0.1545),
    ("2026-01-15T11:20:00", 40.39, 117.58, -24.821, 262.722, -60.189, 283.770, 0.1058),
    ("2026-06-21T04:00:00", 32.31, 80.03, 53.423, 93.584, -25.856, 69.965, 0.4235),
    ("2027-03-03T18:45:00", -30.17, -70.8, 55.120, 306.737, 20.991, 253.879, 0.1880),
    ("2025-12-04T15:10:00", 38.61, 93.9, -55.130, 287.206, 61.552, 106.568, 0.9964),
    ("2030-08-13T20:00:00", 19.82, -155.47, 54.614, 92.821, -50.338, 280.880, 0.9963),
    ("2026-10-26T12:00:00", 26.7, 100.03, -18.007, 264.716, 17.127, 77.942, 0.9969),
)


def _angle_between(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


@pytest.mark.parametrize("row", ASTROPY, ids=[row[0] for row in ASTROPY])
def test_sun_and_moon_agree_with_astropy(row) -> None:
    moment, latitude, longitude, sun_alt, sun_az, moon_alt, moon_az, lit = row
    when = datetime.fromisoformat(moment).replace(tzinfo=UTC)

    sky = sun_moon(when, latitude, longitude)

    assert abs(sky.sun.altitude_deg - sun_alt) < 0.03
    assert _angle_between(sky.sun.azimuth_deg, sun_az) < 0.03
    assert abs(sky.moon.altitude_deg - moon_alt) < 0.05
    assert _angle_between(sky.moon.azimuth_deg, moon_az) < 0.05
    assert abs(sky.moon_illumination - lit) < 0.002


def test_moonlight_counts_only_above_the_horizon() -> None:
    full_and_high = sun_moon(datetime(2025, 12, 4, 15, 10, tzinfo=UTC), 38.61, 93.9)
    full_and_down = sun_moon(datetime(2030, 8, 13, 20, 0, tzinfo=UTC), 19.82, -155.47)

    assert full_and_high.moon_strength == pytest.approx(full_and_high.moon_illumination)
    assert full_and_down.moon_strength == 0.0


def test_a_time_without_a_zone_is_refused() -> None:
    with pytest.raises(ValueError):
        julian_day(datetime(2026, 10, 6, 13, 0))

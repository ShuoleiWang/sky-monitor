"""Sun and Moon positions for the darkness gate and the moonlight allowance.

Low-precision series from Meeus, *Astronomical Algorithms* (2nd ed., chapters
12, 13, 25, 47 and 48): about 0.01 degrees for the Sun and 0.1 degrees for the
Moon in this century, with no ephemeris files and no network access.
Refraction is not applied; the gates that use these altitudes sit several
degrees below the horizon.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

_EARTH_RADIUS_KM = 6378.14
_AU_KM = 149_597_870.7
# TT - UT; a 10 s error moves the Moon by 0.002 degrees.
_DELTA_T_SECONDS = 69.2

# (D, M, M', F, longitude coefficient [1e-6 deg], distance coefficient [1e-3 km])
_MOON_LONGITUDE_DISTANCE = (
    (0, 0, 1, 0, 6288774, -20905355),
    (2, 0, -1, 0, 1274027, -3699111),
    (2, 0, 0, 0, 658314, -2955968),
    (0, 0, 2, 0, 213618, -569925),
    (0, 1, 0, 0, -185116, 48888),
    (0, 0, 0, 2, -114332, -3149),
    (2, 0, -2, 0, 58793, 246158),
    (2, -1, -1, 0, 57066, -152138),
    (2, 0, 1, 0, 53322, -170733),
    (2, -1, 0, 0, 45758, -204586),
    (0, 1, -1, 0, -40923, -129620),
    (1, 0, 0, 0, -34720, 108743),
    (0, 1, 1, 0, -30383, 104755),
    (2, 0, 0, -2, 15327, 10321),
    (0, 0, 1, 2, -12528, 0),
    (0, 0, 1, -2, 10980, 79661),
    (4, 0, -1, 0, 10675, -34782),
    (0, 0, 3, 0, 10034, -23210),
    (4, 0, -2, 0, 8548, -21636),
    (2, 1, -1, 0, -7888, 24208),
    (2, 1, 0, 0, -6766, 30824),
    (1, 0, -1, 0, -5163, -8379),
    (1, 1, 0, 0, 4987, -16675),
    (2, -1, 1, 0, 4036, -12831),
    (2, 0, 2, 0, 3994, -10445),
    (4, 0, 0, 0, 3861, -11650),
    (2, 0, -3, 0, 3665, 14403),
    (0, 1, -2, 0, -2689, -7003),
    (2, 0, -1, 2, -2602, 0),
    (2, -1, -2, 0, 2390, 10056),
    (1, 0, 1, 0, -2348, 6322),
    (2, -2, 0, 0, 2236, -9884),
    (0, 1, 2, 0, -2120, 5751),
    (0, 2, 0, 0, -2069, 0),
    (2, -2, -1, 0, 2048, -4950),
    (2, 0, 1, -2, -1773, 4130),
    (2, 0, 0, 2, -1595, 0),
    (4, -1, -1, 0, 1215, -3958),
    (0, 0, 2, 2, -1110, 0),
    (3, 0, -1, 0, -892, 3258),
    (2, 1, 1, 0, -810, 2616),
    (4, -1, -2, 0, 759, -1897),
    (0, 2, -1, 0, -713, -2117),
    (2, 2, -1, 0, -700, 2354),
    (2, 1, -2, 0, 691, 0),
    (2, -1, 0, -2, 596, 0),
    (4, 0, 1, 0, 549, -1423),
    (0, 0, 4, 0, 537, -1117),
    (4, -1, 0, 0, 520, -1571),
    (1, 0, -2, 0, -487, -1739),
)

# (D, M, M', F, latitude coefficient [1e-6 deg])
_MOON_LATITUDE = (
    (0, 0, 0, 1, 5128122),
    (0, 0, 1, 1, 280602),
    (0, 0, 1, -1, 277693),
    (2, 0, 0, -1, 173237),
    (2, 0, -1, 1, 55413),
    (2, 0, -1, -1, 46271),
    (2, 0, 0, 1, 32573),
    (0, 0, 2, 1, 17198),
    (2, 0, 1, -1, 9266),
    (0, 0, 2, -1, 8822),
    (2, -1, 0, -1, 8216),
    (2, 0, -2, -1, 4324),
    (2, 0, 1, 1, 4200),
    (2, 1, 0, -1, -3359),
    (2, -1, -1, 1, 2463),
    (2, -1, 0, 1, 2211),
    (2, -1, -1, -1, 2065),
    (0, 1, -1, -1, -1870),
    (4, 0, -1, -1, 1828),
    (0, 1, 0, 1, -1794),
    (0, 0, 0, 3, -1749),
    (0, 1, -1, 1, -1565),
    (1, 0, 0, 1, -1491),
    (0, 1, 1, 1, -1475),
    (0, 1, 1, -1, -1410),
    (0, 1, 0, -1, -1344),
    (1, 0, 0, -1, -1335),
    (0, 0, 3, 1, 1107),
    (4, 0, 0, -1, 1021),
    (4, 0, -1, 1, 833),
)


@dataclass(frozen=True)
class Position:
    """Altitude above the horizon and azimuth from north through east, in degrees."""

    altitude_deg: float
    azimuth_deg: float


@dataclass(frozen=True)
class SunMoon:
    sun: Position
    moon: Position
    # Illuminated fraction of the Moon's disc, 0 (new) to 1 (full).
    moon_illumination: float

    @property
    def moon_strength(self) -> float:
        """How much moonlight brightens the sky, 0 (none) to 1 (full Moon high up)."""

        return self.moon_illumination * min(max(self.moon.altitude_deg / 30.0, 0.0), 1.0)


def julian_day(when: datetime) -> float:
    if when.tzinfo is None:
        raise ValueError("a timezone-aware datetime is required")
    return when.timestamp() / 86400.0 + 2440587.5


def _obliquity(t: float) -> float:
    seconds = 21.448 - t * (46.8150 + t * (0.00059 - 0.001813 * t))
    return math.radians(23.0 + (26.0 + seconds / 60.0) / 60.0)


def _sun(t: float) -> tuple[float, float, float]:
    """Apparent right ascension and declination (radians) and distance (km)."""

    mean_longitude = 280.46646 + t * (36000.76983 + 0.0003032 * t)
    anomaly = math.radians(357.52911 + t * (35999.05029 - 0.0001537 * t))
    centre = (
        (1.914602 - t * (0.004817 + 0.000014 * t)) * math.sin(anomaly)
        + (0.019993 - 0.000101 * t) * math.sin(2 * anomaly)
        + 0.000289 * math.sin(3 * anomaly)
    )
    node = math.radians(125.04 - 1934.136 * t)
    longitude = math.radians(mean_longitude + centre - 0.00569 - 0.00478 * math.sin(node))
    obliquity = _obliquity(t) + math.radians(0.00256 * math.cos(node))
    eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    distance = (
        _AU_KM * 1.000001018 * (1 - eccentricity**2) / (1 + eccentricity * math.cos(anomaly + math.radians(centre)))
    )
    right_ascension = math.atan2(math.cos(obliquity) * math.sin(longitude), math.cos(longitude))
    declination = math.asin(math.sin(obliquity) * math.sin(longitude))
    return right_ascension, declination, distance


def _moon(t: float) -> tuple[float, float, float]:
    """Geocentric right ascension and declination (radians) and distance (km)."""

    mean_longitude = 218.3164477 + t * (481267.88123421 + t * (-0.0015786 + t * (1 / 538841 - t / 65194000)))
    elongation = 297.8501921 + t * (445267.1114034 + t * (-0.0018819 + t * (1 / 545868 - t / 113065000)))
    sun_anomaly = 357.5291092 + t * (35999.0502909 + t * (-0.0001536 + t / 24490000))
    moon_anomaly = 134.9633964 + t * (477198.8675055 + t * (0.0087414 + t * (1 / 69699 - t / 14712000)))
    latitude_argument = 93.2720950 + t * (483202.0175233 + t * (-0.0036539 + t * (-1 / 3526000 + t / 863310000)))
    a1 = math.radians(119.75 + 131.849 * t)
    a2 = math.radians(53.09 + 479264.290 * t)
    a3 = math.radians(313.45 + 481266.484 * t)
    eccentricity = 1 - t * (0.002516 + 0.0000074 * t)

    lp = math.radians(mean_longitude)
    d = math.radians(elongation)
    m = math.radians(sun_anomaly)
    mp = math.radians(moon_anomaly)
    f = math.radians(latitude_argument)

    sum_longitude = 3958 * math.sin(a1) + 1962 * math.sin(lp - f) + 318 * math.sin(a2)
    sum_distance = 0.0
    for cd, cm, cmp_, cf, longitude_term, distance_term in _MOON_LONGITUDE_DISTANCE:
        argument = cd * d + cm * m + cmp_ * mp + cf * f
        scale = eccentricity ** abs(cm)
        sum_longitude += longitude_term * scale * math.sin(argument)
        sum_distance += distance_term * scale * math.cos(argument)
    sum_latitude = (
        -2235 * math.sin(lp)
        + 382 * math.sin(a3)
        + 175 * math.sin(a1 - f)
        + 175 * math.sin(a1 + f)
        + 127 * math.sin(lp - mp)
        - 115 * math.sin(lp + mp)
    )
    for cd, cm, cmp_, cf, latitude_term in _MOON_LATITUDE:
        argument = cd * d + cm * m + cmp_ * mp + cf * f
        sum_latitude += latitude_term * eccentricity ** abs(cm) * math.sin(argument)

    longitude = math.radians(mean_longitude + sum_longitude / 1e6)
    latitude = math.radians(sum_latitude / 1e6)
    distance = 385000.56 + sum_distance / 1000.0
    obliquity = _obliquity(t)
    right_ascension = math.atan2(
        math.sin(longitude) * math.cos(obliquity) - math.tan(latitude) * math.sin(obliquity),
        math.cos(longitude),
    )
    declination = math.asin(
        math.sin(latitude) * math.cos(obliquity) + math.cos(latitude) * math.sin(obliquity) * math.sin(longitude)
    )
    return right_ascension, declination, distance


def _horizontal(right_ascension: float, declination: float, sidereal: float, latitude: float) -> tuple[float, float]:
    hour_angle = sidereal - right_ascension
    sine = math.sin(latitude) * math.sin(declination) + math.cos(latitude) * math.cos(declination) * math.cos(
        hour_angle
    )
    altitude = math.asin(min(max(sine, -1.0), 1.0))
    azimuth = math.atan2(
        -math.cos(declination) * math.sin(hour_angle),
        math.sin(declination) * math.cos(latitude) - math.cos(declination) * math.cos(hour_angle) * math.sin(latitude),
    )
    return altitude, azimuth % (2 * math.pi)


def moon_events(
    when: datetime, latitude_deg: float, longitude_deg: float, hours: float = 24.0
) -> dict[str, datetime | None]:
    """The next moonrise and moonset within ``hours``, to the minute; None when there is none."""

    step = timedelta(minutes=5)
    moments = [when + step * index for index in range(int(hours * 60 / 5) + 1)]
    altitudes = [sun_moon(moment, latitude_deg, longitude_deg).moon.altitude_deg for moment in moments]
    events: dict[str, datetime | None] = {"rise": None, "set": None}
    for earlier, later, low, high in zip(moments, moments[1:], altitudes, altitudes[1:], strict=False):
        kind = "rise" if low < 0 <= high else "set" if high < 0 <= low else None
        if kind is None or events[kind] is not None:
            continue
        start, end = earlier, later
        for _ in range(6):  # bisection: 5 minutes down to about 5 seconds
            middle = start + (end - start) / 2
            if (sun_moon(middle, latitude_deg, longitude_deg).moon.altitude_deg < 0) == (kind == "rise"):
                start = middle
            else:
                end = middle
        events[kind] = start + (end - start) / 2
    return events


def sun_moon(when: datetime, latitude_deg: float, longitude_deg: float) -> SunMoon:
    """Sun and Moon as seen from a site; longitude is positive east."""

    jd = julian_day(when)
    t = (jd - 2451545.0) / 36525.0
    dynamical = t + _DELTA_T_SECONDS / (86400.0 * 36525.0)
    sidereal = math.radians(
        280.46061837 + 360.98564736629 * (jd - 2451545.0) + t * t * (0.000387933 - t / 38710000.0) + longitude_deg
    )
    latitude = math.radians(latitude_deg)

    sun_ra, sun_dec, sun_distance = _sun(dynamical)
    moon_ra, moon_dec, moon_distance = _moon(dynamical)
    sun_altitude, sun_azimuth = _horizontal(sun_ra, sun_dec, sidereal, latitude)
    moon_altitude, moon_azimuth = _horizontal(moon_ra, moon_dec, sidereal, latitude)
    # The Moon is close enough for the observer's place on the Earth to matter.
    moon_altitude -= math.asin(_EARTH_RADIUS_KM / moon_distance) * math.cos(moon_altitude)

    cosine = math.sin(sun_dec) * math.sin(moon_dec) + math.cos(sun_dec) * math.cos(moon_dec) * math.cos(
        sun_ra - moon_ra
    )
    separation = math.acos(min(max(cosine, -1.0), 1.0))
    phase_angle = math.atan2(sun_distance * math.sin(separation), moon_distance - sun_distance * math.cos(separation))
    return SunMoon(
        sun=Position(math.degrees(sun_altitude), math.degrees(sun_azimuth)),
        moon=Position(math.degrees(moon_altitude), math.degrees(moon_azimuth)),
        moon_illumination=(1 + math.cos(phase_angle)) / 2,
    )

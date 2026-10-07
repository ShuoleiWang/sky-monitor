"""7Timer's astronomy product: cloud, seeing and transparency every three hours (www.7timer.info)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from ..forecast import Forecast, HourForecast, ProviderError
from ._http import coordinate, get_json, now_utc, number

URL = "https://www.7timer.info/bin/astro.php"

# Cloud cover classes 1-9 (0-6 %, 6-19 %, ..., 94-100 %) at the middle of their band.
_CLOUD = {1: 0.03, 2: 0.125, 3: 0.25, 4: 0.375, 5: 0.5, 6: 0.625, 7: 0.75, 8: 0.875, 9: 0.97}
# Wind speed classes 1-8 (calm ... hurricane) at the middle of their band, m/s.
_WIND = {1: 0.15, 2: 1.9, 3: 5.7, 4: 9.4, 5: 14.0, 6: 20.9, 7: 28.6, 8: 36.0}


class SevenTimer:
    name = "7timer"
    model = "astro"

    def url(self, latitude: float, longitude: float) -> str:
        query = {
            "lon": coordinate(longitude),
            "lat": coordinate(latitude),
            "ac": "0",
            "unit": "metric",
            "output": "json",
            "tzshift": "0",
        }
        return f"{URL}?{urlencode(query)}"

    def fetch(self, latitude: float, longitude: float) -> list[Forecast]:
        payload = get_json(self.url(latitude, longitude))
        fetched = now_utc()
        try:
            return [self.parse(payload, fetched)]
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
            raise ProviderError(f"unexpected answer ({type(error).__name__}: {error})") from None

    def parse(self, payload: object, fetched: datetime) -> Forecast:
        if not isinstance(payload, Mapping):
            raise ProviderError("unexpected answer (not an object)")
        issued = datetime.strptime(str(payload["init"]), "%Y%m%d%H").replace(tzinfo=UTC)
        hours: dict[datetime, HourForecast] = {}
        for point in payload["dataseries"]:
            start = issued + timedelta(hours=int(point["timepoint"]))
            precipitation = point.get("prec_type")
            values = {
                "cloud": _CLOUD.get(_whole(point.get("cloudcover"))),
                "seeing": _index(point.get("seeing")),
                "transparency": _index(point.get("transparency")),
                "humidity": _humidity(point.get("rh2m")),
                "wind_ms": _WIND.get(_whole((point.get("wind10m") or {}).get("speed"))),
                "temperature_c": _temperature(point.get("temp2m")),
                "precipitation_probability": None
                if not isinstance(precipitation, str)
                else (0.0 if precipitation == "none" else 0.7),
            }
            # A point stands for its own hour and the two after it.
            for offset in range(3):
                moment = start + timedelta(hours=offset)
                hours[moment] = HourForecast(time=moment, **values)
        return Forecast(
            self.name, self.model, issued_at=issued, fetched_at=fetched, hours=tuple(hours[t] for t in sorted(hours))
        )


def _whole(value: object) -> int | None:
    result = number(value)
    return None if result is None or result != int(result) else int(result)


def _index(value: object) -> float | None:
    """Seeing and transparency, 1 (best) to 8; 7Timer marks a missing value with -9999."""

    result = _whole(value)
    return None if result is None or not 1 <= result <= 8 else float(result)


def _humidity(value: object) -> float | None:
    """Relative humidity classes -4 (0-5 %) to 16 (100 %), at the middle of their band."""

    result = _whole(value)
    if result is None or not -4 <= result <= 16:
        return None
    return min(1.0, ((result + 4) * 5 + 2.5) / 100.0)


def _temperature(value: object) -> float | None:
    result = number(value)
    return None if result is None or not -90.0 < result < 70.0 else result

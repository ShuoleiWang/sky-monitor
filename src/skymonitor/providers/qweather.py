"""QWeather (和风天气): the hourly forecast for the next 24 hours.

The key travels in a request header, never in the address; accounts created since 2024 have
their own API host, which ``host`` names.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from ..forecast import Forecast, HourForecast, ProviderError, hour_of
from ._http import coordinate, get_json, now_utc, number, parse_time, redact, scaled


class QWeather:
    name = "qweather"
    model = "24h"

    def __init__(self, key: str, host: str = "devapi.qweather.com") -> None:
        self._key = key
        self.host = host.strip().removeprefix("https://").removeprefix("http://").strip("/")

    def url(self, latitude: float, longitude: float) -> str:
        return f"https://{self.host}/v7/weather/24h?location={coordinate(longitude)},{coordinate(latitude)}"

    def fetch(self, latitude: float, longitude: float) -> list[Forecast]:
        try:
            payload = get_json(self.url(latitude, longitude), headers={"X-QW-Api-Key": self._key}, secrets=(self._key,))
            return [self.parse(payload, now_utc())]
        except ProviderError as error:
            raise ProviderError(redact(str(error), (self._key,))) from None
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
            raise ProviderError(redact(f"unexpected answer ({type(error).__name__}: {error})", (self._key,))) from None

    def parse(self, payload: object, fetched: datetime) -> Forecast:
        if not isinstance(payload, Mapping):
            raise ProviderError("unexpected answer (not an object)")
        code = str(payload.get("code"))
        if code != "200":
            raise ProviderError(f"QWeather answered code {code}")
        updated = payload.get("updateTime")
        issued = parse_time(str(updated)) if updated else None
        hours = []
        for entry in payload["hourly"]:
            hours.append(
                HourForecast(
                    time=hour_of(parse_time(entry["fxTime"])),
                    cloud=scaled(entry.get("cloud"), 0.01),
                    precipitation_mm=number(entry.get("precip")),
                    precipitation_probability=scaled(entry.get("pop"), 0.01),
                    wind_ms=scaled(entry.get("windSpeed"), 1.0 / 3.6),
                    humidity=scaled(entry.get("humidity"), 0.01),
                    dew_point_c=number(entry.get("dew")),
                    temperature_c=number(entry.get("temp")),
                )
            )
        return Forecast(self.name, self.model, issued_at=issued, fetched_at=fetched, hours=tuple(hours))

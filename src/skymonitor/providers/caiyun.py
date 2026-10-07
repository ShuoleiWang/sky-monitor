"""Caiyun (彩云天气): hourly cloud, rain, wind and humidity, and radar rain for the next two hours.

The token is part of the address, so every failure is reported with the token replaced.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from ..forecast import Forecast, HourForecast, ProviderError, hour_of
from ._http import coordinate, get_json, now_utc, number, parse_time, redact, scaled

URL = "https://api.caiyunapp.com/v2.6/{token}/{longitude},{latitude}/weather?alert=false&dailysteps=1&hourlysteps=48"


class Caiyun:
    name = "caiyun"
    model = "v2.6"

    def __init__(self, token: str) -> None:
        self._token = token

    def url(self, latitude: float, longitude: float) -> str:
        return URL.format(token=self._token, longitude=coordinate(longitude), latitude=coordinate(latitude))

    def fetch(self, latitude: float, longitude: float) -> list[Forecast]:
        try:
            payload = get_json(self.url(latitude, longitude), secrets=(self._token,))
            return [self.parse(payload, now_utc())]
        except ProviderError as error:
            raise ProviderError(redact(str(error), (self._token,))) from None
        except Exception as error:
            raise ProviderError(
                redact(f"unexpected answer ({type(error).__name__}: {error})", (self._token,))
            ) from None

    def parse(self, payload: object, fetched: datetime) -> Forecast:
        if not isinstance(payload, Mapping):
            raise ProviderError("unexpected answer (not an object)")
        if payload.get("status") != "ok":
            detail = payload.get("error") or payload.get("message") or ""
            raise ProviderError(f"Caiyun answered status {payload.get('status')!r}" + (f": {detail}" if detail else ""))
        result = payload["result"]
        hourly = result["hourly"]
        if hourly.get("status", "ok") != "ok":
            raise ProviderError(f"Caiyun's hourly forecast has status {hourly.get('status')!r}")
        server_time = number(payload.get("server_time"))
        issued = None if server_time is None else datetime.fromtimestamp(server_time, UTC)

        table: dict[datetime, dict[str, float | None]] = {}

        def put(series: object, field: str, value) -> None:
            for entry in series or []:  # type: ignore[union-attr]
                moment = hour_of(parse_time(entry["datetime"]))
                table.setdefault(moment, {})[field] = value(entry)

        put(hourly.get("cloudrate"), "cloud", lambda entry: number(entry.get("value")))
        put(hourly.get("precipitation"), "precipitation_mm", lambda entry: number(entry.get("value")))
        put(
            hourly.get("precipitation"),
            "precipitation_probability",
            lambda entry: scaled(entry.get("probability"), 0.01),
        )
        put(hourly.get("wind"), "wind_ms", lambda entry: scaled(entry.get("speed"), 1.0 / 3.6))
        put(hourly.get("humidity"), "humidity", lambda entry: number(entry.get("value")))
        put(hourly.get("temperature"), "temperature_c", lambda entry: number(entry.get("value")))
        _fold_minutely(table, result.get("minutely"), issued or fetched)
        hours = tuple(HourForecast(time=moment, **table[moment]) for moment in sorted(table))
        return Forecast(self.name, self.model, issued_at=issued, fetched_at=fetched, hours=hours)


def _fold_minutely(table: dict[datetime, dict[str, float | None]], minutely: object, start: datetime) -> None:
    """Radar rain for the next two hours (mm/h each minute, a probability each half hour) raises the hours it falls in."""

    if not isinstance(minutely, Mapping) or minutely.get("status", "ok") != "ok":
        return

    def raise_to(moment: datetime, field: str, value: float | None) -> None:
        entry = table.get(hour_of(moment))
        if entry is None or value is None:
            return
        current = entry.get(field)
        entry[field] = value if current is None else max(current, value)

    for minute, value in enumerate(minutely.get("precipitation_2h") or []):
        raise_to(start + timedelta(minutes=minute), "precipitation_mm", number(value))
    for half_hour, value in enumerate(minutely.get("probability") or []):
        for edge in (0, 29):
            raise_to(start + timedelta(minutes=30 * half_hour + edge), "precipitation_probability", number(value))

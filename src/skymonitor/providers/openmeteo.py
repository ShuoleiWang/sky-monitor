"""Open-Meteo: several weather models through one free API without a key (api.open-meteo.com)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from urllib.parse import urlencode

from ..forecast import Forecast, HourForecast, ProviderError, hour_of
from ._http import coordinate, get_json, now_utc, parse_time, scaled

URL = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo's hourly variable, the HourForecast field it fills, and the factor to plain units.
_VARIABLES = (
    ("cloud_cover", "cloud", 0.01),
    ("cloud_cover_low", "cloud_low", 0.01),
    ("cloud_cover_mid", "cloud_mid", 0.01),
    ("cloud_cover_high", "cloud_high", 0.01),
    ("precipitation", "precipitation_mm", 1.0),
    ("precipitation_probability", "precipitation_probability", 0.01),
    ("wind_speed_10m", "wind_ms", 1.0),
    ("wind_gusts_10m", "gust_ms", 1.0),
    ("relative_humidity_2m", "humidity", 0.01),
    ("dew_point_2m", "dew_point_c", 1.0),
    ("temperature_2m", "temperature_c", 1.0),
)
# Sums and maxima "of the preceding hour" are stamped with the hour's end; the rest are instants.
_PRECEDING_HOUR = frozenset({"precipitation", "precipitation_probability", "wind_gusts_10m"})


class OpenMeteo:
    """One Forecast per model; the models are Open-Meteo's names (``best_match``, ``ecmwf_ifs025``, ...)."""

    name = "open-meteo"

    def __init__(self, models: Sequence[str] = ("best_match",)) -> None:
        self.models = tuple(models) or ("best_match",)

    def url(self, latitude: float, longitude: float) -> str:
        query = {
            "latitude": coordinate(latitude),
            "longitude": coordinate(longitude),
            "hourly": ",".join(variable for variable, _, _ in _VARIABLES),
            "models": ",".join(self.models),
            "timezone": "UTC",
            "forecast_days": "3",
            "wind_speed_unit": "ms",
        }
        return f"{URL}?{urlencode(query, safe=',')}"

    def fetch(self, latitude: float, longitude: float) -> list[Forecast]:
        payload = get_json(self.url(latitude, longitude))
        fetched = now_utc()
        try:
            return self.parse(payload, fetched)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
            raise ProviderError(f"unexpected answer ({type(error).__name__}: {error})") from None

    def parse(self, payload: object, fetched: datetime) -> list[Forecast]:
        if not isinstance(payload, Mapping):
            raise ProviderError("unexpected answer (not an object)")
        hourly = payload["hourly"]
        offset = float(payload.get("utc_offset_seconds") or 0.0)
        times = [hour_of(parse_time(text, offset_seconds=offset)) for text in hourly["time"]]
        row = {moment: index for index, moment in enumerate(times)}
        single = len(self.models) == 1
        forecasts = []
        for model in self.models:
            columns = {variable: _column(hourly, variable, model, single) for variable, _, _ in _VARIABLES}
            hours = []
            for moment in times:
                values: dict[str, float | None] = {}
                for variable, field, factor in _VARIABLES:
                    column = columns[variable]
                    index = row.get(moment + timedelta(hours=1)) if variable in _PRECEDING_HOUR else row[moment]
                    raw = column[index] if column is not None and index is not None and index < len(column) else None
                    values[field] = scaled(raw, factor)
                if any(value is not None for value in values.values()):
                    hours.append(HourForecast(time=moment, **values))
            forecasts.append(Forecast(self.name, model, issued_at=None, fetched_at=fetched, hours=tuple(hours)))
        return forecasts


def _column(hourly: Mapping[str, object], variable: str, model: str, single: bool) -> list | None:
    """With several models every variable carries the model's name as a suffix; with one it does not."""

    names = (variable, f"{variable}_{model}") if single else (f"{variable}_{model}",)
    for name in names:
        column = hourly.get(name)
        if isinstance(column, list):
            return column
    return None

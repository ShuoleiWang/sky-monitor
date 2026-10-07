"""Weather forecasts for the night: several sources, one consensus, and a score for each
source measured against this site's own camera.

A forecast is advice next to the cameras, never a substitute: a model grid is ten
kilometres wide and an hour coarse, while the camera sees this sky now.  What a forecast
adds is the hours ahead (will it stay clear, is rain coming) and the quantities a camera
cannot measure (wind, humidity).  Which source is right for this site is not known in
advance, so every night the camera's measured coverage is compared with what each source
predicted, and the consensus leans on the sources that have been right here.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol

_LOG = logging.getLogger(__name__)

# The quantities a source may give for one hour; all optional, all in plain units.
_FIELDS = (
    "cloud",
    "cloud_low",
    "cloud_mid",
    "cloud_high",
    "precipitation_mm",
    "precipitation_probability",
    "wind_ms",
    "gust_ms",
    "humidity",
    "dew_point_c",
    "temperature_c",
    "seeing",
    "transparency",
)
_JSON_NAMES = {
    "cloud": "cloud",
    "cloud_low": "cloudLow",
    "cloud_mid": "cloudMid",
    "cloud_high": "cloudHigh",
    "precipitation_mm": "rainMm",
    "precipitation_probability": "rainProbability",
    "wind_ms": "windMs",
    "gust_ms": "gustMs",
    "humidity": "humidity",
    "dew_point_c": "dewPointC",
    "temperature_c": "temperatureC",
    "seeing": "seeing",
    "transparency": "transparency",
}


@dataclass(frozen=True)
class HourForecast:
    # Start of the hour, UTC.
    time: datetime
    # Fractions are 0..1; speeds m/s; temperatures Celsius; rain mm in the hour.
    cloud: float | None = None
    cloud_low: float | None = None
    cloud_mid: float | None = None
    cloud_high: float | None = None
    precipitation_mm: float | None = None
    precipitation_probability: float | None = None
    wind_ms: float | None = None
    gust_ms: float | None = None
    humidity: float | None = None
    dew_point_c: float | None = None
    temperature_c: float | None = None
    # 7Timer's astronomy indices, 1 (best) to 8 (worst); None from sources without them.
    seeing: float | None = None
    transparency: float | None = None

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {"t": _iso(self.time)}
        for name in _FIELDS:
            value = getattr(self, name)
            payload[_JSON_NAMES[name]] = None if value is None else round(float(value), 3)
        return payload

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> HourForecast:
        values = {name: payload.get(_JSON_NAMES[name]) for name in _FIELDS}
        return cls(time=_parse(str(payload["t"])), **{k: (None if v is None else float(v)) for k, v in values.items()})


@dataclass(frozen=True)
class Forecast:
    """One source's forecast as fetched at one moment."""

    provider: str
    model: str
    issued_at: datetime | None
    fetched_at: datetime
    hours: tuple[HourForecast, ...]

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"

    def to_json(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "issuedAt": None if self.issued_at is None else _iso(self.issued_at),
            "fetchedAt": _iso(self.fetched_at),
            "hours": [hour.to_json() for hour in self.hours],
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> Forecast:
        issued = payload.get("issuedAt")
        return cls(
            provider=str(payload["provider"]),
            model=str(payload["model"]),
            issued_at=None if issued is None else _parse(str(issued)),
            fetched_at=_parse(str(payload["fetchedAt"])),
            hours=tuple(HourForecast.from_json(hour) for hour in payload["hours"]),  # type: ignore[union-attr]
        )


class ProviderError(Exception):
    """A source could not be read; the message carries no key or token."""


class Provider(Protocol):
    """A forecast source.  ``fetch`` returns one Forecast per model it offers, or raises ProviderError."""

    name: str

    def fetch(self, latitude: float, longitude: float) -> list[Forecast]: ...


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse(text: str) -> datetime:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def hour_of(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def weighted_median(values: Sequence[tuple[float, float]]) -> float | None:
    """The value past half the total weight; a tie goes to the higher (worse) value. None without values."""

    pairs = sorted((v, w) for v, w in values if w > 0)
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    run = 0.0
    for value, weight in pairs:
        run += weight
        if run > total / 2:
            return value
    return pairs[-1][0]


def consensus(
    forecasts: Sequence[Forecast], weights: Mapping[str, float], hours: Sequence[datetime]
) -> list[HourForecast]:
    """One forecast per hour: the weighted median of every source, field by field."""

    by_key: dict[str, dict[datetime, HourForecast]] = {}
    for forecast in forecasts:
        by_key[forecast.key] = {hour_of(h.time): h for h in forecast.hours}
    result = []
    for moment in hours:
        hour = hour_of(moment)
        merged: dict[str, float | None] = {}
        for name in _FIELDS:
            pairs = []
            for key, table in by_key.items():
                entry = table.get(hour)
                value = None if entry is None else getattr(entry, name)
                if value is not None:
                    pairs.append((float(value), float(weights.get(key, 1.0))))
            merged[name] = weighted_median(pairs)
        result.append(HourForecast(time=hour, **merged))
    return result


# --- skill: how right each source has been at this site -----------------------------


@dataclass(frozen=True)
class SkillScore:
    key: str
    n: int
    # Mean absolute error of the cloud fraction, 0..1.
    mae: float
    # Share of hours where "clear" (cloud below 0.3) was predicted right.
    hit_rate: float

    def to_json(self) -> dict[str, object]:
        return {"key": self.key, "n": self.n, "mae": round(self.mae, 3), "hitRate": round(self.hit_rate, 3)}


def score_forecasts(
    snapshots: Sequence[Forecast],
    observed_cloud: Mapping[datetime, float],
    *,
    lead_hours: tuple[float, float] = (2.0, 14.0),
    target_lead_hours: float = 6.0,
) -> list[dict[str, object]]:
    """Compare what each source said ``target_lead_hours`` before an hour with what the camera saw.

    ``observed_cloud`` maps UTC hours to the measured cloud fraction (1 - coverage).  For
    every source and hour the snapshot fetched closest to the target lead, within the lead
    window, is scored.  Returns one record per (source, hour).
    """

    records: list[dict[str, object]] = []
    by_key: dict[str, list[Forecast]] = {}
    for snapshot in snapshots:
        by_key.setdefault(snapshot.key, []).append(snapshot)
    for key, taken in by_key.items():
        for hour, observed in sorted(observed_cloud.items()):
            hour = hour_of(hour)
            best: tuple[float, float] | None = None
            for snapshot in taken:
                lead = (hour - snapshot.fetched_at).total_seconds() / 3600.0
                if not lead_hours[0] <= lead <= lead_hours[1]:
                    continue
                predicted = next(
                    (h.cloud for h in snapshot.hours if hour_of(h.time) == hour and h.cloud is not None), None
                )
                if predicted is None:
                    continue
                distance = abs(lead - target_lead_hours)
                if best is None or distance < best[0]:
                    best = (distance, float(predicted))
            if best is not None:
                records.append(
                    {"key": key, "t": _iso(hour), "forecast": round(best[1], 3), "observed": round(float(observed), 3)}
                )
    return records


def summarize_skill(records: Sequence[Mapping[str, object]], *, since: datetime | None = None) -> dict[str, SkillScore]:
    totals: dict[str, list[tuple[float, float]]] = {}
    for record in records:
        if since is not None and _parse(str(record["t"])) < since:
            continue
        totals.setdefault(str(record["key"]), []).append((float(record["forecast"]), float(record["observed"])))  # type: ignore[arg-type]
    scores = {}
    for key, pairs in totals.items():
        if not pairs:
            continue
        mae = sum(abs(f - o) for f, o in pairs) / len(pairs)
        hits = sum(1 for f, o in pairs if (f < 0.3) == (o < 0.3)) / len(pairs)
        scores[key] = SkillScore(key, len(pairs), mae, hits)
    return scores


def weights_from_skill(scores: Mapping[str, SkillScore], keys: Sequence[str], *, min_n: int = 24) -> dict[str, float]:
    """Sources that have been right here weigh more; unscored ones weigh like an average scored one."""

    scored = {key: 1.0 / (scores[key].mae + 0.05) for key in keys if key in scores and scores[key].n >= min_n}
    default = (sum(scored.values()) / len(scored)) if scored else 1.0
    return {key: scored.get(key, default) for key in keys}


def observed_cloud_from_history(entries: Sequence[Mapping[str, object]]) -> dict[datetime, float]:
    """Hourly cloud fraction measured by the cameras, from the monitor's history entries.

    Hours under a bright Moon are left out: moonlight hides stars the way cloud does, and a
    forecast must not be marked wrong for that.
    """

    buckets: dict[datetime, list[float]] = {}
    for entry in entries:
        coverage = entry.get("coverage")
        if coverage is None or entry.get("verdict") == "UNKNOWN":
            continue
        moon = entry.get("moon")
        if isinstance(moon, Mapping):
            altitude = float(moon.get("altitudeDeg") or 0.0)
            strength = float(moon.get("illumination") or 0.0) * min(max(altitude / 30.0, 0.0), 1.0)
            if strength > 0.3:
                continue
        try:
            hour = hour_of(_parse(str(entry["updatedAt"])))
        except (KeyError, ValueError):
            continue
        buckets.setdefault(hour, []).append(1.0 - float(coverage))  # type: ignore[arg-type]
    observed = {}
    for hour, values in buckets.items():
        if len(values) >= 5:
            observed[hour] = sorted(values)[len(values) // 2]
    return observed


# --- the service: fetch, keep, summarise -------------------------------------------


class ForecastService:
    """Fetches every source on a schedule, keeps the snapshots, scores them and answers for tonight."""

    def __init__(
        self,
        providers: Sequence[Provider],
        latitude: float,
        longitude: float,
        directory: Path,
        *,
        refresh_seconds: float = 3600.0,
        coordinate_precision: int = 2,
        keep_days: int = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._providers = tuple(providers)
        self._latitude = round(latitude, coordinate_precision)
        self._longitude = round(longitude, coordinate_precision)
        self._directory = Path(directory)
        self._refresh = float(refresh_seconds)
        self._keep_days = int(keep_days)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._latest: dict[str, Forecast] = {}
        self._errors: dict[str, str] = {}
        self._refreshed_at: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        (self._directory / "snapshots").mkdir(parents=True, exist_ok=True)
        self._load_latest()

    # Files: latest.json (what the page shows), snapshots/<date>.jsonl (what is scored later),
    # skill.jsonl (the scores).

    def _load_latest(self) -> None:
        try:
            stored = json.loads((self._directory / "latest.json").read_text(encoding="utf-8"))
            for payload in stored.get("forecasts", []):
                forecast = Forecast.from_json(payload)
                self._latest[forecast.key] = forecast
            refreshed = stored.get("refreshedAt")
            self._refreshed_at = None if refreshed is None else _parse(str(refreshed))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def refresh(self) -> None:
        """Ask every source once; a source that fails keeps its last answer and shows its error."""

        now = self._clock()
        for provider in self._providers:
            try:
                forecasts = provider.fetch(self._latitude, self._longitude)
            except ProviderError as error:
                with self._lock:
                    self._errors[provider.name] = str(error)
                _LOG.warning("forecast %s: %s", provider.name, error)
                continue
            except Exception as error:  # a broken adapter must not take the monitor down
                with self._lock:
                    self._errors[provider.name] = f"{type(error).__name__}: {error}"
                _LOG.exception("forecast %s failed", provider.name)
                continue
            with self._lock:
                self._errors.pop(provider.name, None)
                for forecast in forecasts:
                    self._latest[forecast.key] = forecast
            self._record(forecasts, now)
        with self._lock:
            self._refreshed_at = now
            latest = list(self._latest.values())
        try:
            payload = {"refreshedAt": _iso(now), "forecasts": [f.to_json() for f in latest]}
            _write_atomic(self._directory / "latest.json", json.dumps(payload, ensure_ascii=False))
        except OSError as error:
            _LOG.warning("forecast: cannot write latest.json: %s", error)

    def _record(self, forecasts: Sequence[Forecast], now: datetime) -> None:
        path = self._directory / "snapshots" / f"{now.astimezone(UTC).date().isoformat()}.jsonl"
        try:
            with path.open("a", encoding="utf-8") as stream:
                for forecast in forecasts:
                    stream.write(json.dumps(forecast.to_json(), ensure_ascii=False) + "\n")
        except OSError as error:
            _LOG.warning("forecast: cannot keep a snapshot: %s", error)

    def snapshots(self, since: datetime) -> list[Forecast]:
        found = []
        for path in sorted((self._directory / "snapshots").glob("????-??-??.jsonl")):
            if path.stem < since.astimezone(UTC).date().isoformat():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    found.append(Forecast.from_json(json.loads(line)))
                except (ValueError, KeyError, TypeError):
                    continue
        return found

    def score_night(self, observed_cloud: Mapping[datetime, float]) -> int:
        """Score every source against last night's camera measurements; returns the records added."""

        if not observed_cloud:
            return 0
        earliest = min(observed_cloud) - timedelta(hours=16)
        records = score_forecasts(self.snapshots(earliest), observed_cloud)
        if records:
            try:
                with (self._directory / "skill.jsonl").open("a", encoding="utf-8") as stream:
                    for record in records:
                        stream.write(json.dumps(record) + "\n")
            except OSError as error:
                _LOG.warning("forecast: cannot keep the scores: %s", error)
        return len(records)

    def skills(self, days: int = 30) -> dict[str, SkillScore]:
        try:
            lines = (self._directory / "skill.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return {}
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return summarize_skill(records, since=self._clock() - timedelta(days=days))

    def prune(self, today: date) -> None:
        oldest = (today - timedelta(days=self._keep_days)).isoformat()
        for path in (self._directory / "snapshots").glob("????-??-??.jsonl"):
            if path.stem < oldest:
                path.unlink(missing_ok=True)

    def summary(
        self,
        night_hours: Sequence[datetime],
        *,
        rain_window_hours: float = 0.0,
        rain_probability: float = 0.5,
        rain_mm: float = 0.2,
    ) -> dict[str, object]:
        """The block the status carries: tonight hour by hour, the sources, their scores, rain ahead."""

        with self._lock:
            latest = dict(self._latest)
            errors = dict(self._errors)
            refreshed = self._refreshed_at
        skills = self.skills()
        keys = sorted(latest)
        weights = weights_from_skill(skills, keys)
        merged = consensus(list(latest.values()), weights, night_hours)
        night = []
        for hour in merged:
            entry = hour.to_json()
            entry["sources"] = {}
            for key, forecast in latest.items():
                match = next((h.cloud for h in forecast.hours if hour_of(h.time) == hour.time), None)
                entry["sources"][key] = None if match is None else round(float(match), 3)
            night.append(entry)
        now = self._clock()
        soon = [h for h in merged if 0 <= (h.time - hour_of(now)).total_seconds() / 3600.0 < rain_window_hours]
        rain_soon = rain_window_hours > 0 and any(
            (h.precipitation_probability or 0.0) >= rain_probability or (h.precipitation_mm or 0.0) >= rain_mm
            for h in soon
        )
        providers = []
        for name in sorted({*(f.provider for f in latest.values()), *errors}):
            models = [f for f in latest.values() if f.provider == name]
            for forecast in models or [None]:
                key = forecast.key if forecast else name
                skill = skills.get(key)
                providers.append(
                    {
                        "key": key,
                        "provider": name,
                        "model": forecast.model if forecast else None,
                        "issuedAt": None
                        if forecast is None or forecast.issued_at is None
                        else _iso(forecast.issued_at),
                        "fetchedAt": None if forecast is None else _iso(forecast.fetched_at),
                        "error": errors.get(name),
                        "skill": None if skill is None else skill.to_json(),
                        "weight": round(weights.get(key, 1.0), 3) if forecast else None,
                    }
                )
        winds = [h.wind_ms for h in merged if h.wind_ms is not None]
        return {
            "refreshedAt": None if refreshed is None else _iso(refreshed),
            "night": night,
            "rainSoon": rain_soon,
            "rainWindowHours": rain_window_hours,
            "maxWindMs": None if not winds else round(max(winds), 1),
            "providers": providers,
        }

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.is_set():
                due = (
                    self._refreshed_at is None or (self._clock() - self._refreshed_at).total_seconds() >= self._refresh
                )
                if due:
                    try:
                        self.refresh()
                    except Exception:
                        _LOG.exception("forecast refresh failed")
                self._stop.wait(30.0)

        self._thread = threading.Thread(target=loop, name="forecast", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def _write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            threading.Event().wait(0.05 * (attempt + 1))

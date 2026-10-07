from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from skymonitor.analysis import Verdict
from skymonitor.config import parse_config
from skymonitor.decision import DecisionMachine
from skymonitor.forecast import (
    Forecast,
    ForecastService,
    HourForecast,
    ProviderError,
    consensus,
    hour_of,
    observed_cloud_from_history,
    score_forecasts,
    summarize_skill,
    weighted_median,
    weights_from_skill,
)
from skymonitor.service import Monitor, SafetyHolder
from skymonitor.synthetic import SkyScene
from skymonitor_helpers import NIGHT, Clock, SkySource

T0 = hour_of(NIGHT)


def _hours(start: datetime, count: int, **values) -> tuple[HourForecast, ...]:
    return tuple(HourForecast(time=start + timedelta(hours=i), **values) for i in range(count))


class FakeProvider:
    """A source that answers with one model, or fails."""

    def __init__(
        self,
        name: str,
        cloud: float,
        *,
        rain: float = 0.0,
        fail: bool = False,
        model: str = "m",
        clock: Clock | None = None,
    ) -> None:
        self.name, self.cloud, self.rain, self.fail, self.model = name, cloud, rain, fail, model
        self.clock = clock
        self.calls = 0

    def fetch(self, latitude: float, longitude: float) -> list[Forecast]:
        self.calls += 1
        assert latitude == round(latitude, 2) and longitude == round(longitude, 2)
        if self.fail:
            raise ProviderError("the server said no")
        now = self.clock.now if self.clock is not None else datetime.now(UTC)
        hours = _hours(T0 - timedelta(hours=12), 48, cloud=self.cloud, precipitation_probability=self.rain, wind_ms=3.0)
        return [Forecast(self.name, self.model, issued_at=None, fetched_at=now, hours=hours)]


def test_weighted_median_and_consensus() -> None:
    assert weighted_median([(0.2, 1.0), (0.9, 1.0), (0.5, 1.0)]) == 0.5
    assert weighted_median([(0.2, 1.0), (0.9, 3.0)]) == 0.9
    assert weighted_median([]) is None

    a = Forecast("a", "m", None, T0, _hours(T0, 3, cloud=0.2, wind_ms=2.0))
    b = Forecast("b", "m", None, T0, _hours(T0, 2, cloud=0.8))
    merged = consensus([a, b], {"a/m": 1.0, "b/m": 3.0}, [T0, T0 + timedelta(hours=2)])

    assert [h.cloud for h in merged] == [0.8, 0.2]
    assert merged[0].wind_ms == 2.0 and merged[0].humidity is None


def test_scoring_picks_the_forecast_made_six_hours_ahead() -> None:
    hour = T0 + timedelta(hours=10)
    early = Forecast("a", "m", None, hour - timedelta(hours=12), _hours(T0, 24, cloud=0.1))
    evening = Forecast("a", "m", None, hour - timedelta(hours=5), _hours(T0, 24, cloud=0.6))
    late = Forecast("a", "m", None, hour - timedelta(hours=1), _hours(T0, 24, cloud=0.9))

    records = score_forecasts([early, evening, late], {hour: 0.7})

    assert records == [{"key": "a/m", "t": "2026-10-07T00:00:00Z", "forecast": 0.6, "observed": 0.7}]


def test_skill_weights_favour_the_source_that_was_right_here() -> None:
    records = []
    for index in range(30):
        moment = (T0 + timedelta(hours=index)).isoformat().replace("+00:00", "Z")
        records.append({"key": "good/m", "t": moment, "forecast": 0.2, "observed": 0.25})
        records.append({"key": "bad/m", "t": moment, "forecast": 0.8, "observed": 0.25})

    scores = summarize_skill(records)
    weights = weights_from_skill(scores, ["good/m", "bad/m", "new/m"])

    assert scores["good/m"].n == 30 and scores["good/m"].mae == pytest.approx(0.05)
    assert scores["good/m"].hit_rate == 1.0 and scores["bad/m"].hit_rate == 0.0
    assert weights["good/m"] > weights["new/m"] > weights["bad/m"]
    assert weights_from_skill(scores, ["good/m"], min_n=31) == {"good/m": 1.0}


def test_observed_cloud_comes_from_the_cameras_hourly() -> None:
    entries = []
    for minute in range(0, 60, 10):
        moment = (T0 + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")
        entries.append({"updatedAt": moment, "coverage": 0.9 if minute < 30 else 0.5, "verdict": "CLEAR"})
    entries.append({"updatedAt": (T0 + timedelta(hours=1)).isoformat(), "coverage": 0.1, "verdict": "UNKNOWN"})
    entries.append({"updatedAt": (T0 + timedelta(hours=2)).isoformat(), "coverage": 0.1, "verdict": "CLOUDY"})

    observed = observed_cloud_from_history(entries)

    # Six looks in the first hour: the median; the second hour has one unknown look, the third too few.
    assert observed == {T0: pytest.approx(0.5)}


def test_the_service_keeps_snapshots_scores_them_and_summarises_tonight(tmp_path) -> None:
    clock = Clock()
    good = FakeProvider("good", 0.2, clock=clock)
    bad = FakeProvider("bad", 0.9, rain=0.8, clock=clock)
    broken = FakeProvider("broken", 0.5, fail=True, clock=clock)
    service = ForecastService([good, bad, broken], 26.70, 100.03, tmp_path / "forecast", clock=clock)

    service.refresh()
    night = [T0 + timedelta(hours=i) for i in range(8)]
    summary = service.summary(night, rain_window_hours=3.0)

    assert (good.calls, bad.calls) == (1, 1)
    assert (tmp_path / "forecast" / "latest.json").is_file()
    assert len(list((tmp_path / "forecast" / "snapshots").glob("*.jsonl"))) == 1
    assert [h["cloud"] for h in summary["night"]] == [0.9] * 8  # equal weights: the median of two is the upper one
    assert summary["night"][0]["sources"] == {"bad/m": 0.9, "good/m": 0.2}
    assert summary["rainSoon"] is True and summary["maxWindMs"] == 3.0
    broken_entry = next(p for p in summary["providers"] if p["provider"] == "broken")
    assert broken_entry["error"] == "the server said no" and broken_entry["skill"] is None

    # The cameras saw a clear night: "good" was right, and from now on it outweighs "bad".
    observed = {T0 + timedelta(hours=i + 3): 0.15 for i in range(6)}
    clock.advance(10 * 3600)
    assert service.score_night(observed) == 12
    skills = service.skills()
    assert skills["good/m"].mae == pytest.approx(0.05) and skills["bad/m"].mae == pytest.approx(0.75)
    weighted = ForecastService([good, bad], 26.70, 100.03, tmp_path / "forecast", clock=clock)
    weighted.refresh()
    assert [h["cloud"] for h in weighted.summary(night)["night"]][:2] == [0.9, 0.9]  # n < 24: not yet trusted
    for _ in range(3):
        service.score_night(observed)
    reloaded = ForecastService([good, bad], 26.70, 100.03, tmp_path / "forecast", clock=clock)
    assert [h["cloud"] for h in reloaded.summary(night)["night"]][:2] == [0.2, 0.2]

    service.prune(date(2030, 1, 1))
    assert list((tmp_path / "forecast" / "snapshots").glob("*.jsonl")) == []


def test_rain_in_the_forecast_holds_the_roof_closed_without_losing_the_wait() -> None:
    machine = DecisionMachine()
    when = NIGHT
    decisions = []
    for minute in range(14):
        when += timedelta(minutes=1)
        veto = "RAIN_FORECAST" if minute < 12 else None
        decisions.append(machine.update(when, [Verdict.CLEAR], [0.9], -30.0, veto=veto))

    assert [d.reason for d in decisions[:10]] == ["WAITING"] * 10
    assert [d.reason for d in decisions[10:12]] == ["RAIN_FORECAST"] * 2
    assert not decisions[11].safe and decisions[11].held_seconds >= 600
    assert decisions[12].safe and decisions[12].reason == "CLEAR"


def test_the_monitor_carries_the_forecast_and_obeys_the_rain_veto(tmp_path) -> None:
    clock = Clock()
    settings = {
        "language": "zh",
        "site": {"name": "Test site", "latitude": 26.70, "longitude": 100.03},
        "decision": {"open_after_minutes": 2.0},
        "analysis": {"fixed_window_minutes": 10.0},
        "camera": [{"name": "sky", "type": "file", "path": "unused.png"}],
        "output": {"directory": "data"},
        "forecast": {"rain_veto_hours": 3.0, "providers": ["open-meteo"]},
    }
    config = parse_config(settings, tmp_path / "sky-monitor.toml")
    rainy = FakeProvider("rainy", 0.6, rain=0.9)
    service = ForecastService([rainy], 26.70, 100.03, config.output.directory / "forecast", clock=clock)
    service.refresh()
    monitor = Monitor(
        config, sources={"sky": SkySource(SkyScene(), clock)}, clock=clock, holder=SafetyHolder(), forecast=service
    )

    statuses = []
    for _ in range(9):
        statuses.append(monitor.cycle())
        clock.advance(60)

    assert statuses[-1]["forecast"]["rainSoon"] is True
    assert len(statuses[-1]["forecast"]["night"]) >= 6
    assert statuses[-1]["reason"] == "RAIN_FORECAST" and not statuses[-1]["safe"]
    assert "预报 3 小时内有雨" in statuses[-1]["message"]
    lines = (config.output.directory / "history").glob("*.jsonl")
    assert all(
        "forecast" not in json.loads(line) for path in lines for line in path.read_text(encoding="utf-8").splitlines()
    )

    rainy.rain = 0.0
    service.refresh()
    dry = monitor.cycle()
    assert dry["safe"] and dry["reason"] == "CLEAR"
    monitor.close()
